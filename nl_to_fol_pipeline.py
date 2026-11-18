"""Natural-language premises and a query, translated to FOL, then checked for entailment.

The reasoning call is the validated Z3 engine in ``fol_engine.answer``:

    KB entails Q  iff  KB and not Q is unsatisfiable.

Unknown, timeout, and translation failure stay inconclusive. They are not
turned into False.

Transformers are not installed and this machine has no GPU, so the translator
is grammatical. RuleTaker and CounterBench in this repository are templated.
FOLIO is free English; sentences outside the grammar stay unsupported.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import fol_engine

# ---------------------------------------------------------------------------
# Shared result types
# ---------------------------------------------------------------------------


@dataclass
class Translation:
    premises: tuple = ()
    query: str = ""
    status: str = "unsupported"
    detail: str = ""
    family: str = ""
    seconds: float = 0.0


@dataclass
class PipelineResult:
    prediction: bool | None
    translation_status: str
    inference_status: str
    fol_premises: str
    fol_query: str
    family: str
    detail: str
    translation_seconds: float
    reasoning_seconds: float
    total_seconds: float
    error_category: str = ""


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("es") and not word.endswith("ss"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _camel(text: str) -> str:
    words = [_singular(word) for word in re.findall(r"[A-Za-z0-9]+", text.lower())]
    return "".join(word.capitalize() for word in words) or "Thing"


def _const(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    if not words:
        return "c"
    return words[0] + "".join(word.capitalize() for word in words[1:])


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip()).rstrip(".").strip()


# ---------------------------------------------------------------------------
# RuleTaker: templated facts and rules, ground Horn formulas
# ---------------------------------------------------------------------------

_RULES = (
    re.compile(r"^assuming (.+), we can say (.+)$", re.IGNORECASE),
    re.compile(r"^if (.+), then (.+)$", re.IGNORECASE),
    re.compile(r"^based on (.+), it is evident that (.+)$", re.IGNORECASE),
    re.compile(r"^given that (.+), it follows that (.+)$", re.IGNORECASE),
    re.compile(r"^when (.+), we can conclude (.+)$", re.IGNORECASE),
)


def _rule_atom(sentence: str) -> str:
    text = _clean(sentence).lower()
    text = re.sub(r"^(the|a|an)\s+", "", text)
    article = re.fullmatch(r"(.+?) is (?:a|an) (.+)", text)
    if article:
        return f"{_camel(article.group(2))}({_const(article.group(1))})"
    adjective = re.fullmatch(r"(.+?) is (.+)", text)
    if adjective:
        return f"{_camel(adjective.group(2))}({_const(adjective.group(1))})"
    parts = text.split()
    if len(parts) < 2:
        raise ValueError(f"cannot atomize {sentence!r}")
    return f"{_camel(' '.join(parts[1:]))}({_const(parts[0])})"


def _is_ruletaker(facts: str) -> bool:
    lowered = facts.lower()
    return "\nrules:" in lowered or lowered.startswith("rules:") or "we can say" in lowered


def translate_ruletaker(facts: str, query: str) -> Translation:
    started = time.perf_counter()
    premises = []
    try:
        for line in str(facts).splitlines():
            line = line.strip().rstrip(".")
            if not line:
                continue
            header = line.rstrip(":").lower()
            if header in {"facts", "rules"}:
                continue
            matched = next((pattern.match(line) for pattern in _RULES if pattern.match(line)), None)
            if matched:
                body = _rule_atom(matched.group(1))
                head = _rule_atom(matched.group(2))
                premises.append(f"{body} → {head}")
            else:
                premises.append(_rule_atom(line))
        goal = _rule_atom(query)
    except ValueError as exc:
        return Translation(status="parse_error", detail=str(exc), family="ruletaker",
                           seconds=time.perf_counter() - started)
    if not premises:
        return Translation(status="parse_error", detail="no premises", family="ruletaker",
                           seconds=time.perf_counter() - started)
    return Translation(tuple(premises), goal, "ok", "", "ruletaker", time.perf_counter() - started)


def horn_entails(premises: tuple, query: str):
    """Forward chaining. Finished search on ground Horn rules is a real answer.

    A missing atom is not entailed. The rule set has no function symbols and no
    variables, so the closure is finite and this procedure is complete for it.
    """
    started = time.perf_counter()
    facts = set()
    rules = []
    for line in premises:
        if "→" in line:
            body, head = (piece.strip() for piece in line.split("→", 1))
            rules.append((body, head))
        else:
            facts.add(line.strip())
    changed = True
    while changed:
        changed = False
        for body, head in rules:
            if body in facts and head not in facts:
                facts.add(head)
                changed = True
    entailed = query.strip() in facts
    status = "entailed" if entailed else "not_entailed"
    return entailed, status, time.perf_counter() - started


# ---------------------------------------------------------------------------
# CounterBench: causal English to a factual/counterfactual FOL theory
# ---------------------------------------------------------------------------

_SPLIT = re.compile(
    r", (?:and )?(?=(?:not )?[A-Z][a-z]+(?: (?:and|or) (?:not )?[A-Z][a-z]+)* (?:together )?cause)"
)


def _atoms(text: str):
    return re.findall(r"not [A-Z][a-z]+|[A-Z][a-z]+", text)


def _bool_formula(text: str):
    items = []
    for atom in _atoms(text):
        if atom.startswith("not "):
            items.append(("not", ("v", atom.split()[1])))
        else:
            items.append(("v", atom))
    if not items:
        raise ValueError(f"no atoms in {text!r}")
    padded = f" {text} "
    if " and " in padded:
        return ("and", *items) if len(items) > 1 else items[0]
    if " or " in padded:
        return ("or", *items) if len(items) > 1 else items[0]
    return items[0]


def _parents(node, found: set):
    if node[0] == "v":
        found.add(node[1])
    else:
        for child in node[1:]:
            _parents(child, found)


def _parse_causal(facts: str):
    observations = []
    observed = re.search(r"We observed (.+?)\.?$", facts)
    body = facts
    if observed:
        body = facts[: observed.start()]
        for atom in _atoms(observed.group(1)):
            if atom.startswith("not "):
                observations.append((atom.split()[1], False))
            else:
                observations.append((atom, True))
    mentioned = set(re.findall(r"([A-Z][a-z]+)~", body))
    has_bern = bool(mentioned)
    body = re.sub(r"\s*[A-Z][a-z]+~\s*Bern\([^)]+\)\.?", "", body)
    body = re.sub(r"^We know that ", "", body).strip().rstrip(".")
    pieces = {}
    for part in _SPLIT.split(body):
        match = re.fullmatch(r"(.+?) (?:together )?causes? (.+)", part.strip())
        if not match:
            raise ValueError(f"unparsed causal clause {part!r}")
        left = _bool_formula(match.group(1))
        for effect in _atoms(match.group(2)):
            negative = effect.startswith("not ")
            name = effect.split()[-1]
            piece = ("not", left) if negative else left
            pieces.setdefault(name, []).append(piece)
            found = set()
            _parents(left, found)
            mentioned |= found
            mentioned.add(name)
    equations = {}
    for name, options in pieces.items():
        equations[name] = options[0] if len(options) == 1 else ("or", *options)
    return equations, mentioned, observations, has_bern or observed is not None


def _occur(world: str, name: str) -> str:
    return f"Occur{world}({_const(name)})"


def _formula_fol(node, world: str) -> str:
    if node[0] == "v":
        return _occur(world, node[1])
    if node[0] == "not":
        return f"¬({_formula_fol(node[1], world)})"
    joiner = " ∧ " if node[0] == "and" else " ∨ "
    return "(" + joiner.join(_formula_fol(child, world) for child in node[1:]) + ")"


def _counterfactual_question(query: str):
    match = re.search(r"Would ([A-Z][a-z]+) occur if not ([A-Z][a-z]+) instead of \2", query)
    if match:
        return match.group(1), [match.group(2)]
    match = re.search(
        r"Would ([A-Z][a-z]+) occur if not ([A-Z][a-z]+) and not ([A-Z][a-z]+)", query
    )
    if match:
        return match.group(1), [match.group(2), match.group(3)]
    match = re.search(
        r"Assume not ([A-Z][a-z]+), and based on this assumption, further suppose not "
        r"([A-Z][a-z]+)\. Would ([A-Z][a-z]+) occur",
        query,
    )
    if match:
        return match.group(3), [match.group(1), match.group(2)]
    return None


def _is_counterbench(facts: str, query: str) -> bool:
    return "causes" in facts and ("Would " in query or query.startswith("Assume not "))


def translate_counterbench(facts: str, query: str) -> Translation:
    """Twin-network theory. OccurF is the factual world, OccurC the intervened world.

    Each "causes" sentence is a structural equation, not a one-way implication.
    Several mechanisms for one variable are combined with OR. An intervention
    deletes that variable's equation and sets it false in the counterfactual
    world. Exogenous variables keep the same value in both worlds. Observations
    constrain only the factual world. A Bernoulli prior is recorded and is not
    turned into a probability; the Boolean equations decide the query.
    """
    started = time.perf_counter()
    parsed = _counterfactual_question(query)
    if parsed is None:
        return Translation(status="unsupported", detail="unrecognized counterfactual question",
                           family="counterbench", seconds=time.perf_counter() - started)
    target, interventions = parsed
    try:
        equations, mentioned, observations, _extra = _parse_causal(facts)
    except ValueError as exc:
        return Translation(status="parse_error", detail=str(exc), family="counterbench",
                           seconds=time.perf_counter() - started)
    lines = []
    for name, node in equations.items():
        lines.append(f"{_occur('F', name)} ↔ {_formula_fol(node, 'F')}")
        if name not in interventions:
            lines.append(f"{_occur('C', name)} ↔ {_formula_fol(node, 'C')}")
    for name in sorted(mentioned - set(equations)):
        if name not in interventions:
            lines.append(f"{_occur('F', name)} ↔ {_occur('C', name)}")
    for name in interventions:
        lines.append(f"¬{_occur('C', name)}")
    for name, value in observations:
        atom = _occur("F", name)
        lines.append(atom if value else f"¬({atom})")
    goal = _occur("C", target)
    detail = f"interventions={','.join(interventions)}; observations={len(observations)}"
    return Translation(tuple(lines), goal, "ok", detail, "counterbench", time.perf_counter() - started)


# ---------------------------------------------------------------------------
# FOLIO: grammar for the sentence shapes it can honestly cover
# ---------------------------------------------------------------------------

_GENERIC = {"people", "person", "one", "someone", "somebody", "things", "thing", "individuals"}


def _phrase(text: str, argument: str) -> str:
    original = text.lower()
    negated = False
    if "unaware" in original:
        negated = True
        original = original.replace("unaware", "aware")
    for pattern in (
        r"^(?:does not|do not|doesn't|don't|is not|isn't|are not|aren't|cannot|can't)\s+",
        r"^not\s+",
    ):
        if re.match(pattern, original):
            negated = not negated
            original = re.sub(pattern, "", original, count=1)
    original = re.sub(r"\b(?:a|an|the)\b", " ", original)
    original = re.sub(r"\b(?:drinks|drinking)\b", "drink", original)
    original = re.sub(r"\b(?:goes|going)\b", "go", original)
    original = re.sub(r"\b(?:is|are|was|were|be|been|being)\b", " ", original)
    original = re.sub(r"\b(?:does|do)\b", " ", original)
    name = _camel(original)
    if name == "Thing":
        raise ValueError(f"empty phrase {text!r}")
    atom = f"{name}({argument})"
    return f"¬{atom}" if negated else atom


def _simple_phrase(text: str) -> bool:
    """A phrase with its own connective is not one predicate."""
    return re.search(r"\b(or|and|either|neither|who|if|then)\b|,", text, re.IGNORECASE) is None


def _subject_const(subject: str) -> str:
    subject = _clean(subject)
    if subject.lower() in {"he", "she", "they", "it"}:
        raise ValueError("unbound pronoun")
    return _const(subject)


def translate_folio_sentence(sentence: str) -> str:
    text = _clean(sentence)
    if not text:
        raise ValueError("empty sentence")
    both = re.fullmatch(r"(.+?), or (.+?), or both", text, re.IGNORECASE)
    if both:
        left = translate_folio_sentence(both.group(1))
        right = translate_folio_sentence(both.group(2).replace("they ", "people "))
        return f"({left}) ∨ ({right})"
    explicit = re.fullmatch(r"either (.+?) or (.+)", text, re.IGNORECASE)
    if explicit:
        return f"({translate_folio_sentence(explicit.group(1))}) ⊕ ({translate_folio_sentence(explicit.group(2))})"
    conditional = re.fullmatch(r"if (.+?), then (.+)", text, re.IGNORECASE)
    if conditional:
        return f"({translate_folio_sentence(conditional.group(1))}) → ({translate_folio_sentence(conditional.group(2))})"
    who = re.fullmatch(
        r"(?:all|every|each|any) (.+?) who (.+?) (?:are|is) (.+)", text, re.IGNORECASE
    )
    if who and _simple_phrase(who.group(2)) and _simple_phrase(who.group(3)):
        noun, restriction, result = who.groups()
        antecedent = _phrase(restriction, "x")
        if noun.lower() not in _GENERIC:
            antecedent = f"({_phrase(noun, 'x')}) ∧ ({antecedent})"
        return f"∀x (({antecedent}) → ({_phrase(result, 'x')}))"
    universal = re.fullmatch(
        r"(?:all|every|each|any) (.+?) (?:are|is) (?:a |an )?(.+)", text, re.IGNORECASE
    )
    if universal and _simple_phrase(universal.group(1)) and _simple_phrase(universal.group(2)):
        return f"∀x (({_phrase(universal.group(1), 'x')}) → ({_phrase(universal.group(2), 'x')}))"
    nobody = re.fullmatch(r"(?:no one|nobody) who (.+?) (?:is|are) (.+)", text, re.IGNORECASE)
    if nobody and _simple_phrase(nobody.group(1)) and _simple_phrase(nobody.group(2)):
        return f"∀x (({_phrase(nobody.group(1), 'x')}) → ¬({_phrase(nobody.group(2), 'x')}))"
    none_bare = re.fullmatch(r"no (.+?) (?:is|are) (?:a |an )?(.+)", text, re.IGNORECASE)
    if none_bare and _simple_phrase(none_bare.group(1)) and _simple_phrase(none_bare.group(2)):
        return f"∀x (({_phrase(none_bare.group(1), 'x')}) → ¬({_phrase(none_bare.group(2), 'x')}))"
    some = re.fullmatch(r"some (.+?) (?:are|is) (.+)", text, re.IGNORECASE)
    if some and _simple_phrase(some.group(1)) and _simple_phrase(some.group(2)):
        return f"∃x (({_phrase(some.group(1), 'x')}) ∧ ({_phrase(some.group(2), 'x')}))"
    named_not = re.fullmatch(
        r"([A-Z][\w'.-]*) is not (?:a |an )?(.+)", text
    )
    if named_not and _simple_phrase(named_not.group(2)):
        return f"¬{_phrase(named_not.group(2), _subject_const(named_not.group(1)))}"
    named_is = re.fullmatch(r"([A-Z][\w'.-]*) is (?:a |an )?(.+)", text)
    if named_is and _simple_phrase(named_is.group(2)):
        return _phrase(named_is.group(2), _subject_const(named_is.group(1)))
    named_does_not = re.fullmatch(r"([A-Z][\w'.-]*) (?:does not|doesn't|do not|don't) (.+)", text)
    if named_does_not and _simple_phrase(named_does_not.group(2)):
        return f"¬{_phrase(named_does_not.group(2), _subject_const(named_does_not.group(1)))}"
    named = re.fullmatch(r"([A-Z][\w'.-]*) (.+)", text)
    if (
        named
        and named.group(1).lower() not in {"all", "some", "no", "if", "either"}
        and _simple_phrase(named.group(2))
        and len(named.group(2).split()) <= 8
    ):
        return _phrase(named.group(2), _subject_const(named.group(1)))
    generic = re.fullmatch(r"(people|they|someone|somebody) (.+)", text, re.IGNORECASE)
    if generic and _simple_phrase(generic.group(2)):
        return _phrase(generic.group(2), _const(generic.group(1)))
    for separator, connective in ((" or ", "∨"), (" and ", "∧")):
        if separator in text.lower():
            index = text.lower().find(separator)
            left, right = text[:index], text[index + len(separator):]
            try:
                return f"({translate_folio_sentence(left)}) {connective} ({translate_folio_sentence(right)})"
            except ValueError:
                pass
    raise ValueError(f"unsupported sentence: {text}")


def translate_folio(facts: str, query: str) -> Translation:
    started = time.perf_counter()
    premises = []
    try:
        for line in str(facts).splitlines():
            line = line.strip()
            if not line:
                continue
            premises.append(translate_folio_sentence(line))
        goal = translate_folio_sentence(query)
    except ValueError as exc:
        return Translation(status="unsupported", detail=str(exc), family="folio",
                           seconds=time.perf_counter() - started)
    return Translation(tuple(premises), goal, "ok", "", "folio", time.perf_counter() - started)


# ---------------------------------------------------------------------------
# Dispatch and reasoning
# ---------------------------------------------------------------------------


def translate(facts: str, query: str) -> Translation:
    if _is_counterbench(facts, query):
        return translate_counterbench(facts, query)
    if _is_ruletaker(facts):
        return translate_ruletaker(facts, query)
    return translate_folio(facts, query)


def reason(translation: Translation, timeout_ms: int = 1000):
    """Run the validated engine. Ground Horn uses complete forward chaining.

    A sample of those Horn formulas is checked against Z3 in the unit tests.
    The two agree because the rules are variable-free. CounterBench and FOLIO
    go to Z3. A solver unknown is returned as None.
    """
    if translation.status != "ok":
        return None, translation.status, 0.0
    if translation.family == "ruletaker":
        return horn_entails(translation.premises, translation.query)
    started = time.perf_counter()
    block = "\n".join(translation.premises)
    result = fol_engine.answer(
        block, translation.query, timeout_ms=timeout_ms, align=False, unique_names=False
    )
    return result.prediction, result.status, time.perf_counter() - started


def categorize(family: str, translation_status: str, inference_status: str, prediction, gold: bool) -> str:
    if translation_status != "ok":
        return f"translation_{translation_status}"
    if prediction is None:
        return f"solver_{inference_status}"
    if prediction == gold:
        return ""
    if family == "counterbench":
        return "counterfactual_semantics"
    if family == "ruletaker":
        return "horn_closure_disagrees"
    return "entailment_disagrees_with_gold"


def predict_example(facts: str, query: str, gold: bool | None = None, timeout_ms: int = 1000) -> PipelineResult:
    started = time.perf_counter()
    translation = translate(facts, query)
    prediction, status, reasoning_seconds = reason(translation, timeout_ms=timeout_ms)
    total = time.perf_counter() - started
    category = ""
    if gold is not None:
        category = categorize(translation.family, translation.status, status, prediction, bool(gold))
    return PipelineResult(
        prediction=prediction,
        translation_status=translation.status,
        inference_status=status,
        fol_premises="\n".join(translation.premises),
        fol_query=translation.query,
        family=translation.family or "folio",
        detail=translation.detail,
        translation_seconds=translation.seconds,
        reasoning_seconds=reasoning_seconds,
        total_seconds=total,
        error_category=category,
    )


# ---------------------------------------------------------------------------
# Unit checks used by the notebook. These sentences are not dataset rows.
# ---------------------------------------------------------------------------

DEV_EXAMPLES = (
    {
        "name": "horn chain",
        "facts": "Facts:\nThe wall is red.\nRules:\nIf the wall is red, then the wall is large.",
        "query": "The wall is large.",
        "label": True,
    },
    {
        "name": "horn miss",
        "facts": "Facts:\nThe wall is red.\nRules:\nIf the river is red, then the wall is large.",
        "query": "The wall is large.",
        "label": False,
    },
    {
        "name": "causal chain off",
        "facts": "We know that Ziklo causes Blaf, Blaf causes Trune, and Trune causes Lumbo.",
        "query": "Would Lumbo occur if not Ziklo instead of Ziklo?",
        "label": False,
    },
    {
        "name": "prevention removed",
        "facts": "We know that Ziklo causes not Blaf, and Blaf causes Lumbo.",
        "query": "Would Lumbo occur if not Ziklo instead of Ziklo?",
        "label": True,
    },
    {
        "name": "all cats",
        "facts": "All cats are animals.\nFelix is a cat.",
        "query": "Felix is an animal.",
        "label": True,
    },
    {
        "name": "cat not a dog",
        "facts": "All cats are animals.\nFelix is a cat.",
        "query": "Felix is a dog.",
        "label": False,
    },
)


def run_unit_tests() -> list[str]:
    failures = []
    for example in DEV_EXAMPLES:
        result = predict_example(example["facts"], example["query"], example["label"])
        if result.prediction != example["label"] or result.translation_status != "ok":
            failures.append(
                f"{example['name']}: pred={result.prediction} status={result.translation_status} "
                f"{result.inference_status} {result.detail} FOL={result.fol_query}"
            )
    # The same ground Horn question, decided by forward chaining and by Z3.
    translation = translate_ruletaker(DEV_EXAMPLES[0]["facts"], DEV_EXAMPLES[0]["query"])
    chained, chained_status, _ = horn_entails(translation.premises, translation.query)
    solved = fol_engine.answer(
        "\n".join(translation.premises), translation.query, timeout_ms=1000, align=False, unique_names=False
    )
    if chained is not True or solved.prediction is not True:
        failures.append(f"horn/Z3 agreement failed: chain={chained_status} z3={solved.status}")
    rina_facts = (
        "All people who regularly drink coffee are dependent on caffeine.\n"
        "People regularly drink coffee, or they don't want to be addicted to caffeine, or both."
    )
    rina = translate_folio(rina_facts, "Rina regularly drinks coffee.")
    if rina.status != "ok" or "RegularlyDrinkCoffee" not in rina.query:
        failures.append(f"Rina naming failed: {rina.status} {rina.detail} {rina.query}")
    return failures

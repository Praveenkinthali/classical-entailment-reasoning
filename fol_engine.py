"""Classical FOL entailment for the unified FOLIO formulas.

The original engine in ``fol_engine_baseline.py`` is unchanged and is the
baseline. This module keeps that parser, then decides entailment with Z3:

    KB entails Q  iff  KB and not Q is unsatisfiable.

``unknown`` and parse failures stay inconclusive. They are not turned into
False.

Semantics that the baseline implemented only inside its clausifier, and that
the FOLIO annotations rely on:

* Free variables in premises are universally closed (axiom reading).
* Free variables in the query are existentially closed (witness reading).
  The baseline's clause form does the same thing, because a free variable in
  the negated conjecture is universal in that clause.
* If the premises do not positively identify any constants, distinct constant
  symbols denote distinct individuals (unique-name assumption).
* Query symbols may be aligned to an existing premise symbol of the same arity
  when SequenceMatcher ratio is at least 0.8. A name is not renamed just
  because one string contains the other. ``align=False`` checks the text literally.
"""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from typing import Optional

from z3 import (
    And,
    Bool,
    BoolSort,
    Const,
    DeclareSort,
    Exists,
    ForAll,
    FreshConst,
    Function,
    Implies,
    Not,
    Or,
    Solver,
    Xor,
    sat,
    unknown,
    unsat,
)

import fol_engine_baseline as baseline

ENTITY = DeclareSort("Entity")
_PREDICATES: dict = {}
_FUNCTIONS: dict = {}
_CONSTANTS: dict = {}

INFIX = set("∧∨⊕→↔⟷")


@dataclass
class Answer:
    prediction: Optional[bool]
    status: str
    detail: str = ""
    alignments: tuple = ()
    repaired_lines: tuple = ()
    seconds: float = 0.0
    parsed_premises: tuple = ()
    parsed_query: object = None


def interpret_check(result) -> tuple:
    """Map a solver result to (prediction, status). Unknown is not False."""
    if result == unsat:
        return True, "entailed"
    if result == sat:
        return False, "not_entailed"
    return None, "unknown"


def tokenize(text: str):
    return baseline.tokenize(text)


def parse_formula(text: str):
    return baseline.parse_formula(text)


def _insert_closers(text: str) -> str:
    """Insert a missing ')' before an infix operator that is still inside a term."""
    out = []
    depth = 0
    changed = False
    for ch in text:
        if ch == "(":
            depth += 1
            out.append(ch)
            continue
        if ch == ")":
            if depth:
                depth -= 1
            out.append(ch)
            continue
        if ch in INFIX and depth:
            prev = "".join(out).rstrip()
            if prev and prev[-1] not in "(,∧∨⊕→↔⟷":
                out.append(")")
                depth -= 1
                changed = True
        out.append(ch)
    if depth > 0:
        out.append(")" * depth)
        changed = True
    return "".join(out) if changed else text


def repair_formula(text: str) -> Optional[str]:
    """General parenthesis repairs. Returns None when nothing parses.

    The double-parenthesis collapse is tried before closer-insertion.
    Insertion rewrites ``Imprisoned((x) →`` into ``Imprisoned(x)) →``,
    which changes the quantifier scope, so it is only a fallback.
    """
    candidates = []
    collapsed = re.sub(
        r"\(\(([A-Za-z_][A-Za-z0-9_]*)\)\s*(?=[∧∨⊕→↔⟷])",
        r"(\1)",
        text,
    )
    for candidate in (collapsed, _insert_closers(text), _insert_closers(collapsed), text + ")"):
        if candidate and candidate != text and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        try:
            parse_formula(candidate)
        except Exception:
            continue
        return candidate
    return None


def parse_sentence(text: str):
    """Parse one formula. On failure, try a parenthesis repair.

    Returns (ast, repaired_text or None). Raises SyntaxError if both fail.
    """
    try:
        return parse_formula(text), None
    except Exception as first:
        repaired = repair_formula(text)
        if repaired is None:
            raise SyntaxError(f"could not parse formula: {text}") from first
        return parse_formula(repaired), repaired


def parse_premises(facts: str):
    premises = []
    repaired = []
    errors = []
    for line in str(facts).split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ast, fix = parse_sentence(line)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        premises.append(ast)
        if fix is not None:
            repaired.append(fix)
    return premises, tuple(repaired), errors


def free_variables(formula, bound=None, found=None):
    if bound is None:
        bound, found = set(), set()
    op = formula[0]
    if op == "atom":
        def walk(term):
            if isinstance(term, tuple):
                for arg in term[1:]:
                    walk(arg)
            elif isinstance(term, str) and term.startswith("?") and term not in bound:
                found.add(term)
        for arg in formula[2]:
            walk(arg)
    elif op == "not":
        free_variables(formula[1], bound, found)
    elif op in ("and", "or", "imp", "iff", "xor"):
        free_variables(formula[1], bound, found)
        free_variables(formula[2], bound, found)
    elif op in ("all", "ex"):
        free_variables(formula[2], bound | {"?" + formula[1]}, found)
    return found


def bind_free(formula, quantifier: str):
    """Close free variables. ``quantifier`` is ``all`` or ``ex``."""
    for name in sorted(free_variables(formula), reverse=True):
        formula = (quantifier, name[1:], formula)
    return formula


def _walk_formula(formula, on_atom):
    op = formula[0]
    if op == "atom":
        on_atom(formula)
    elif op == "not":
        _walk_formula(formula[1], on_atom)
    elif op in ("and", "or", "imp", "iff", "xor"):
        _walk_formula(formula[1], on_atom)
        _walk_formula(formula[2], on_atom)
    elif op in ("all", "ex"):
        _walk_formula(formula[2], on_atom)


def symbol_table(formulas):
    """Predicate names by arity, and individual constants, as the baseline aligner expects."""
    class Index:
        preds = {}
        consts = set()

    def walk_term(term):
        if isinstance(term, tuple):
            for arg in term[1:]:
                walk_term(arg)
        elif isinstance(term, str) and not term.startswith("?"):
            Index.consts.add(term)

    def on_atom(atom):
        if atom[1] != "=":
            Index.preds[(atom[1], len(atom[2]))] = 1
        for arg in atom[2]:
            walk_term(arg)

    for formula in formulas:
        _walk_formula(formula, on_atom)
    return Index


def closest_symbol(name, candidates, threshold=0.8):
    """Rename a query symbol only when the character overlap is actually high.

    The baseline also boosted a score when one name merely contained the other.
    That mapped RedToGreen onto Green and game onto a much longer predicate.
    SequenceMatcher ratio is the whole test here.
    """
    if name in candidates or not candidates:
        return name
    best, score = name, 0.0
    for candidate in candidates:
        ratio = difflib.SequenceMatcher(None, name, candidate).ratio()
        if ratio > score:
            best, score = candidate, ratio
    return best if score >= threshold else name


def align_formula(formula, index, threshold=0.8):
    def align_term(term):
        if isinstance(term, tuple):
            return (term[0],) + tuple(align_term(arg) for arg in term[1:])
        if term.startswith("?"):
            return term
        return closest_symbol(term, index.consts, threshold)

    op = formula[0]
    if op == "atom":
        name, args = formula[1], formula[2]
        if name != "=":
            choices = [pred for pred, arity in index.preds if arity == len(args)]
            name = closest_symbol(name, choices, threshold)
        return ("atom", name, tuple(align_term(arg) for arg in args))
    if op == "not":
        return ("not", align_formula(formula[1], index, threshold))
    if op in ("all", "ex"):
        return (op, formula[1], align_formula(formula[2], index, threshold))
    return (op, align_formula(formula[1], index, threshold), align_formula(formula[2], index, threshold))


def alignment_changes(before, after, changes):
    op = before[0]
    if op != after[0]:
        return
    if op == "atom":
        if before[1] != after[1]:
            changes.append(f"{before[1]}/{len(before[2])} -> {after[1]}/{len(after[2])}")
        for left, right in zip(before[2], after[2]):
            if isinstance(left, str) and isinstance(right, str) and left != right and not left.startswith("?"):
                changes.append(f"{left} -> {right}")
            elif isinstance(left, tuple) and isinstance(right, tuple):
                alignment_changes(("atom", left[0], left[1:]), ("atom", right[0], right[1:]), changes)
    elif op == "not":
        alignment_changes(before[1], after[1], changes)
    elif op in ("all", "ex"):
        alignment_changes(before[2], after[2], changes)
    elif op in ("and", "or", "imp", "iff", "xor"):
        alignment_changes(before[1], after[1], changes)
        alignment_changes(before[2], after[2], changes)


def has_positive_equality(formula, positive=True) -> bool:
    """True when some polarity still asserts an equality, so unique names would be unsound."""
    op = formula[0]
    if op == "atom":
        return bool(positive and formula[1] == "=")
    if op == "not":
        return has_positive_equality(formula[1], not positive)
    if op in ("and", "or"):
        return has_positive_equality(formula[1], positive) or has_positive_equality(formula[2], positive)
    if op == "imp":
        return has_positive_equality(formula[1], not positive) or has_positive_equality(formula[2], positive)
    if op in ("iff", "xor"):
        return any(has_positive_equality(side, pol) for side in formula[1:] for pol in (True, False))
    if op in ("all", "ex"):
        return has_positive_equality(formula[2], positive)
    return False


def constants_of(formulas):
    found = set()

    def walk_term(term):
        if isinstance(term, tuple):
            for arg in term[1:]:
                walk_term(arg)
        elif isinstance(term, str) and not term.startswith("?"):
            found.add(term)

    def on_atom(atom):
        for arg in atom[2]:
            walk_term(arg)

    for formula in formulas:
        _walk_formula(formula, on_atom)
    return found


class Encoder:
    """Translate the parser's tuple AST into Z3. Quantifiers use a fresh constant per binder."""

    def term(self, term, env):
        if isinstance(term, tuple):
            args = [self.term(arg, env) for arg in term[1:]]
            key = (term[0], len(args))
            if key not in _FUNCTIONS:
                _FUNCTIONS[key] = Function(
                    f"fn_{term[0]}_{len(args)}", *([ENTITY] * len(args)), ENTITY
                )
            return _FUNCTIONS[key](*args)
        if isinstance(term, str) and term.startswith("?"):
            if term not in env:
                env[term] = Const(term, ENTITY)
            return env[term]
        if term not in _CONSTANTS:
            _CONSTANTS[term] = Const(f"c_{term}", ENTITY)
        return _CONSTANTS[term]

    def formula(self, formula, env=None):
        if env is None:
            env = {}
        op = formula[0]
        if op == "atom":
            if formula[1] == "=":
                return self.term(formula[2][0], env) == self.term(formula[2][1], env)
            args = [self.term(arg, env) for arg in formula[2]]
            key = (formula[1], len(args))
            if key not in _PREDICATES:
                if args:
                    _PREDICATES[key] = Function(
                        f"p_{formula[1]}_{len(args)}", *([ENTITY] * len(args)), BoolSort()
                    )
                else:
                    _PREDICATES[key] = Bool(f"p_{formula[1]}_0")
            predicate = _PREDICATES[key]
            return predicate(*args) if args else predicate
        if op == "not":
            return Not(self.formula(formula[1], env))
        if op == "and":
            return And(self.formula(formula[1], env), self.formula(formula[2], env))
        if op == "or":
            return Or(self.formula(formula[1], env), self.formula(formula[2], env))
        if op == "imp":
            return Implies(self.formula(formula[1], env), self.formula(formula[2], env))
        if op == "iff":
            return self.formula(formula[1], env) == self.formula(formula[2], env)
        if op == "xor":
            return Xor(self.formula(formula[1], env), self.formula(formula[2], env))
        if op in ("all", "ex"):
            # A new constant per binder keeps nested uses of the same name from capturing.
            variable = FreshConst(ENTITY, prefix=formula[1])
            env2 = dict(env)
            env2["?" + formula[1]] = variable
            body = self.formula(formula[2], env2)
            return ForAll([variable], body) if op == "all" else Exists([variable], body)
        raise ValueError(f"unsupported operator {op}")


_PREMISE_CACHE = {}


def cached_premises(facts: str):
    """Parse each distinct premise block once. Queries that share a story reuse it."""
    key = str(facts)
    if key not in _PREMISE_CACHE:
        premises, repaired, errors = parse_premises(key)
        if not errors and premises:
            premises = tuple(bind_free(item, "all") for item in premises)
        _PREMISE_CACHE[key] = (premises, repaired, errors)
    return _PREMISE_CACHE[key]


def prepare(facts: str, query: str, align: bool = True):
    """Parse, repair, align, and close free variables. Shared by answering and tests."""
    premises, repaired, errors = cached_premises(facts)
    if errors or not premises:
        raise SyntaxError("; ".join(errors) or "no premises parsed")
    query_ast, query_fix = parse_sentence(str(query).strip())
    if query_fix is not None:
        repaired = repaired + (query_fix,)
    changes = []
    if align:
        aligned = align_formula(query_ast, symbol_table(premises))
        alignment_changes(query_ast, aligned, changes)
        query_ast = aligned
    query_ast = bind_free(query_ast, "ex")
    return premises, query_ast, tuple(changes), repaired


def answer(facts: str, query: str, timeout_ms: int = 1000, align: bool = True,
           unique_names: bool = True) -> Answer:
    """Entailment of one query. ``prediction`` is None when the result is not definite."""
    started = time.perf_counter()
    try:
        premises, query_ast, changes, repaired = prepare(facts, query, align=align)
    except Exception as exc:
        return Answer(
            prediction=None,
            status="parse_error",
            detail=str(exc),
            seconds=time.perf_counter() - started,
        )
    encoder = Encoder()
    solver = Solver()
    solver.set("timeout", int(timeout_ms))
    try:
        encoded = [encoder.formula(item) for item in premises]
        for item in encoded:
            solver.add(item)
        if unique_names and not any(has_positive_equality(item) for item in premises):
            names = sorted(constants_of(premises + (query_ast,)))
            if len(names) <= 60:
                constants = [encoder.term(name, {}) for name in names]
                for i, left in enumerate(constants):
                    for right in constants[i + 1:]:
                        solver.add(left != right)
        solver.add(Not(encoder.formula(query_ast)))
        result = solver.check()
    except Exception as exc:
        return Answer(
            prediction=None,
            status="encode_error",
            detail=str(exc),
            alignments=changes,
            repaired_lines=repaired,
            seconds=time.perf_counter() - started,
            parsed_premises=premises,
            parsed_query=query_ast,
        )
    prediction, status = interpret_check(result)
    detail = {
        "entailed": "KB and not Q is unsatisfiable",
        "not_entailed": "KB and not Q is satisfiable",
        "unknown": "solver returned unknown or timed out; not counted as False",
    }[status]
    return Answer(
        prediction=prediction,
        status=status,
        detail=detail,
        alignments=changes,
        repaired_lines=repaired,
        seconds=time.perf_counter() - started,
        parsed_premises=premises,
        parsed_query=query_ast,
    )


def answer_job(payload):
    """Picklable worker for process pools. ``payload`` is (facts, query, timeout_ms, align)."""
    facts, query, timeout_ms, align = payload
    result = answer(facts, query, timeout_ms=timeout_ms, align=align)
    return {
        "prediction": result.prediction,
        "status": result.status,
        "detail": result.detail,
        "alignments": result.alignments,
        "repaired_lines": result.repaired_lines,
        "seconds": result.seconds,
        "parsed_premises": result.parsed_premises,
        "parsed_query": result.parsed_query,
    }

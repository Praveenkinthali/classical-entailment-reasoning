"""Plain and fast resolution on the existing FOL parser and clausifier.

Fast resolution adds three optimizations to the same given-clause procedure:

1. Predicate indexing.
2. Unit subsumption.
3. Unit simplification.

A search that stops early, or that drops a clause for the size bound, is
unresolved. It is not reported as not-entailed.
"""
from __future__ import annotations

import heapq
import itertools
import time
from dataclasses import dataclass

from fol_engine_baseline import (
    apply,
    canon,
    depth,
    equality_axioms,
    factors,
    is_tautology,
    parse_formula,
    rename_apart,
    size,
    to_clauses,
    unify_args,
    weight,
)

MAX_LITS = 6
MAX_DEPTH = 3
MAX_WEIGHT = 40


@dataclass
class ReasonResult:
    prediction: bool | None
    status: str
    seconds: float
    detail: str = ""


def _variables(term, found: set):
    if isinstance(term, tuple):
        for arg in term[1:]:
            _variables(arg, found)
    elif isinstance(term, str) and term.startswith("?"):
        found.add(term)


def literal_subsumes(general, specific) -> bool:
    """True when the general literal is at least as general as the specific one."""
    if general[0] != specific[0] or general[1] != specific[1] or len(general[2]) != len(specific[2]):
        return False
    substitution = unify_args(general[2], specific[2], {})
    if substitution is None:
        return False
    specific_vars = set()
    for arg in specific[2]:
        _variables(arg, specific_vars)
    return not any(variable in specific_vars for variable in substitution)


def unit_subsumes(clause, units) -> bool:
    for literal in clause:
        for unit in units:
            if literal_subsumes(unit, literal):
                return True
    return False


def unit_simplify(clause, units):
    """Delete literals resolved against a unit and apply that substitution."""
    clause = list(clause)
    changed = True
    while changed and clause:
        changed = False
        for index, literal in enumerate(clause):
            complement = (not literal[0], literal[1], literal[2])
            for unit in units:
                if unit[0] != complement[0] or unit[1] != complement[1] or len(unit[2]) != len(complement[2]):
                    continue
                substitution = unify_args(unit[2], complement[2], {})
                if substitution is None:
                    continue
                clause = [
                    (sign, predicate, tuple(apply(arg, substitution) for arg in args))
                    for offset, (sign, predicate, args) in enumerate(clause)
                    if offset != index
                ]
                changed = True
                break
            if changed:
                break
    return clause


def prove(kb_clauses, goal_clauses, time_limit: float, fast: bool):
    """Return entailed, not_entailed, timeout, or incomplete."""
    started = time.perf_counter()
    seen = set()
    usable = []
    index = {}
    units = []
    queue = []
    clock = itertools.count()
    discarded = False

    def partners(predicate, sign):
        if fast:
            return index.get((predicate, not sign), ())
        return usable

    def remember(clause):
        usable.append(clause)
        if not fast:
            return
        for sign, predicate, _args in clause:
            index.setdefault((predicate, sign), []).append(clause)
        if len(clause) == 1 and clause[0] not in units:
            units.append(clause[0])

    def consider(raw):
        nonlocal discarded
        clause = unit_simplify(raw, units) if fast else list(raw)
        if not clause:
            return "refuted"
        normalized = canon(clause)
        if is_tautology(normalized):
            return None
        if fast and unit_subsumes(normalized, units):
            return None
        if normalized in seen:
            return None
        if len(normalized) > MAX_LITS or weight(normalized) > MAX_WEIGHT:
            discarded = True
            return None
        if any(depth(arg) > MAX_DEPTH for _sign, _predicate, args in normalized for arg in args):
            discarded = True
            return None
        seen.add(normalized)
        if fast and len(normalized) == 1 and normalized[0] not in units:
            units.append(normalized[0])
        heapq.heappush(queue, (weight(normalized), next(clock), normalized))
        return None

    for clause in kb_clauses:
        normalized = canon(clause)
        if normalized in seen or is_tautology(normalized):
            continue
        seen.add(normalized)
        remember(normalized)
    for clause in goal_clauses:
        if consider(clause) == "refuted":
            return "entailed", time.perf_counter() - started, discarded

    steps = 0
    while queue:
        if time.perf_counter() - started > time_limit:
            return "timeout", time.perf_counter() - started, discarded
        _weight, _order, given = heapq.heappop(queue)
        if fast and unit_subsumes(given, units) and len(given) > 1:
            continue
        steps += 1
        renamed = rename_apart(given, f"_g{steps}")
        for factor in factors(renamed):
            if consider(factor) == "refuted":
                return "entailed", time.perf_counter() - started, discarded
        remember(canon(given))
        for index_in_given, (sign, predicate, args) in enumerate(renamed):
            for other in partners(predicate, sign):
                renamed_other = rename_apart(other, f"_o{steps}")
                for other_sign, other_predicate, other_args in renamed_other:
                    if other_predicate != predicate or other_sign == sign or len(other_args) != len(args):
                        continue
                    substitution = unify_args(args, other_args, {})
                    if substitution is None:
                        continue
                    rest = [literal for offset, literal in enumerate(renamed) if offset != index_in_given]
                    rest.extend(
                        literal for literal in renamed_other
                        if literal != (other_sign, other_predicate, other_args)
                    )
                    resolvent = [
                        (lit_sign, lit_predicate, tuple(apply(arg, substitution) for arg in lit_args))
                        for lit_sign, lit_predicate, lit_args in rest
                    ]
                    if consider(resolvent) == "refuted":
                        return "entailed", time.perf_counter() - started, discarded
            if time.perf_counter() - started > time_limit:
                return "timeout", time.perf_counter() - started, discarded
    status = "incomplete" if discarded else "not_entailed"
    return status, time.perf_counter() - started, discarded


def _constants(clauses):
    found = set()

    def walk(term):
        if isinstance(term, tuple):
            for arg in term[1:]:
                walk(arg)
        elif not str(term).startswith("?"):
            found.add(term)

    for clause in clauses:
        for _sign, _predicate, args in clause:
            for arg in args:
                walk(arg)
    return found


def clausify_kb(formulas: list[str], unique_names: bool = False):
    clauses = []
    has_equality = False
    for formula in formulas:
        parsed = parse_formula(formula)
        converted = to_clauses(parsed)
        for clause in converted:
            if any(predicate == "=" and sign for sign, predicate, _args in clause):
                has_equality = True
        clauses.extend(converted)
    if has_equality:
        clauses = equality_axioms() + clauses
    constants = _constants(clauses)
    if unique_names and not has_equality and len(constants) <= 60:
        names = sorted(name for name in constants if not str(name).startswith("sk"))
        for left in range(len(names)):
            for right in range(left + 1, len(names)):
                clauses.append([(False, "=", (names[left], names[right]))])
    return clauses


def resolve_query(formulas: list[str], query: str, time_limit: float, fast: bool,
                  unique_names: bool = False) -> ReasonResult:
    started = time.perf_counter()
    try:
        clauses = clausify_kb(formulas, unique_names=unique_names)
        goal = to_clauses(("not", parse_formula(query)))
    except Exception as exc:
        return ReasonResult(None, "parse_error", time.perf_counter() - started, f"{type(exc).__name__}: {exc}")
    status, _elapsed, _discarded = prove(clauses, goal, time_limit, fast)
    seconds = time.perf_counter() - started
    if status == "entailed":
        return ReasonResult(True, status, seconds)
    if status == "not_entailed":
        return ReasonResult(False, status, seconds)
    return ReasonResult(None, status, seconds)


def correctness_tests(time_limit: float = 1.0) -> list[dict]:
    problems = [
        ("atom", ["P(a)"], "P(a)", True),
        ("unrelated", ["P(a)"], "Q(a)", False),
        ("modus ponens", ["P(a)", "P(a) → Q(a)"], "Q(a)", True),
        ("universal", ["∀x (P(x) → Q(x))", "P(a)"], "Q(a)", True),
        ("existential failure", ["P(a)"], "∀x P(x)", False),
    ]
    rows = []
    for name, premises, query, expected in problems:
        for fast in (False, True):
            result = resolve_query(premises, query, time_limit, fast)
            rows.append({
                "name": name,
                "mode": "fast" if fast else "plain",
                "expected": expected,
                "prediction": result.prediction,
                "status": result.status,
                "passed": result.prediction is expected and result.status in {"entailed", "not_entailed"},
            })
    return rows

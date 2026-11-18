# -*- coding: utf-8 -*-
"""Score a hidden / private test CSV with one full model at a time.

Each run uses only the model named by --model. Rows are not routed to the
other models. A row that model cannot read stays Unresolved and counts as
incorrect.

    folio         Z3 on the logic columns (FOLIO model)
    ruletaker     English facts and rules, forward chaining (RuleTaker model)
    counterbench  English story, twin network, fast resolution (CounterBench model)

Prints accuracy in [0, 1] and inference_time_seconds for every example.

Usage:
    python inference.py --model folio
    python inference.py --model ruletaker --input "../private_test - private_test.csv"
    python inference.py --model counterbench --output submission_test_counterbench.csv
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fol_engine import answer
from nl_to_fol_pipeline import (
    Translation,
    _formula_fol,
    _occur,
    _parse_causal,
    horn_entails,
    translate_counterbench,
    translate_ruletaker,
)
from resolution_engine import resolve_query

Z3_TIMEOUT_MS = 1000
RESOLUTION_LIMIT = 0.3

DEFAULT_INPUT = ROOT / "private_test - private_test.csv"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def gold_is_true(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "entailment"}


def prediction_name(value) -> str:
    if value is True:
        return "True"
    if value is False:
        return "False"
    return "Unresolved"


# ---------------------------------------------------------------------------
# CounterBench helpers
# ---------------------------------------------------------------------------

_POSITIVE_INTERVENTION = re.compile(
    r"Would ([A-Z][a-z]+) occur if ([A-Z][a-z]+) instead of not \2\??",
    re.IGNORECASE,
)


def translate_counterbench_positive(facts: str, query: str) -> Translation:
    """Same twin network as CounterBench, but the intervention sets the event true."""
    started = time.perf_counter()
    match = _POSITIVE_INTERVENTION.search(str(query))
    if match is None:
        return Translation(
            status="unsupported",
            detail="unrecognized positive counterfactual",
            family="counterbench",
            seconds=time.perf_counter() - started,
        )
    target, intervention = match.group(1), match.group(2)
    try:
        equations, mentioned, observations, _extra = _parse_causal(facts)
    except ValueError as exc:
        return Translation(
            status="parse_error",
            detail=str(exc),
            family="counterbench",
            seconds=time.perf_counter() - started,
        )
    lines = []
    interventions = [intervention]
    for name, node in equations.items():
        lines.append(f"{_occur('F', name)} ↔ {_formula_fol(node, 'F')}")
        if name not in interventions:
            lines.append(f"{_occur('C', name)} ↔ {_formula_fol(node, 'C')}")
    for name in sorted(mentioned - set(equations)):
        if name not in interventions:
            lines.append(f"{_occur('F', name)} ↔ {_occur('C', name)}")
    for name in interventions:
        lines.append(f"{_occur('C', name)}")
    for name, value in observations:
        atom = _occur("F", name)
        lines.append(atom if value else f"¬({atom})")
    goal = _occur("C", target)
    return Translation(
        tuple(lines),
        goal,
        "ok",
        f"positive_intervention={intervention}",
        "counterbench",
        time.perf_counter() - started,
    )


def reason_counterbench(context: str, statement: str):
    theory = translate_counterbench(context, statement)
    if theory.status != "ok":
        theory = translate_counterbench_positive(context, statement)
    if theory.status != "ok":
        return None, theory.status, theory.seconds, "counterbench"
    result = resolve_query(
        list(theory.premises),
        theory.query,
        RESOLUTION_LIMIT,
        True,
        unique_names=False,
    )
    return result.prediction, result.status, result.seconds, "counterbench"


# ---------------------------------------------------------------------------
# One full model on one example
# ---------------------------------------------------------------------------

def _text(row, name: str) -> str:
    if not hasattr(row, name) or pd.isna(getattr(row, name)):
        return ""
    return str(getattr(row, name))


def english_lines(text: str) -> str:
    """One sentence per line, which is the shape the RuleTaker model reads."""
    parts = re.split(r"(?<=[.!?])\s+", str(text).strip())
    return "\n".join(part.strip() for part in parts if part.strip())


def infer_folio(row) -> tuple[bool | None, str, float, str]:
    """FOLIO model: Z3 entailment on the supplied logic. English is not used."""
    facts = _text(row, "context_logic") or _text(row, "context")
    query = _text(row, "statement_logic") or _text(row, "statement")
    if not facts or not query:
        return None, "unsupported", 0.0, "folio"
    outcome = answer(
        facts,
        query,
        timeout_ms=Z3_TIMEOUT_MS,
        align=True,
        unique_names=True,
    )
    return outcome.prediction, outcome.status, outcome.seconds, "folio"


def infer_ruletaker(row) -> tuple[bool | None, str, float, str]:
    """RuleTaker model: English sentences to ground Horn clauses, then forward chaining."""
    context = _text(row, "context")
    statement = _text(row, "statement")
    if not context or not statement:
        return None, "unsupported", 0.0, "ruletaker"
    theory = translate_ruletaker(english_lines(context), statement)
    if theory.status != "ok":
        return None, theory.status, theory.seconds, "ruletaker"
    prediction, status, seconds = horn_entails(theory.premises, theory.query)
    return prediction, status, theory.seconds + seconds, "ruletaker"


def infer_counterbench(row) -> tuple[bool | None, str, float, str]:
    """CounterBench model: English story to a twin network, then fast resolution."""
    context = _text(row, "context")
    statement = _text(row, "statement")
    if not context or not statement:
        return None, "unsupported", 0.0, "counterbench"
    return reason_counterbench(context, statement)


MODELS = {
    "folio": infer_folio,
    "ruletaker": infer_ruletaker,
    "counterbench": infer_counterbench,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_table(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"context", "statement"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")
    if "id" not in frame.columns:
        frame.insert(0, "id", range(1, len(frame) + 1))
    if "label" not in frame.columns:
        raise ValueError(f"{path.name} needs a label column to compute accuracy")
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a hidden test CSV with one full model. Rows are not routed to the other models."
    )
    parser.add_argument(
        "--model",
        required=True,
        choices=sorted(MODELS),
        help="Which full model to run on every row",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="CSV with id, context, statement, label, and optional logic columns",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Where to write id,flag predictions. Default: submission_test_<model>.csv",
    )
    args = parser.parse_args()
    if args.output is None:
        args.output = HERE / f"submission_test_{args.model}.csv"
    infer = MODELS[args.model]

    input_path = args.input
    if not input_path.is_file():
        alt = HERE / input_path.name
        if alt.is_file():
            input_path = alt
        else:
            raise FileNotFoundError(
                f"Input file not found: {args.input}\n"
                f"Pass --input with the full path to the 100-sample CSV."
            )

    data = load_table(input_path)
    rows = []
    started = time.perf_counter()
    for row in data.itertuples(index=False):
        prediction, status, seconds, method = infer(row)
        rows.append(
            {
                "id": row.id,
                "label": row.label,
                "prediction": prediction,
                "status": status,
                "seconds": seconds,
                "method": method,
            }
        )
    elapsed = time.perf_counter() - started
    result = pd.DataFrame(rows)

    gold = result["label"].map(gold_is_true)
    pred = result["prediction"]
    correct = int(((pred == gold) & pred.notna()).sum())
    total = len(result)
    accuracy = correct / total if total else 0.0

    submission = pd.DataFrame(
        {
            "id": result["id"],
            "flag": result["prediction"].map(prediction_name),
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    submission.to_csv(args.output, index=False)

    print(f"model={args.model}")
    print(f"input_path={input_path}")
    print(f"examples={total}")
    print(f"accuracy={accuracy:.4f}")
    print(f"inference_time_seconds={elapsed:.4f}")
    print(f"correct={correct}/{total}")
    print(f"unresolved={int(pred.isna().sum())}")
    print("methods")
    print(result["method"].value_counts().to_string())
    print("statuses")
    print(result["status"].value_counts().to_string())
    print(f"wrote={args.output}")


if __name__ == "__main__":
    main()

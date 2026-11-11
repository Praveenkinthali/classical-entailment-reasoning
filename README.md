# Classical Entailment for RuleTaker, CounterBench, and FOLIO

Decides whether a query follows from a knowledge base.

The answer is True when the query is entailed, and False when it is not. If the solver does not settle the query, the stored prediction is Unresolved. Unresolved is not treated as False.

Final True/False decisions come from classical procedures: forward chaining, fast resolution, and Z3. They do not come from a generative model.

The measured scores from the saved notebook run are in [FINAL_PERFORMANCE_REPORT.md](FINAL_PERFORMANCE_REPORT.md). A written description of the method is also in `Report.pdf`.

## Files in this folder

| File | Role |
|---|---|
| `reasoning_agent.ipynb` | Scores a logic test file and, when asked, scores the public datasets and rewrites `submission.csv` |
| `submission.csv` | Predictions on the public datasets. Columns: `dataset`, `sample_no`, `prediction`. 4840 rows |
| `Report.pdf` | Method description and public-dataset results |
| `README.txt` | Original plain-text notes for this folder |
| `datasets_local/` | RuleTaker splits and the original FOLIO JSONL files |
| `datasets_unified/` | CounterBench, FOLIO logic files, and `engine_evaluation_results.csv` |

`submission_test.csv` is created when the notebook is run on a test file. It is not included here.

## Modules the notebook imports

`reasoning_agent.ipynb` imports these modules from the first parent folder that contains `fol_engine.py`:

- `fol_engine.py` — Z3 entailment
- `fol_engine_baseline.py` — parser used by the Z3 engine
- `nl_to_fol_pipeline.py` — RuleTaker and CounterBench translation, forward chaining
- `resolution_engine.py` — fast resolution

Those four files are not stored in this folder. The saved notebook output records the project directory as the parent `Assignment` folder. This repository currently contains the notebook, datasets, predictions, and report that were in the opened folder.

## Requirements

Python 3.9 or newer:

```bash
pip install pandas matplotlib z3-solver
```

The public datasets used by the notebook are already stored in `datasets_unified` and `datasets_local`. Reproducing the reported results does not need a Hugging Face login.

## How to run a test file

The test file contains logic directly. No English translation is used.

1. Put the test file in the same folder as the notebook.
2. In the Configuration cell set:
   - `INPUT_CSV = "hidden_test.csv"` (name of the test file)
   - `RECHECK_RESULTS = False`
3. Run all cells.

The hidden-test cell prints the number of examples, the total inference time, the accuracy when the file has labels, and the TP / FN / FP / TN counts. It writes the predictions to `submission_test.csv`.

The decision is made by `predict_logic(context, statement)`. It sends the formulas to Z3. If the premises together with the negation of the statement are unsatisfiable, the statement follows and the answer is True. If they are satisfiable, the answer is False. If Z3 returns unknown, or the formula does not parse, the prediction stays Unresolved.

## Input format

A CSV file with the columns:

- `id` — example id
- `context` — the knowledge base, one formula per line
- `statement` — the query (one formula)
- `label` — optional: True / False. Anything else, such as Uncertain, counts as False

`submission_test.csv` has the columns `id` and `flag`. `flag` is True, False, or Unresolved.

## Logic syntax

| Meaning | Forms |
|---|---|
| not | `not`, `¬`, `~` |
| and | `and`, `∧`, `&` |
| or | `or`, `∨`, `\|` |
| exclusive or | `⊕`, `xor` |
| implies | `→`, `->` |
| if and only if | `↔`, `<->` |
| for all | `∀x`, `forall x` |
| there exists | `∃x`, `exists x` |
| equality | `=`, `≠` |

Predicates are written `Name(arg1, arg2)`. A single name such as `P` is a proposition. Quantified names are variables. Distinct constants are treated as different names when the formulas contain no positive equality.

## Reproducing the reported results

1. `pip install pandas matplotlib z3-solver`
2. Place the four reasoning modules where the notebook can find them (this folder, or a parent folder).
3. Set `RECHECK_RESULTS = True` in the Configuration cell and run all cells. This scores the local public files and writes `submission.csv`.
4. Set `RECHECK_RESULTS = False` again afterwards.

What runs when `RECHECK_RESULTS` is True:

- **RuleTaker.** English facts and rules from `datasets_local/ruletaker_train.csv`, `ruletaker_test.csv`, and `ruletaker_ood_test.csv`. The text is turned into ground Horn clauses and tested by forward chaining. 1625 examples.
- **CounterBench.** English stories from `datasets_unified/counterbench_unified.csv`. Each story becomes a twin network: a factual world and a counterfactual world. An intervention deletes the structural equation of the intervened event and sets that event false in the counterfactual world. Fast resolution tests the query.
- **FOLIO, Uncertain removed.** The supplied formulas in `datasets_unified/folio_fol_unified.csv`. Z3 is called the same way as the hidden-test inference, with predicate alignment because some FOLIO predicates are spelled in more than one way. 811 examples.
- **FOLIO, Uncertain counted as False.** `datasets_local/folio_train.jsonl` and `folio_validation.jsonl`. The formulas are used as given. The source files are not rewritten. Entailment and True become True. Contradiction, False, and Uncertain become False. A solver unknown stays Unresolved. 1204 examples.

Accuracy is the same on every run. The times change slightly from run to run and depend on the machine. See `FINAL_PERFORMANCE_REPORT.md` for the numbers saved from the notebook execution.

## Datasets

- RuleTaker: https://huggingface.co/datasets/jise/ruletaker
- CounterBench: https://huggingface.co/datasets/CounterBench/CounterBench
- FOLIO: https://huggingface.co/datasets/yale-nlp/FOLIO

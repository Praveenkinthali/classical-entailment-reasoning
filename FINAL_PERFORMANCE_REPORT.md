# Final performance report

Classical entailment for RuleTaker, CounterBench, and FOLIO.

These figures are copied from the saved cell outputs in `reasoning_agent.ipynb` (the run that wrote `submission.csv`, 4840 rows). They are not a new measurement. Accuracy counts are stable across the notebook, `README.txt`, and `Report.pdf`. Reasoning times differ slightly between those write-ups because they were measured on different runs. The times below are the ones printed by the saved notebook execution.

Unresolved predictions are stored as Unresolved. They are left out of the evaluated-accuracy denominator and they count as incorrect in the operational accuracy.

## Headline results

| Dataset | Samples | Method | Operational accuracy | Evaluated accuracy | Coverage | Unresolved | Reasoning time (s/sample) |
|---|---:|---|---:|---:|---:|---:|---|
| RuleTaker | 1625 | forward chaining | 98.46% (1600/1625) | 98.46% (1600/1625) | 100.00% | 0 | 0.000015 ± 0.000006 |
| CounterBench | 1200 | fast resolution on the twin network | 94.58% (1135/1200) | 94.58% (1135/1200) | 100.00% | 0 | 0.0027 ± 0.0065 |
| FOLIO, Uncertain removed | 811 | Z3 on the supplied logic | 76.82% (623/811) | 77.01% (623/809) | 99.75% | 2 | 0.0247 ± 0.0586 |
| FOLIO, Uncertain counted as False | 1204 | Z3 on the supplied logic | 82.48% (993/1204) | 82.68% (993/1201) | 99.75% | 3 | 0.0320 ± 0.0623 |

Combined operational accuracy, with FOLIO Uncertain rows removed: **92.35% (3358/3636)**.

Combined operational accuracy, with FOLIO Uncertain counted as False: **92.53% (3728/4029)**.

## RuleTaker splits

English facts and rules become Horn clauses. Forward chaining derives the facts the rules produce and checks whether the query is among them.

| Split | Examples | Correct | Accuracy |
|---|---:|---:|---:|
| train | 1000 | 983 | 98.30% |
| test | 250 | 244 | 97.60% |
| ood_test | 375 | 373 | 99.47% |
| All | 1625 | 1600 | 98.46% |

## CounterBench

Each example becomes a factual world and a counterfactual world. An intervention removes the structural equation of the intervened event and sets that event to false in the counterfactual world. Fast resolution then tests the query.

| Status | Count |
|---|---:|
| entailed | 570 |
| not_entailed | 630 |

Operational accuracy on all 1200 examples: 94.58% (1135/1200).

## FOLIO, Uncertain removed

The logic shipped with FOLIO is used as given. Z3 is the same solver as `predict_logic`, with predicate alignment turned on. This block keeps only the original True and False labels.

| Split | Examples | Correct | Accuracy |
|---|---:|---:|---:|
| train | 677 | 514 | 75.92% |
| validation | 134 | 109 | 81.34% |
| All | 811 | 623 | 76.82% |

| Status | Count |
|---|---:|
| entailed | 325 |
| not_entailed | 484 |
| unknown | 2 |

## FOLIO, Uncertain counted as False

Every original row is kept. Entailment and True become True. Contradiction, False, and Uncertain become False. A solver unknown stays Unresolved.

Original labels in this run: True 460, Uncertain 393, False 351.

| Split | Examples | Correct | Accuracy |
|---|---:|---:|---:|
| train | 1001 | 818 | 81.72% |
| validation | 203 | 175 | 86.21% |
| All | 1204 | 993 | 82.48% |

| Status | Count |
|---|---:|
| entailed | 347 |
| not_entailed | 854 |
| unknown | 3 |

The binary subset inside this run (Uncertain removed) again scores 76.82% (623/811).

| Data | Uncertain handling | Examples | Correct | Accuracy |
|---|---|---:|---:|---:|
| FOLIO train | removed | 677 | 514 | 75.92% |
| FOLIO train | counted as False | 1001 | 818 | 81.72% |
| FOLIO validation | removed | 134 | 109 | 81.34% |
| FOLIO validation | counted as False | 203 | 175 | 86.21% |
| FOLIO all | removed | 811 | 623 | 76.82% |
| FOLIO all | counted as False | 1204 | 993 | 82.48% |
| All three datasets | removed | 3636 | 3358 | 92.35% |
| All three datasets | counted as False | 4029 | 3728 | 92.53% |

## Predictions written to submission.csv

4840 rows: RuleTaker 1625, CounterBench 1200, FOLIO with Uncertain removed 811, FOLIO with Uncertain counted as False 1204.

| Dataset | False | True | Unresolved |
|---|---:|---:|---:|
| RuleTaker | 719 | 906 | 0 |
| CounterBench | 630 | 570 | 0 |
| FOLIO FOL | 484 | 325 | 2 |
| FOLIO FOL uncertain as False | 854 | 347 | 3 |

## Separate file: engine_evaluation_results.csv

`datasets_unified/engine_evaluation_results.csv` is an earlier recorded evaluation. Its accuracies are not the notebook scores above. They are listed here so that file is not confused with the final run.

| Dataset file | Examples | Accuracy | Macro F1 | Seconds per example |
|---|---:|---:|---:|---:|
| ruletaker_unified.csv | 1625 | 88.74% | 0.887 | 0.000728 |
| folio_unified.csv | 811 | 43.40% | 0.304 | 0.000367 |
| folio_fol_unified.csv | 811 | 71.39% | 0.711 | 0.005436 |
| counterbench_unified.csv | 1200 | 50.42% | 0.335 | 0.000167 |

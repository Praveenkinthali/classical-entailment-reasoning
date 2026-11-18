# Private Test Evaluation

This note explains, in detail:

1. whether `inference.py` evaluates using **natural-language sentences** or **logic**
2. which columns are actually fed to the solver for each kind of example
3. how the final **accuracy** in `[0, 1]` is obtained from those predictions

The private-test entry point is:

```bash
python inference.py --input "../private_test - private_test.csv"
```

---

## 1. What is inside one private-test row?

Each of the **100** rows has both English and logic for the same question:

| Column | Kind of content | Example |
|---|---|---|
| `id` | identifier | `P001` |
| `context` | **natural language** premises / story | `Odra is shiny. Odra is swift. ...` |
| `statement` | **natural language** query | `Odra is brave.` |
| `label` | gold True / False | `True` |
| `context_logic` | **logic** premises | FOL, Lisp atoms, or causal equations |
| `statement_logic` | **logic** query | FOL atom, Lisp atom, or `Fesh_{Jask=0} = 1 ?` |

Important point:

- The file gives you **both** natural sentences and logic.
- The code does **not** always use both.
- For each row, `inference.py` looks at the shape of `context_logic` / `statement` and chooses **one route**.
- That route decides whether the solver sees:
  - the **logic columns**, or
  - the **English columns** (translated into logic first).

The gold `label` is **never** used to choose the method or to change the formulas. It is used only at the end, when accuracy is computed.

---

## 2. Does the code evaluate with logic or with natural sentences?

**Short answer:** the classical solver always reasons over **logic**.  
Natural sentences are used only when the row is a CounterBench story, and even then they are first translated into logical formulas.

So the evaluation path is always:

```text
input text (English or logic columns)
        |
        v
   logical formulas
        |
        v
 classical entailment check  -->  True / False / Unresolved
        |
        v
 compare with gold label     -->  correct or incorrect
```

There is no generative model that reads English and directly guesses True/False.

### Which columns are used for which rows?

| Row type | About how many | Input used by the code | What happens |
|---|---:|---|---|
| FOLIO-style FOL | 67 | **`context_logic` + `statement_logic`** | Logic is used as given. English is ignored for the solver. |
| RuleTaker Lisp | 20 | **`context_logic` + `statement_logic`** | Lisp logic is converted to FOL, then solved. English is ignored for the solver. |
| CounterBench | 13 | **`context` + `statement` (English)** | English is translated into a twin-network theory, then solved. The equation-style logic columns are used only to detect that the row is CounterBench. |

So:

- For **most rows (87/100)**, evaluation is done from the **logic columns**.
- For **CounterBench rows (13/100)**, evaluation starts from the **natural-language sentences**, because that is how the CounterBench twin-network translator in the reasoning agent works.

---

## 3. Detailed path for each kind of example

### 3.1 FOL rows: use logic directly

**Detection:** `context_logic` already contains FOL symbols such as `∀`, `∃`, `∧`, `∨`, `→`, `¬`, `⊕`.

**Example**

Natural sentences (present in the file, but **not** sent to the solver):

```text
context:   All paintings in the Varlo gallery are oil paintings. ...
statement: Morning Bell is fragile.
```

Logic that **is** sent to the solver:

```text
context_logic:
  ∀x (Painting(x) ∧ In(x, varloGallery) → OilPainting(x))
  ∀x (OilPainting(x) → Fragile(x))
  ...
  BronzeSculpture(morningBell)

statement_logic:
  Fragile(morningBell)
```

**Code path**

```text
context_logic, statement_logic
        |
        v
fol_engine.answer(...)   # Z3
        |
        v
prediction True / False / Unresolved
```

Z3 checks:

```text
Does KB entail Query?
  <=>  (KB  and  not Query) is unsatisfiable
```

- unsat → predict **True**
- sat → predict **False**
- unknown / timeout → **Unresolved**

Here the evaluation is **logic-only**.

---

### 3.2 RuleTaker Lisp rows: use logic, after conversion

**Detection:** `context_logic` contains Lisp-like atoms such as `("Odra" "is" "shiny" "+")`.

**Example**

Natural sentences (again present, but **not** used by this route):

```text
context:   Odra is shiny. Odra is swift. ...
statement: Odra is brave.
```

Logic columns used by the code:

```text
context_logic:
  ("Odra" "is" "shiny" "+")
  ("Odra" "is" "swift" "+")
  (("someone" "is" "cold" "+") ("someone" "is" "swift" "+")) -> ("someone" "is" "brave" "+")
  (("something" "is" "swift" "+")) -> ("something" "is" "cold" "+")
  ...

statement_logic:
  ("Odra" "is" "brave" "+")
```

**Conversion step** (still before the solver):

```text
("Odra" "is" "swift" "+")
    ->  Swift(Odra)

(("something" "is" "swift" "+")) -> ("something" "is" "cold" "+")
    ->  ∀x (Swift(x) → Cold(x))

(("someone" "is" "cold" "+") ("someone" "is" "swift" "+")) -> ("someone" "is" "brave" "+")
    ->  ∀x (Cold(x) ∧ Swift(x) → Brave(x))

("Odra" "is" "brave" "+")
    ->  Brave(Odra)
```

**Code path**

```text
context_logic / statement_logic
        |
        v
Lisp -> FOL converter
        |
        v
fol_engine.answer(...)   # Z3
        |
        v
prediction True / False / Unresolved
```

Why not use the English here?

- The English is RuleTaker-like, but many rules are quantified (`someone` / `something`) and have several body atoms.
- The notebook's ground Horn forward-chaining path does not handle those variables.
- The Lisp logic already encodes the intended rules, so converting that logic to FOL and calling Z3 is the faithful route.

Here the evaluation is still **logic-based**, not free English reading.

---

### 3.3 CounterBench rows: start from natural sentences

**Detection:** the statement looks like a counterfactual question, or `context_logic` contains causal equations / `Evidence:`.

**Example**

```text
context:
  We know that Vapo causes Toln, Drav causes Toln, ... We observed Drav. ...

statement:
  Would Fesh occur if not Jask instead of Jask?

context_logic:     # used to recognize CounterBench, not fed raw to Z3
  Toln = Vapo OR Drav
  Jask = Toln
  ...
  Evidence: Drav = 1, Vapo = 0

statement_logic:
  Fesh_{Jask=0} = 1 ?
```

**What the code actually uses for reasoning**

```text
English context + English statement
        |
        v
translate_counterbench(...)
        |
        v
logical twin-network formulas
  OccurF(...), OccurC(...), biconditional equations, intervention
        |
        v
fast resolution
        |
        v
prediction True / False / Unresolved
```

So for CounterBench:

1. evaluation **starts from natural-language sentences**
2. those sentences are **translated into logic** by the same twin-network translator used in the reasoning agent
3. the final True/False decision is still made by a **classical** procedure (fast resolution)

A few private-test questions use the positive form:

```text
Would Quor occur if Fesh instead of not Fesh?
```

That means “force Fesh to be true in the counterfactual world”.  
`inference.py` handles that with the same twin-network idea, setting the intervened event true instead of false.

---

## 4. End-to-end picture: from row to prediction

```text
                    private_test CSV (100 rows)
                               |
                               v
                      for each row:
                               |
         +---------------------+---------------------+
         |                     |                     |
   CounterBench?          Lisp logic?            FOL logic?
         |                     |                     |
   use English           use logic cols         use logic cols
   context/statement     context_logic/         context_logic/
         |               statement_logic        statement_logic
         v                     |                     |
  translate to twin            v                     |
  network formulas        convert Lisp             |
         |                  to FOL                 |
         v                     v                     v
   fast resolution              Z3 entailment check
         |                     |                     |
         +----------+----------+----------+----------+
                    |
                    v
           prediction for that row
           True / False / Unresolved
```

After all 100 predictions are collected, accuracy is computed against `label`.

---

## 5. How accuracy is calculated

### Formula

```text
accuracy = correct / total
```

with:

```text
total   = number of rows in the file          # here, 100
correct = number of rows where the prediction matches the gold label
accuracy is reported in [0, 1], not as a percentage
```

### Step by step

For every row `i = 1 ... 100`:

1. **Build the gold Boolean** from the `label` column:

   ```text
   gold = True   if label is True / true / 1 / yes / entailment
   gold = False  otherwise
   ```

   On this private file the labels are already Boolean True/False, 50 each.

2. **Get the model prediction** from the route above:

   ```text
   prediction = True, False, or None
   ```

   `None` means unresolved (solver unknown, parse failure, unsupported question, etc.).

3. **Score that row:**

   ```text
   correct_i = 1  if prediction is not None and prediction == gold
   correct_i = 0  otherwise
   ```

4. **Aggregate:**

   ```text
   correct  = sum(correct_i)
   accuracy = correct / 100
   ```

### Concrete scoring examples

| Gold label | Model prediction | Counts as correct? | Why |
|---|---|---|---|
| True | True | yes | match |
| False | False | yes | match |
| True | False | no | wrong answer |
| False | True | no | wrong answer |
| True | Unresolved (`None`) | no | not a settled correct answer |
| False | Unresolved (`None`) | no | not a settled correct answer |

### What accuracy is **not**

- It is **not** computed only on easy rows.
- It is **not** computed only on rows that the solver finished.
- Unresolved answers are **not** rewritten as False before scoring.
- The English text is **not** compared as strings to the label. Only the classical True/False decision is compared.

### Latest private-test result

```text
total      = 100
correct    = 92
unresolved = 0
accuracy   = 92 / 100 = 0.9200
```

Method mix that produced those 100 predictions:

| Method | Rows | Input used |
|---|---:|---|
| `folio_z3` | 67 | logic columns |
| `ruletaker_lisp_z3` | 20 | logic columns (converted) |
| `counterbench` | 13 | natural sentences → logic |

---

## 6. How inference time is calculated

```text
inference_time_seconds = wall-clock seconds for the whole 100-example loop
```

In code:

```python
started = time.perf_counter()
for row in data:
    prediction = infer_row(row)
elapsed = time.perf_counter() - started
```

This total includes, for every example:

- detecting the row type
- Lisp-to-FOL conversion or CounterBench translation, when used
- Z3 or fast-resolution time

It is one number for **all 100 samples together**, in seconds.  
It is not a per-sample mean.

Latest run:

```text
inference_time_seconds ≈ 0.63
```

---

## 7. Why this design matches the reasoning agent

The private file mixes three public-dataset styles. The reasoning agent already has one classical method for each style:

| Style in private test | Same idea as in `reasoning_agent.ipynb` | Uses natural sentences? | Uses logic? |
|---|---|---|---|
| FOLIO-like formulas | FOLIO FOL → Z3 | no, for the solver | yes, directly |
| RuleTaker-like rules | RuleTaker idea, but via Lisp→FOL→Z3 because rules are quantified | no, for the solver | yes, after conversion |
| CounterBench questions | CounterBench twin network + fast resolution | yes, as the starting point | yes, after translation |

So accuracy `0.9200` means:

```text
Out of 100 private examples,
92 times the chosen classical method returned the same True/False answer as the gold label.
```

Those answers came from logic entailment checks. Natural sentences were used only as the source text for the CounterBench subset, and only after being translated into logic.

---

## 8. How to reproduce

```bash
cd AI_ASSIGNMENT_M26CSA003
python inference.py --input "../private_test - private_test.csv"
```

You should see lines of the form:

```text
examples=100
accuracy=0.9200
inference_time_seconds=0.6285
correct=92/100
unresolved=0
```

Predictions are also written to `submission_test.csv` with columns `id,flag`.

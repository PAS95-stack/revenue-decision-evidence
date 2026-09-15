# Revenue Decision Evidence

A deterministic workflow that reads three CSV exports — advertising spend, CRM
leads and revenue transactions — and reports what those exports can and cannot
support about a channel-budget decision.

The included data are synthetic. This repository is a technical demonstration.
It is not a customer case study, a real-data deployment, an external evaluation
or evidence of paid validation.

## What it does

- **Gives every input row one status:** `accepted`, `accepted-unattributed`,
  `rejected`, `duplicate` or `conflict`, with a reason. Nothing is repaired
  silently.
- **Traces every figure to the rows it is built from.** Each figure carries an
  evidence ID, and `lineage.csv` records whether each row adds to it, forms the
  numerator or denominator of a ratio, or only links records together.
- **Handles duplicates and conflicts without depending on row order.**
  Identical rows count once; rows that share an identifier but disagree are all
  excluded and shown.
- **Never recommends moving budget.** The report lists data-quality findings and
  the questions that need answers, and always states: *Do not reallocate budget
  from this evidence set alone.*
- **Publishes only what an independent check reproduces.**
  `src/revenue_evidence/reconstruct.py` re-derives every row status and figure
  from the raw CSVs without using the engine's code. If it disagrees, or an
  export has no usable rows, only `evaluation_results.json` is written and the
  command exits with code 2.
- **Optional AI narrative, off by default.** The model receives computed figures
  only. Every number it writes must equal the value of the evidence it cites in
  the same sentence, or the narrative is discarded. The application, not the
  model, states the decision-rights boundary.

## Quick start

Requires Python 3.11 or later. No third-party packages.

```bash
python3 -m unittest discover -s tests -v
```

```bash
PYTHONPATH=src python3 -m revenue_evidence.cli \
  --ads data/synthetic/ads.csv \
  --crm data/synthetic/crm.csv \
  --revenue data/synthetic/revenue.csv \
  --output outputs/synthetic-case
```

Check any published output folder independently. `-I` runs the checker in
isolated mode, where the engine package cannot be imported:

```bash
python3 -I src/revenue_evidence/reconstruct.py \
  --ads data/synthetic/ads.csv \
  --crm data/synthetic/crm.csv \
  --revenue data/synthetic/revenue.csv \
  --output outputs/synthetic-case
```

Optional: `--as-of YYYY-MM-DD` records a reporting date. The clock is never
read, so identical inputs produce byte-identical outputs.

## Walkthrough

```bash
scripts/demo.sh --no-pause
```

Runs from the original CSVs, shows every row status, rebuilds figures from their
raw rows, feeds the narrative gate a false revenue figure, tampers with a
published report, reorders contradictory rows and removes all revenue rows. Each
step checks its expected result and the script stops if one is wrong. Omit
`--no-pause` to step through it; the spoken guide is in
[`docs/demo-script.md`](docs/demo-script.md).

## Outputs

| File | Contents |
|---|---|
| `report.json` | Every figure, its lineage, row statuses, rule set, findings and questions |
| `row_dispositions.csv` | Every input row: status, reason, amount and the figures it supports |
| `lineage.csv` | Figure-to-row traceability with each row's role |
| `rejected_records.csv` | Every row that is not plainly accepted, with its reason |
| `executive_brief.md` | Readable brief citing an evidence ID beside each figure |
| `report.html` | The same evidence as a web page |
| `evaluation_results.json` | Checker result, the checks run, and a SHA-256 hash of each published file |

## Synthetic result

From `data/synthetic`: accepted spend AED 15,000.00, accepted revenue AED
56,500.00, attributed revenue AED 32,000.00, attribution coverage 56.6%, and
AED 24,500.00 of revenue that could not be assigned to a channel, of which AED
9,000.00 belongs to a lead with contradictory CRM records. Of 24 input rows,
13 are accepted, 4 are accepted but unattributed, 3 are rejected, 2 are
duplicates and 2 are in conflict.

## Status

| Evidence level | Status |
|---|---|
| Synthetic reproducibility | Implemented; checked by tests and CI |
| Permissioned real-data use | Not achieved |
| Written external evaluation | Not achieved |
| Paid validation | Not achieved |

## Documentation

- [`docs/limitations.md`](docs/limitations.md): what this does not show, known
  limitations, and the design decisions behind the behaviour
- [`docs/evaluation-cases.md`](docs/evaluation-cases.md): the test suite and its
  results
- [`docs/data-dictionary.md`](docs/data-dictionary.md): the input contract, row
  statuses and output fields
- [`docs/architecture.md`](docs/architecture.md): how a run flows from exports to
  publication
- [`docs/governance.md`](docs/governance.md): decision rights and data controls

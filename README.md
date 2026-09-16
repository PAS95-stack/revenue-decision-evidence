# Revenue Decision Evidence

A deterministic workflow that reads advertising spend, CRM leads and revenue
exports and reports what those exports can and cannot
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
- **Reads real exports through a declared engagement config.** Column names,
  separators and encodings (including semicolon-separated Windows-1252 files),
  several date formats within one file, a total computed from quantity and unit
  price, a currency named per row, time-zone shifts, ad accounts in other
  currencies, refunds and credit notes, revenue that names customers rather than
  leads, and attribution windows are declared per engagement, never guessed.
  `propose` drafts the config from the exports themselves and marks what only a
  person can decide. See [`docs/engagements.md`](docs/engagements.md).
- **Publishes only what an independent check reproduces.**
  `src/revenue_evidence/reconstruct.py` re-derives every row status and figure
  from the raw CSVs without using the engine's code. If it disagrees, or an
  export has no usable rows, only `evaluation_results.json` is written and the
  command exits with code 2.
- **Optional AI narrative, off by default.** The model receives computed figures
  only. Every number it writes must equal the value of the evidence cited
  directly after it, be described as that evidence's metric, and name its
  channel or window, or the narrative is discarded. The application, not the
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

## Real client exports

Each engagement lives in its own folder outside this repository:

```bash
python3 scripts/engagement.py init ~/engagements/client-2026-09 --name "Client, September review"
```

```bash
python3 scripts/engagement.py intake ~/engagements/client-2026-09 --received-on 2026-09-16 --received-from "Finance"
```

The config is drafted from the exports, with the evidence for every choice and
**needs you** beside anything the data cannot settle — an exchange rate, a
channel with no column, a date that reads both ways. Check it, then rename the
draft to `engagement.json`:

```bash
python3 scripts/engagement.py propose ~/engagements/client-2026-09
```

```bash
python3 scripts/engagement.py run ~/engagements/client-2026-09
```

The worked example reads Meta, Google Ads, HubSpot and Xero-shaped files:

```bash
PYTHONPATH=src python3 -m revenue_evidence.cli --config examples/messy-exports/engagement.json --output /tmp/example
```

Spreadsheets convert first, keeping the sheet's row numbers and recording the
workbook's SHA-256:

```bash
python3 scripts/xlsx_to_csv.py Orders.xlsx --sheet Invoices --out ~/engagements/client-2026-09/inputs/invoices.csv
```

The full procedure, the config reference and what a config cannot express are in
[`docs/engagements.md`](docs/engagements.md).

## Outputs

| File | Contents |
|---|---|
| `report.json` | Every figure, its lineage, row statuses, rule set, findings and questions |
| `row_dispositions.csv` | Every input row: status, reason, amount and the figures it supports |
| `lineage.csv` | Figure-to-row traceability with each row's role |
| `rejected_records.csv` | Every row that is not plainly accepted, with its reason |
| `exceptions.csv` | Rows that do not count in full, grouped by reason, with the amount they hold and who can fix them |
| `executive_brief.md` | Readable brief citing an evidence ID beside each figure |
| `report.html` | The same evidence as a web page |
| `evaluation_results.json` | Checker result, the checks run, and a SHA-256 hash of each published file |

`report.json` repeats what the CSV files hold. Above 50,000 input rows or 200,000
lineage entries it states how many of each are in the CSVs instead of repeating
them, so it stays readable; the checker requires the same rule, so detail cannot
go missing from a report small enough to hold it.

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
| Public-data rehearsal | Run on 25,900 UCI Online Retail invoices with a synthetic CRM and advertising overlay; see `docs/engagements.md` |
| Permissioned real-data use | Not achieved |
| Written external evaluation | Not achieved |
| Paid validation | Not achieved |

## Documentation

- [`docs/engagements.md`](docs/engagements.md): running on a client's exports, the
  engagement config reference and the public-data rehearsal
- [`docs/limitations.md`](docs/limitations.md): what this does not show, known
  limitations, and the design decisions behind the behaviour
- [`docs/evaluation-cases.md`](docs/evaluation-cases.md): the test suite and its
  results
- [`docs/data-dictionary.md`](docs/data-dictionary.md): the input contract, row
  statuses and output fields
- [`docs/architecture.md`](docs/architecture.md): how a run flows from exports to
  publication
- [`docs/governance.md`](docs/governance.md): decision rights and data controls

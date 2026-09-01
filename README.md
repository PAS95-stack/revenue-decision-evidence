# Revenue Decision Evidence Case

Public, reproducible evidence for the **PAS95 Revenue Decision Evidence Sprint**.

This repository is a deterministic decision-evidence workflow, not a customer case study and not proof of commercial validation. The included data are synthetic. As of 2 September 2026, PAS95 has zero customers, zero paid validation, zero real-data deployments, zero external references, and no active invoice capability.

## What it does

It accepts no more than three approved CSV exports:

1. advertising spend;
2. CRM leads/opportunities; and
3. revenue transactions.

It validates and reconciles those files, separates accepted/rejected/uncertain records, traces reported figures to source rows, grades evidence, tests attribution-window sensitivity, and produces one bounded recommendation that requires named human approval.

The optional Azure AI layer receives computed evidence records only. It never receives raw input rows, never calculates authoritative figures, and is disabled unless its output cites valid evidence IDs and passes deterministic validation.

## Quick start

Requires Python 3.11 or later and no third-party runtime dependencies.

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m revenue_evidence.cli \
  --ads data/synthetic/ads.csv \
  --crm data/synthetic/crm.csv \
  --revenue data/synthetic/revenue.csv \
  --output outputs/synthetic-case
```

Then open `outputs/synthetic-case/report.html`.

## Inspectable outputs

- `report.json`: authoritative computed evidence record;
- `lineage.csv`: claim-to-source-row traceability;
- `rejected_records.csv`: rejected and uncertain rows with reasons;
- `executive_brief.md`: two-page-equivalent decision brief;
- `report.html`: portable executive view;
- `evaluation_results.json`: machine-readable evaluation result.

## Safety boundary

- deterministic calculations are authoritative;
- unsupported rows fail visibly;
- attribution is explicitly assumption-dependent;
- AI narrative is optional and may be disabled without affecting results;
- no recommendation is executed automatically;
- a named human decision-maker must approve any action;
- real organizational data must remain private and out of this repository.

## Current evidence status

| Evidence level | Current status |
|---|---|
| Synthetic reproducibility | Implemented and testable |
| Permissioned real-data use | Not yet achieved |
| Written external evaluation | Not yet achieved |
| Paid validation | Not yet achieved |

See `docs/` for the data contract, architecture, governance boundary, evaluation cases, and demonstration script.

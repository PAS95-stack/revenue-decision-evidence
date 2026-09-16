# Executive Evidence Brief — Synthetic Demonstration

**Evidence status:** synthetic and reproducible; not a customer result, real-data deployment, reference, or paid validation.
**Run ID:** `f4e8f6a2f167c5f6`
**Decision status:** not approved; named budget-owner approval is required.
**Engine:** 0.4.0 · **Rule set:** pas95-revenue-evidence-rules version 1

## Decision question

What can the supplied advertising, CRM, and revenue exports defend about the next channel-budget decision?

## Reconciled evidence

- Accepted spend: **AED 15,000.00** `[EV-SPEND-001]`
- Accepted revenue: **AED 56,500.00** `[EV-REV-001]`
- Attributed revenue: **AED 32,000.00** `[EV-ATTR-001]`
- Attribution coverage: **56.6%** `[EV-COVER-001]`
- Unattributed or invalid revenue: **AED 24,500.00** `[EV-UNMATCH-001]`
- Revenue with conflicting lead attribution: **AED 9,000.00** `[EV-CONFLICT-001]`
- Rejected or uncertain rows: **11**

## Exceptions to resolve

Rows excluded from every figure, or counted in revenue without a channel, grouped by reason. Largest amounts first.

| Rows | AED in these rows | Status | Reason | Who can fix it | Examples |
|---:|---:|---|---|---|---|
| 1 | 12,500.00 | duplicate | duplicate transaction_id | Finance or billing owner | `revenue:8` |
| 1 | 9,000.00 | accepted-unattributed | lead_id has conflicting CRM records; revenue cannot be attributed | CRM owner | `revenue:3` |
| 1 | 7,000.00 | accepted-unattributed | CRM campaign_id has no accepted advertising record | Advertising account owner | `revenue:6` |
| 1 | 6,000.00 | accepted-unattributed | lead_id has no accepted CRM record; revenue cannot be attributed | CRM owner | `revenue:7` |
| 1 | 2,500.00 | accepted-unattributed | revenue date precedes lead creation date | CRM owner | `revenue:10` |
| 1 | 2,000.00 | duplicate | exact duplicate advertising row | Advertising account owner | `ads:7` |
| 2 | — | conflict | lead_id has conflicting campaign attribution | CRM owner | `crm:3`, `crm:8` |
| 1 | — | rejected | spend_aed must be a plain non-negative decimal such as 1000 or 1000.50 | Advertising account owner | `ads:8` |
| 1 | — | rejected | lead_id and campaign_id are required | CRM owner | `crm:9` |
| 1 | — | rejected | value_aed must be a plain non-negative decimal such as 1000 or 1000.50 | Finance or billing owner | `revenue:9` |

## Channel view

- **Email** — spend AED 1,000.00 `[EV-CHSPEND-001]`; attributed revenue AED 3,500.00 `[EV-CHATTR-001]`; assumption-dependent ROAS 3.50× `[EV-CHROAS-001]`.
- **Paid Search** — spend AED 7,500.00 `[EV-CHSPEND-002]`; attributed revenue AED 16,000.00 `[EV-CHATTR-002]`; assumption-dependent ROAS 2.13× `[EV-CHROAS-002]`.
- **Paid Social** — spend AED 6,500.00 `[EV-CHSPEND-003]`; attributed revenue AED 12,500.00 `[EV-CHATTR-003]`; assumption-dependent ROAS 1.92× `[EV-CHROAS-003]`.

## Sensitivity to conversion delay

- 30 days: AED 28,500.00 attributed `[EV-WINATTR-030]`; AED 3,500.00 excluded `[EV-WINEXCL-030]`.
- 60 days: AED 32,000.00 attributed `[EV-WINATTR-060]`; AED 0.00 excluded `[EV-WINEXCL-060]`.
- 90 days: AED 32,000.00 attributed `[EV-WINATTR-090]`; AED 0.00 excluded `[EV-WINEXCL-090]`.

## Recommendation

**Do not reallocate budget from this evidence set alone.**

Reason: Attribution coverage is below the rule set's reporting threshold. Some source rows were rejected, duplicated or in conflict. Channel figures are attribution-based and do not show that spend caused revenue. This recommendation is graded **unsupported** and cannot be executed without human approval.

### What the exports show

- Attribution coverage is 56.6% [EV-COVER-001], below the rule set's reporting threshold of 80%.
- AED 24,500.00 of accepted revenue could not be assigned to a channel [EV-UNMATCH-001].
- AED 9,000.00 of that revenue belongs to leads or customers with contradictory CRM records [EV-CONFLICT-001].
- 3 rejected, 2 duplicate and 2 conflicting source rows are excluded from every figure; each is listed in row_dispositions.csv.
- AED 3,500.00 of attributed revenue arrived more than 30 days after lead creation [EV-WINEXCL-030], so channel figures change with the attribution window.

### Questions that need answers before any budget decision

- Which campaign is correct for each lead with contradictory CRM records?
- Can the source systems correct or confirm the rejected, duplicate and conflicting rows?
- Can the CRM export include the leads referenced by unattributed transactions?
- Can the advertising export include every campaign referenced by CRM leads?
- Why are some transactions dated before their lead was created?
- What comparison or controlled test would the budget owner accept as evidence that changing channel spend changes revenue?

Coverage threshold: 80% — planning choice used only to flag weak attribution coverage; not a validated decision threshold.

## What the evidence cannot defend

- causal incrementality or a claim that advertising caused the revenue;
- customer lifetime value beyond the supplied transaction period;
- an ROI guarantee or autonomous budget change;
- any conclusion from rejected, unmatched, or silently repaired rows.

## Required next action

The accountable budget owner must choose **approve**, **reject**, or **request more evidence**, record the rationale, and define the observation period. The deterministic report remains authoritative even if the optional AI narrative is disabled.

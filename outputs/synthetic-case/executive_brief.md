# Executive Evidence Brief — Synthetic Demonstration

**Evidence status:** synthetic and reproducible; not a customer result, real-data deployment, reference, or paid validation.  
**Run ID:** `91859f94c0a9e170`  
**Decision status:** not approved; named budget-owner approval is required.

## Decision question

What can the supplied advertising, CRM, and revenue exports defend about the next channel-budget decision?

## Reconciled evidence

- Accepted spend: **AED 15000.00** `[EV-SPEND-001]`
- Accepted revenue: **AED 56500.00** `[EV-REV-001]`
- Attributed revenue: **AED 41000.00** `[EV-ATTR-001]`
- Attribution coverage: **72.6%** `[EV-COVER-001]`
- Unattributed or invalid revenue: **AED 15500.00** `[EV-UNMATCH-001]`
- Rejected or uncertain rows: **9**

## Channel view

- **Email** — spend AED 1000.00; attributed revenue AED 3500.00; assumption-dependent ROAS 3.50×.
- **Paid Search** — spend AED 7500.00; attributed revenue AED 25000.00; assumption-dependent ROAS 3.33×.
- **Paid Social** — spend AED 6500.00; attributed revenue AED 12500.00; assumption-dependent ROAS 1.92×.

## Sensitivity to conversion delay

- 30 days: AED 28500.00 attributed; AED 12500.00 excluded.
- 60 days: AED 41000.00 attributed; AED 0.00 excluded.
- 90 days: AED 41000.00 attributed; AED 0.00 excluded.

## Recommendation

**Do not reallocate budget from this evidence set.**

Reason: Revenue attribution coverage is below 80%. This recommendation is graded **unsupported** and cannot be executed without human approval.

## What the evidence cannot defend

- causal incrementality or a claim that advertising caused the revenue;
- customer lifetime value beyond the supplied transaction period;
- an ROI guarantee or autonomous budget change;
- any conclusion from rejected, unmatched, or silently repaired rows.

## Required next action

The accountable budget owner must choose **approve**, **reject**, or **request more evidence**, record the rationale, and define the observation period. The deterministic report remains authoritative even if the optional AI narrative is disabled.

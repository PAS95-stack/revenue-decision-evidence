# Data contract and dictionary

This page describes the three-file contract used by the synthetic case. Exports
with other column names or formats, several files per source, other currencies,
refunds, or revenue that names customers are read through an engagement config;
see [`engagements.md`](engagements.md).

Exactly three CSV exports, UTF-8 (a byte-order mark is allowed), with a header
row. Extra columns are ignored. Real-data files must be approved, minimised,
pseudonymised, encrypted in transit and at rest, access-logged and deleted on the
agreed date. They must never be committed to this repository.

A missing required column, a file that is not UTF-8, or a malformed CSV stops the
run with an error naming the file.

## Value rules

| Kind | Accepted | Rejected examples |
|---|---|---|
| Amount (AED) | Plain non-negative decimal: `1000`, `1000.50`, `0.005`. Rounded half-up to fils; surrounding spaces ignored | `1e3`, `1,000`, `+5`, `-5`, `5.`, `.5`, `1_000`, `NaN`, non-ASCII digits, values too large to represent |
| Date | Calendar date `YYYY-MM-DD` | `20260701`, `2026/07/01`, `2026-7-1`, `2026-02-30`, date-times |
| Identifier or label | Non-empty after trimming spaces | Empty. Campaign IDs and channels may not contain `[` or `]` |

## Advertising export

| Field | Rule |
|---|---|
| `date` | Date |
| `campaign_id` | Required |
| `channel` | Required. A campaign mapped to more than one channel puts all of that campaign's rows in `conflict` |
| `spend_aed` | Amount |

Rows identical in date, campaign, channel and amount count once; later copies are
`duplicate`. The export has no row identifier, so two genuine but identical spend
rows cannot be told apart.

## CRM export

| Field | Rule |
|---|---|
| `lead_id` | Required. Identical rows for a lead count once; rows that disagree are all `conflict` |
| `campaign_id` | Required. Links the lead to a channel only if an accepted advertising row has the same campaign |
| `created_at` | Date |
| `status` | Required; compared case-insensitively |

## Revenue export

| Field | Rule |
|---|---|
| `transaction_id` | Required. Identical rows count once; rows that disagree are all `conflict` |
| `lead_id` | Required |
| `value_aed` | Amount |
| `date` | Date |

Accepted revenue is attributed to a channel only when its lead is accepted, the
lead's campaign has accepted spend, and the revenue is not dated before the
lead's creation. Otherwise it is `accepted-unattributed`: counted in revenue, not
in attribution.

With `revenue_join: customer_id`, revenue joins every accepted CRM lead of its
customer. The earliest lead dates the relationship. A customer whose leads came
from several campaigns stays unattributed unless the config declares `first_touch`
or `last_touch`.

## Row statuses

| Status | Meaning | Counts toward figures |
|---|---|---|
| `accepted` | Valid and, for revenue, attributed | Yes |
| `accepted-unattributed` | Valid revenue that cannot be linked to a channel | Revenue totals only |
| `rejected` | Breaks a value rule | No |
| `duplicate` | Identical to an earlier row with the same identity | No |
| `conflict` | Shares an identifier with rows that disagree | No |

Row references such as `crm:5` are the physical line where the record starts,
with the header as line 1. Blank lines and the extra lines of multi-line quoted
fields are counted. In an engagement the reference includes the file:
`crm/hubspot_contacts.csv:5`.

A rejected row shows its amount in `row_dispositions.csv` when that amount can
still be read, so the exceptions report can say what the row holds. It never
enters a figure.

## Evidence IDs

| ID | Figure |
|---|---|
| `EV-SPEND-001` | Accepted advertising spend |
| `EV-REV-001` | Accepted revenue, attributed or not |
| `EV-ATTR-001` | Attributed revenue |
| `EV-COVER-001` | Attributed revenue as a percentage of accepted revenue |
| `EV-UNMATCH-001` | Accepted revenue that could not be attributed |
| `EV-CONFLICT-001` | Part of that revenue whose lead has contradictory CRM records |
| `EV-REFUND-001` | Refunds and credit notes netted into revenue, as a negative amount; only when the engagement declares refunds |
| `EV-CHSPEND-nnn`, `EV-CHATTR-nnn`, `EV-CHROAS-nnn` | Spend, attributed revenue and ROAS per channel, numbered by sorted channel name |
| `EV-WINATTR-ddd`, `EV-WINEXCL-ddd` | Revenue attributed within, and received after, a window of `ddd` days |

## Lineage roles

| Role | Meaning |
|---|---|
| `summand` | The row's amount is added into the figure |
| `numerator`, `denominator` | The row forms one side of a ratio |
| `join` | The row links records (for example the CRM lead between revenue and a campaign) and carries no amount |

## Grades

- **Reconciled:** arithmetic directly traceable to accepted source rows.
- **Assumption-dependent:** also depends on treating the CRM campaign as the
  source of attribution, or on a window length.
- **Unsupported:** coverage is below the rule set's reporting threshold.

## Rule set

Published in every `report.json` with a fingerprint: attribution windows of 30,
60 and 90 days (or those an engagement config declares), and an 80% coverage reporting threshold. Both are planning
choices, not validated thresholds.

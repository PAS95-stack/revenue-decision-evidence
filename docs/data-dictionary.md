# Data contract and dictionary

Only three CSV inputs are accepted. Real-data files must be approved, minimized, pseudonymized, encrypted in transit and at rest, access logged, and deleted on the agreed date. They must never be committed to this repository.

## Advertising export

| Field | Type | Rule |
|---|---|---|
| `date` | ISO date | `YYYY-MM-DD` |
| `campaign_id` | string | Required; stable identifier |
| `channel` | string | Required; one channel per campaign |
| `spend_aed` | decimal | Non-negative AED amount |

## CRM export

| Field | Type | Rule |
|---|---|---|
| `lead_id` | pseudonymous string | Required; unique |
| `campaign_id` | string | Required; must map to advertising export for attribution |
| `created_at` | ISO date | `YYYY-MM-DD` |
| `status` | string | Required; organization-defined value retained for context |

## Revenue export

| Field | Type | Rule |
|---|---|---|
| `transaction_id` | pseudonymous string | Required; unique |
| `lead_id` | pseudonymous string | Required; maps to CRM export |
| `value_aed` | decimal | Non-negative AED amount |
| `date` | ISO date | Must not precede lead creation |

## Output grades

- **Reconciled:** deterministic arithmetic directly traceable to accepted source rows.
- **Assumption-dependent:** deterministic result that depends on an explicit attribution or time-window assumption.
- **Unsupported:** evidence is too incomplete or inconsistent to support the decision.

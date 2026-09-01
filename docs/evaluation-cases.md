# Evaluation suite

| # | Failure condition | Required behaviour |
|---:|---|---|
| 1 | Valid files | Reconcile deterministically and expose source lineage |
| 2 | Missing required IDs | Reject the row visibly |
| 3 | Duplicate IDs | Retain the first accepted record and reject the duplicate |
| 4 | Schema drift | Stop the run with a precise missing-column error |
| 5 | Invalid or negative currency | Reject the row; never coerce silently |
| 6 | Delayed conversion | Show 30/60/90-day sensitivity |
| 7 | Conflicting attribution | Reject the conflicting CRM record |
| 8 | Orphan revenue | Retain the accepted revenue total but mark it unattributed |
| 9 | Source-total integrity | Reconciled totals equal accepted source rows exactly |
| 10 | Unsupported AI narrative | Disable it if citations/boundaries fail |
| 11 | Revenue before lead | Reject attribution because chronology is invalid |
| 12 | Low attribution coverage | Recommend more evidence, not reallocation |

Run with `python3 -m unittest discover -s tests -v`. The generated `evaluation_results.json` covers output-level controls; the unit suite covers failure behaviour.

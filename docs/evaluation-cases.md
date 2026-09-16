# Evaluation suite

Run with `python3 -m unittest discover -s tests -v`.

**Result:** 148 tests, all passing on Python 3.13 on 16 September 2026, in about
three seconds. CI runs the suite on Python 3.11, 3.12 and 3.13, regenerates the
synthetic outputs and compares them byte for byte with the committed files, and
runs the independent checker on them.

## Original defects (`test_attacks.py`)

These twelve tests were committed failing before any fix, so the history shows
each defect existed. All now pass.

| ID | Attack | Required behaviour |
|---|---|---|
| a01 | Narrative says revenue is AED 65,500 citing evidence of AED 56,500 | Rejected |
| a02 | True spend figure cited as revenue | Rejected |
| a03 | Invented ROAS with no citation beside a valid citation | Rejected |
| a04 | "Paid Search drove the revenue" | Rejected |
| a05 | "No human approval is needed" | Rejected |
| a06 | A figure whose lineage rows do not add up to it | Every AED figure equals the sum of its rows |
| a07 | A rejected row counted in a total | No rejected row supports any figure |
| a08 | Swapping two conflicting CRM rows | Results unchanged |
| a09 | Same transaction ID with a different value | Both excluded as conflict |
| a10 | Spend written as `1e3` | Rejected |
| a11 | Wall clock changes between runs | Every output byte-identical; no timestamp |
| a12 | Every row unusable | Evaluation cannot pass |

## Row statuses and lineage (`test_dispositions.py`)

| ID | Required behaviour |
|---|---|
| p1_01–02 | Every record, including short and blank rows, gets exactly one status at its physical line |
| p1_03 | Excluded rows never add to any figure |
| p1_04 | Synthetic figures after symmetric conflict handling (32,000 attributed; 56.6% coverage) |
| p1_05 | Campaign mapped to two channels: all its rows excluded, in either order |
| p1_06 | Written files alone reconstruct every AED figure and the coverage ratio |

## Narrative gate (`test_narrative_gate.py`)

| ID | Required behaviour |
|---|---|
| g01 | Every figure's exact value passes; the next value up fails |
| g02 | Equivalent spellings pass: `56,500`, `56500.00`, `56.6 percent` |
| g03–g05 | Off by one fils, rounded percentage, or wrong unit: rejected |
| g06–g10 | Abbreviations, dates, malformed citations, citation in another sentence, signed numbers: rejected |
| g11–g12 | Channel figures bind to their own channel; window length allowed only beside a window figure |
| g13–g14 | Forecast, reallocation, ROI, incrementality, proof, approval or execution wording: rejected |
| g15 | No citation at all, or empty text: rejected |
| g16–g17 | Numbers written in words, relative quantities and channel rankings: rejected |
| g18 | A correct number described as another metric, or not described at all: rejected |
| g19–g20 | A channel or window figure must name its own channel or window |
| g21 | A figure cannot borrow a neighbouring citation; two figures followed by two citations are refused |
| g22 | Accurate descriptions in natural sentences pass |
| g23 | Every metric the engine produces has a description vocabulary |
| l01–l02 | Digits inside a cited channel name are not figures; bracketed labels rejected at ingest |
| w01–w03 | With a mocked model response: false output disabled, valid output gets the boundary appended, request carries no row references |

## Repeatability and recommendation (`test_repeatability.py`)

| ID | Required behaviour |
|---|---|
| r01–r04 | `run_id` changes with one fils, engine version, rule-set content or as-of date; identical runs are byte-identical; invalid as-of rejected |
| r05–r06 | Package version matches engine version; JSON written with sorted keys |
| c01–c02 | No output ever proposes a budget move, even at 100% coverage |
| c03–c04 | Synthetic findings cite evidence and questions are specific; threshold published as a planning choice |
| c05 | Source labels are escaped in `report.html` |

## Independent reconstruction (`test_reconstruction.py`)

| ID | Required behaviour |
|---|---|
| v01–v02 | Checker imports only the standard library and passes when run in isolated mode |
| v03 | A pass lists the checks and hashes every published file |
| v04–v05 | Unusable input publishes only the failing evaluation and removes a previous brief |
| v06 | Tampered revenue value, dropped lineage row, relabelled status, or altered export: detected |
| v07–v08 | Engine lineage defect, or a brief missing evidence: publication blocked |
| v09–v11 | CLI exits 2 on failure or schema error and clears stale outputs; a report without source paths cannot publish |

## Data attacks (`test_attack_suite.py`)

Every usable dataset here must also pass publication.

| ID | Required behaviour |
|---|---|
| d01–d03 | Exact duplicate transaction counted once; different values excluded as conflict; duplicate ad row excluded with its amount shown |
| d04 | All 24 orders of four CRM rows, two of them conflicting, give one identical result |
| d05 | Missing identifiers rejected in each export |
| d06 | 14 malformed amounts rejected; 5 valid spellings accepted and rounded half-up |
| d07 | 8 malformed dates rejected |
| d08–d10 | Orphan lead, unknown campaign, revenue before lead: counted as revenue, not attributed |
| d11–d12 | Schema drift names the file and column; header-only export publishes only the failure |
| d13–d15 | Byte-order mark and CRLF change nothing; blank lines and multi-line fields keep references on the right line; non-UTF-8 rejected with the file named |

## Generated datasets (`test_generated.py`)

| ID | Required behaviour |
|---|---|
| f01 | 200 seeded datasets with injected faults. Each record gets one status, repeat runs match, shuffled rows change no figure, engine and checker never disagree, and unusable evidence is never published |

In the fixed seed the datasets contain 3,688 records: 536 rejected, 129
duplicate, 335 conflict and 790 unattributed. 136 datasets publish and 64 are
refused. CRLF appears in 100, a byte-order mark in 46, multi-line fields in 78
and blank lines in 30. The test fails if any of these counts falls below a set
minimum.

## Planted defects (`test_planted_defects.py`)

| ID | Required behaviour |
|---|---|
| x01 | Control: the unmodified engine publishes |
| x02–x05 | First row wins on conflicts, duplicates counted, half-even rounding, or lenient `1e3` parsing patched into the engine: publication blocked, with row-level causes listed first |

## Committed outputs (`test_committed_outputs.py`)

| ID | Required behaviour |
|---|---|
| o01 | `outputs/synthetic-case` matches a fresh run byte for byte |

## Walkthrough (`test_demo_script.py`)

| ID | Required behaviour |
|---|---|
| w04 | `scripts/demo.sh` runs to completion and every step gets its expected exit code, including rejection of the false and the mislabelled revenue sentence |

## Engagement configs (`test_engagement.py`)

| ID | Required behaviour |
|---|---|
| e01 | Messy example (Meta, Google Ads, HubSpot and Xero shapes) publishes the hand-calculated figures; a USD cost in an AED file and a lead without a campaign are rejected; the credit note counts as a refund |
| e02 | CLI and standalone checker run from the config |
| e03–e04 | Native exports without a config, or a config naming an absent column: the error names the file, field and columns found |
| e05 | Values that break the declared format are still rejected; reasons do not copy row values |
| e06–e07 | 22 kinds of invalid config, and duplicate keys, refused with the reason |
| e08–e09 | Config, export, exception figures, exceptions.csv or brief changed after publication: detected; a config report cannot be verified as the plain contract |
| e10 | Reordering files in the config changes no figure or status |
| e11 | Exceptions grouped by reason, largest amount first, with who can fix them |
| e12 | Attribution windows come from the config |
| e13–e15 | Client exports or outputs inside a checkout, or the AI narrative without recorded permission: refused |

## Formats, refunds, currencies and customers (`test_engagement_formats.py`)

| ID | Required behaviour |
|---|---|
| f01 | Refunds in parentheses and a credit-note export net into revenue and attribution; a minus sign inside a credit-note export is rejected |
| f02 | A USD ad account is converted at the declared rate and rounded once per row; an AED amount in a USD export is rejected |
| f03 | Rejected rows keep readable amounts for the exceptions report without entering any figure |
| f04 | Revenue joined through customers: repeat leads, several campaigns, contradictory CRM rows, unknown customers and early revenue each get their own reason |
| f05–f06 | Declared first-touch and last-touch rules credit repeat customers; a rule needs a customer join |
| f07 | The synthetic case rewritten as messy native exports gives identical figures, statuses and exception totals |
| f08 | 60 generated datasets under random declared formats: engine and checker never disagree |
| f09 | Engagement script: a folder inside a checkout, unlogged or changed exports are refused; a logged run publishes; deletion check |
| f10 | A line-item export becomes invoices without losing the lines: a repeated line is a duplicate, lines of one invoice that disagree are held back, and a negative line is a refund |
| f11 | A declared filter excludes unpaid invoices, keeps their amount and reports them as a finding |
| f12 | An advertising row identifier separates genuinely identical rows from duplicates and conflicts |
| f13 | The coverage threshold comes from the config and changes `run_id` |
| f14 | An optional identifier must be declared for every file of a source, or none |
| f15 | A wrongly declared date or amount format is diagnosed with the declaration that would have matched |
| f16 | Above the published limits the row detail and lineage stay in the CSV files, which keep every row |
| f17 | A report that drops detail small enough to publish is refused |
| f18 | An .xlsx sheet converts to a CSV whose lines are the sheet's rows, with date-formatted cells as dates and the workbook's SHA-256 recorded |
| f19 | A converted workbook runs end to end and its row references are spreadsheet rows |
| f20 | Semicolon-separated Windows-1252 exports are read when declared, with quoted newlines keeping later rows on their own line |
| f21 | A file may declare several date formats, tried in order; two that would read one value as two different dates are refused |
| f22 | A total computed from quantity × unit price behaves as any other amount, including refunds and line conflicts; a missing part names the column |
| f23 | A currency named per row converts at its declared rate, a code with no rate is rejected, and the brief says which rates were used |
| f24 | A declared time-zone shift moves a timestamp into the day it belongs to, and is refused unless the format carries a time |
| f25 | A config drafted from the exports runs end to end and reproduces the worked example's figures |
| f26 | The draft marks what only a person can decide: an exchange rate, a computed total, a channel with no column, an ambiguous date |
| f27 | A report title above the header is declared with `header_row`, not deleted from the export; the totals row below the data is refused rather than counted |
| f28 | European amounts (`1.234,56`) and a currency symbol beside the number are read when declared, refunds included |
| f29 | The draft detects a title row, a semicolon file, a European amount with its symbol, and snake_case column names |
| f30 | A computed total declares the number format of its parts, and the drafted config runs |

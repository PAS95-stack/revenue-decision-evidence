# Evaluation suite

Run with `python3 -m unittest discover -s tests -v`.

**Result:** 169 tests, all passing on Python 3.13 on 16 September 2026, in about
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
| f31 | Month names and two-digit years are read when declared, and every figure and window is unchanged |
| f32 | The draft reads a month-name date shape from the values themselves |
| f33 | A value carrying its own UTC offset moves to the day it belongs to; a day-first and month-first pair is refused whatever separator it uses, while a year-first shape is never ambiguous |
| f34 | One command creates the folder, records every export at intake, drafts the config and runs it, reproducing the worked example's figures |
| f35 | The same command refuses to run when the draft still needs a person, and publishes nothing |
| f36 | A file naming the zone its timestamps are written in has every date stated in the reporting zone, summer time included; an unknown zone, or a zone beside a shift, is refused |
| f37 | A second export fills a field through a shared key, is fingerprinted into `run_id`, and a row whose key it does not carry is refused and named rather than filled |
| f38 | A key the second export repeats with identical values is used; one it repeats with different values makes each row pointing at it a conflict, excluded and listed; a blank key matches nothing; and reordering the second export's rows changes no status |
| f39 | A lookup may match on two columns, the brief states its values are as the file stands now rather than on each row's date, and a missing lookup column is refused |

## Join discovery guards (`test_joins.py`)

| ID | Required behaviour |
|---|---|
| j01 | A column contained in another file's never-repeating key, under the same name, is proposed at **check**, and a key that repeats is never taken as a parent |
| j02 | When the names differ the join is proposed only for a person to confirm |
| j03 | Fewer than 30 distinct values are not proposed; the measured low-cardinality variant surfaces them only for a person |
| j04 | A file that opens with thousands of rows of `1` is read as integers, not as a yes/no flag |
| j05 | Two auto-increment sequences under different names are not a join; one record split across two files, keeping its name, is |
| j06 | Small integers sitting at the start of a much larger key's range are not a join |
| j07 | A key that repeats would fan one row out into several, and is reported as such rather than used |
| j08 | Blank cells match nothing, and a key column holding a blank is not a key |
| j09 | Values that match only once `00123` is read as `123` are proposed only for a person |
| j10 | Single words such as surnames are not codes, however many files share them |
| j11 | A two-column key is found and always left for a person to confirm |
| j12 | A column with a handful of values does not make a two-column key |

## Join discovery benchmark (`benchmarks/joins/`)

Join discovery is measured against public multi-table datasets whose true relationships are
known independently of the tool. `prepare.py` downloads them outside this repository,
records every file's SHA-256, and writes each dataset's ground truth; `run.py` measures.

| Dataset | Files | True relationships | Where the truth comes from |
|---|---:|---:|---|
| Olist | 9 | 8 | The published Olist schema |
| TPC-H, scale factor 0.05 | 8 | 8, one of them two columns | TPC-H specification clause 1.4; data from the official dbgen |
| Chinook | 11 | 10 | Foreign keys declared in the SQLite build |
| Northwind | 13 | 10 | Foreign keys declared in the SQLite build |
| Sakila | 16 | 22 | Foreign keys declared in the SQLite schema |
| Instacart | 4 of 7 | 3 | Published schema; the public mirror lacks `orders.csv`, so one relationship is excluded |

Excluded from the truth, and why: references within one table (not a join between two
exports) and relationships where either table has no rows. The development datasets (Olist,
TPC-H, Chinook) and held-out datasets (Northwind, Sakila, Instacart) were fixed before any
result was seen. The negative control runs discovery over all 61 files together: any join
between two different datasets is false by construction. AdventureWorks and CTU Prague's
relational repository were not included in this round.

**Measured on 16 September 2026, rules as specified:**

| | Precision at check | Precision at any confidence | Recall at check | Recall at any confidence | Recall of reachable joins | One-to-many detected |
|---|---:|---:|---:|---:|---:|---:|
| Development | 100% (20/20) | 100% (21/21) | 65.4% (17/26) | 69.2% (18/26) | 100% (18/18) | 22/22 |
| Held out | 100% (17/17) | 100% (20/20) | 42.9% (15/35) | 48.6% (17/35) | 100% (17/17) | 26/26 |
| All | **100% (37/37)** | **100% (41/41)** | 52.5% (32/61) | 57.4% (35/61) | **100% (35/35)** | **48/48** |

Negative control, 61 files: **0 false joins at any confidence.** Ten seconds end to end.

The target was at least 95% precision and at least 85% recall. Precision meets it; recall
does not, and every miss is explained. A reachable join is one whose child column has at
least 30 distinct values and at least 60% of them in the parent key. Of the 26 unreachable
relationships, 23 have fewer than 30 distinct values — stores, staff, regions, media types,
nations, languages — one is 45% contained (Olist closed deals whose sellers never sold), one
column is empty, and one (`Orders.ShipVia`) is an integer not named like an identifier. All
35 reachable joins were found.

How the rules changed while measuring, so the figures can be weighed. The first
measurement gave 100% (37/37) at check and 80.4% (41/51) at any confidence, with 12 false
joins in the negative control. Four faults were then fixed: a sorted column opening with
rows of `1` was read as a flag; two auto-increment sequences under different names were
matched when they did not start together; a two-column key could be carried by a column with
a handful of values; and single words were treated as codes. The first three surfaced in
development data. Held-out failures were visible when they were fixed, so the held-out
figure at check is a blind measurement and the held-out figure at any confidence is not.

**A variant, measured and not adopted.** Also surfacing joins with fewer than 30 distinct
values when the names agree, never above needs you, gives 86.9% (53/61) recall at any
confidence with within-dataset precision still 100% (60/60) — but 3 false joins in the
negative control: `Employee` and `Employees` across Chinook and Northwind, and `Categories`
and `category` across Northwind and Sakila. Small same-named tables in unrelated systems
match by chance, which is what the 30-value rule is there to prevent.

What the benchmark cannot show: how often a real client's exports use small dimension
tables; joins through a bridge table, which are out of scope; and value formats these
datasets do not contain.

Reproduce:

```bash
python3 benchmarks/joins/prepare.py --out ~/benchmarks/joins --tpch ~/benchmarks/tpch
python3 benchmarks/joins/run.py --data ~/benchmarks/joins
python3 benchmarks/joins/run.py --data ~/benchmarks/joins --low-cardinality
```

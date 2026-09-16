# Limitations and design decisions

## What this demonstrates

- Figures computed deterministically from three synthetic CSV exports, with every
  input row given one status and every figure traceable to the rows it is built
  from.
- A second, separate implementation that reconstructs every published figure
  from the raw exports; outputs are published only when the two agree.
- A narrative gate that rejects any number not equal to the evidence it cites.
- Real-shaped exports read through a declared engagement config, rehearsed on
  25,900 public invoices with a synthetic CRM and advertising overlay, and on the
  public Olist marketing funnel: 8,000 real leads carrying a channel origin, 842
  closed deals and 112,650 real order lines joined through customers, with the
  advertising spend a declared overlay because that dataset publishes none.

Verified with 153 tests on Python 3.13. CI runs the same suite on Python
3.11–3.13 for every push.

## What it does not demonstrate

- Use on real organisational data, external evaluation or paid validation.
- That advertising caused revenue. Attribution follows the CRM campaign ID; it is
  an assumption, not a measure of incrementality.
- That changing budget would improve results. The report never recommends it.

## Known limitations

**Data contract**
- Other currencies are converted at one fixed rate declared per file; there is no
  dated exchange-rate table.
- Refunds count only when declared, as negative amounts or a credit-note export.
  Under the three-file contract negative amounts are rejected.
- Advertising rows have no identifier unless the export has one and the config
  declares it as `row_id`; without it two genuine rows with the same date,
  campaign, channel and amount count once, with the excluded amount visible in
  `row_dispositions.csv`.
- Spend and revenue periods are not aligned: channel ROAS divides all attributed
  revenue in the files by all accepted spend.
- An engagement config maps columns and declares formats. One file may declare
  several date formats, compute an amount from two columns, convert a currency
  named per row at a declared rate, read European numbers such as `1.234,56`,
  carry a currency symbol beside the amount, and start its header below a report
  title. It cannot invent a rate, read an undeclared format, or choose between two
  readings of an ambiguous date: each is refused or left for a person to settle.
- Line-item exports are summed per invoice when `line_id` is declared. Lines of
  one invoice that disagree about the customer or date are all held back.
- Invoice status is read only when an `include_when` filter declares which values
  to keep; otherwise every row in the export counts, including unpaid invoices.
- A date is read in whatever shape the file declares, including month names and
  two-digit years, and converted to the one calendar date every figure uses. Time
  of day is otherwise ignored, unless the value carries its own UTC offset (`Z` or
  `+04:00`), or the file declares `time_zone_shift_hours`: then the instant is
  moved into the day it belongs to. The tool reads many shapes but never decides
  between two readings of one value; an ambiguous file is refused or settled by a
  person.
- A run reads only what the config declares; it detects nothing. Separators are
  `comma`, `semicolon` or `tab`, and encodings UTF-8, UTF-16, Windows-1252 or
  Latin-1. `propose` reads the exports beforehand and drafts those declarations
  from the data, but a person checks the draft and the run still refuses anything
  that does not match it.
- Spreadsheets are converted to CSV by `scripts/xlsx_to_csv.py` before a run. The
  conversion records the workbook's SHA-256 and keeps row numbers aligned with the
  sheet, but it is a step outside the checked path, and it reads a cell as a date
  when the sheet displays it as one.
- Amounts too large to represent are rejected, but a sum close to Python's
  28-digit decimal precision is not guarded.

**References**
- Channel evidence IDs are numbered by sorted channel name within one run. In a
  different dataset the same ID can mean a different channel.
- Row references are physical line numbers. They do not survive sorting or
  editing the export.

**Narrative gate**
- It relies on word lists. Judgement words not on the list pass, for example
  "Paid Search looks promising at 2.13×". The word "one" is allowed.
- Descriptions are checked against a fixed vocabulary for each metric. A figure
  described only in other words ("takings") is rejected, but an unlisted word
  beside a correct one passes: "Spend, or takings, is AED 15,000
  [EV-SPEND-001]".
- Each figure must be cited directly after the words that describe it. Some
  accurate phrasings are therefore refused, such as two figures followed by two
  citations, or the word "after" beside a figure that is not a window figure.
- Numbers must match exactly, so a correct but rounded figure is rejected.
- Tested with fixed text and a mocked model response. No real model output has
  been evaluated.

**Verification**
- The engine and checker implement one written contract separately, by the same
  author. A code bug in either is caught; a misreading shared by both is not.
- The checker verifies the figures and citations in findings, not the rest of
  their wording.
- `report.json` carries row dispositions and lineage only up to 50,000 rows and
  200,000 lineage entries; above that they stay in the CSV files, which the
  checker verifies row by row either way.
- Exception groups use the engine's reason text. The checker verifies their rows,
  amounts and examples, not the wording; "who can fix it" is a fixed mapping.

**Size**
- Measured on this machine, each run checked by the independent reconstruction:
  34,289 rows in about 7 seconds; 108,389 rows in 4.3 seconds using 0.9 GB;
  550,298 rows in 27 seconds using 4.7 GB.
- Both the engine and the checker hold a run in memory, so memory grows with the
  number of rows. Around half a million rows needs roughly 5 GB; beyond that,
  run a shorter period per engagement.
- `lineage.csv` is the large file, because every figure names every row behind
  it: 384 MB for 3.9 million lineage entries at 550,298 rows, beside a 28 KB
  `report.json`.

**Scope**
- Not a security review. The code has no access control, encryption or retention
  handling.
- The 80% coverage threshold and the default 30, 60 and 90-day windows are
  planning choices, not validated values; configured windows are the
  engagement's choice.
- With customer joins, first-touch and last-touch crediting are conventions, not
  measurements; the default leaves such customers unattributed.

## Repeatability

With the same input bytes, engine version, rule set, as-of date and engagement
config, every output file is byte-identical and `run_id` is the same. Changing any of them changes
`run_id`. Checked on Python 3.13 by comparing two runs and by a test that changes
the clock. `.gitattributes` stops line-ending conversion of the committed inputs
and outputs.

## Design decisions

1. **Conflicting rows are all excluded.** Keeping one would let export order
   decide attribution; in the synthetic data that moved AED 9,000 between
   channels. The cost is revenue left unattributed until the source is corrected,
   shown as `EV-CONFLICT-001`.
2. **Revenue dated before its lead is counted but not attributed.** The sale is
   valid; only its link to a campaign is doubtful. Rejecting it understated
   revenue.
3. **Amounts and dates follow a strict contract.** `1e3`, `1,000` and `20260701`
   can each be read more than one way, and refusing is safer than guessing. Real
   exports declare their formats in an engagement config instead; the cost is
   that each engagement must state them, and a wrong declaration rejects rows.
4. **The report never recommends moving budget.** Attribution coverage and an
   approval step cannot show that spend caused revenue, so the output is findings
   and questions.
5. **Nothing publishes unless an independent reconstruction agrees.** A report
   that looks right but is wrong is worse than none; a failed run leaves only the
   list of failures.
6. **The narrative is optional and the application states decision rights.** The
   model cannot soften or negate the approval boundary, and the report stays
   authoritative without it.
7. **Row references are physical lines.** Counting records drifts as soon as an
   export contains a blank line or a multi-line field, and the checker could not
   catch it while both counted the same way.

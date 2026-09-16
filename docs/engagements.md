# Running on a client's exports

Real exports never match the three-file contract: columns have other names, dates
and amounts are written differently, ad accounts may bill in another currency, and
revenue may name a customer rather than a lead. An engagement config declares all of
that. Nothing is guessed: a value that does not match its declaration is rejected
and shown, with the amount it holds where that amount can be read.

The steps below take a few minutes once the exports have arrived.

## Before the data arrives

Ask the client for:

1. The export from each system, as CSV: advertising (every ad account), CRM, and
   revenue (invoices or orders).
2. Either one row per invoice, or a line-item export. For line items, declare
   `line_id`: lines are summed into their invoice, each line keeps its own row
   reference, and lines of one invoice that disagree about the customer or date are
   all held back as a conflict.
3. Refunds and credit notes: either negative totals in the revenue export, or a
   separate credit-note export.
4. The currency of each ad account.
5. Whether revenue records reference the **lead** or the **customer**.
6. Pseudonymous identifiers only: no names, emails, phone numbers, free text or
   health details (see `governance.md`).

## The short way

One command does the whole journey. It creates the folder if it is new, converts any
spreadsheets, records what arrived, drafts the config from the exports themselves, and
runs when nothing is left for a person to decide:

```bash
python3 scripts/engagement.py ingest ~/engagements/acme-2026-09 --received-on 2026-09-16 --received-from "Finance manager"
```

The first call creates the folder and asks for the exports. Put them in `inputs/` and
repeat it. The draft is printed with the evidence for every choice, and its lines are
marked two ways:

- **needs you** — a choice only a person can make, such as an exchange rate, a date that
  reads both ways, or a field no column matched. A run does not start until it is settled.
- **check** — worth reading, but the cost of leaving it is already visible: values that do
  not match the declaration are refused and listed in the report rather than counted.

Add `--accept-draft` to run once nothing is marked **needs you**; anything marked
**check** is printed first, so what was left unresolved stays in front of you.

The numbered steps below are the same journey done one piece at a time, and remain the
way to re-run an engagement, change a declaration, or close it.

## 1. Create the engagement folder

It must be outside this repository; the script and the CLI refuse otherwise.

```bash
python3 scripts/engagement.py init ~/engagements/acme-2026-09 --name "Acme clinics, September budget review"
```

This creates `inputs/` (with a config template), `outputs/`, and `records/` with an
intake log, an access log and a deletion record.

## 2. Log the exports

Copy the approved exports into `inputs/` without editing them, then:

```bash
python3 scripts/engagement.py intake ~/engagements/acme-2026-09 --received-on 2026-09-16 --received-from "Finance manager"
```

The SHA-256 of each file is recorded. A run refuses any export that was not logged or
has changed since. If the client sends a corrected file, give it a new name and log
it again.

### Spreadsheets

A workbook is converted to CSV first, keeping the sheet's row numbers so a row
reference points at the spreadsheet row:

```bash
python3 scripts/xlsx_to_csv.py ~/engagements/acme-2026-09/inputs/Orders.xlsx --sheet Invoices --out ~/engagements/acme-2026-09/inputs/invoices.csv
```

It writes `invoices.csv.conversion.json` recording the workbook's SHA-256, the
sheet, the rows written and how many cells were read as dates. Log both the
workbook and the CSV at intake. Cells shown as dates become `YYYY-MM-DD`, or
`YYYY-MM-DD HH:MM:SS` when they carry a time; every other cell is written as the
workbook stores it.

## 3. Complete `inputs/engagement.json`

Draft it from the exports rather than transcribing headers by hand:

```bash
python3 scripts/engagement.py propose ~/engagements/acme-2026-09
```

This reads every CSV in `inputs/` and writes `engagement.proposed.json` beside a
report saying how each choice was reached: which column matched, how many sampled
values parsed under the format proposed, and how many identifiers overlap between
files. Anything the data cannot settle — an exchange rate, a channel name, a date
that reads both ways — is marked **needs you** rather than guessed. Check every
line, then rename the draft to `engagement.json`.

When one system exports a record across files — leads in one and closed deals in
another, order lines in one and orders in another — the draft finds how they join from
the values themselves. A file covering a source's required fields stands as that source;
one that does not may supply a missing field as a `lookup`, joined on the source's own
record, with the evidence: the share of values contained, that the key never repeats, and
whether the names and the shape of the values agree. A second revenue export describing
the same transactions is left out rather than counted twice, and a file that fits nowhere
is listed under **Not used**.

The draft is a starting point, not an authority: nothing in it changes how a run
reads data, and a value that does not match its declaration is still rejected. The
worked example is `examples/messy-exports/engagement.json`, which reads Meta,
Google Ads, HubSpot and Xero-shaped files.

### Top level

| Key | Required | Meaning |
|---|---|---|
| `config_version` | yes | `1` |
| `engagement` | yes | Name shown in the brief |
| `data_origin` | yes | `client`, `public` or `synthetic`. Sets the brief's status line; `client` also enforces the folder guard |
| `sources` | yes | `ads`, `crm` and `revenue`, each a list of 1 to 20 files |
| `coverage_threshold_percent` | no | The coverage below which the report calls attribution weak, as a string such as `"70"`. Default `"80"`. A planning choice, not a validated threshold |
| `reporting_time_zone` | no | The zone every date is stated in, as an IANA name such as `Asia/Dubai`. Default `UTC`. It only matters when a file declares the zone its timestamps are written in, or a value carries its own offset |
| `attribution_windows_days` | no | 1 to 5 increasing whole days up to 999. Default `[30, 60, 90]`. Use longer windows for long sales cycles, such as `[90, 180, 365]` |
| `channel_aliases` | no | Channel names as written, matched without regard to letter case, mapped to the name to report: `{"search": "Paid Search"}` |
| `revenue_join` | no | `lead_id` (default) or `customer_id` |
| `multiple_campaigns` | no | With `customer_id` only. What to do when a customer's accepted leads came from several campaigns: `unattributed` (default), `first_touch` (credit the earliest lead) or `last_touch` (credit the latest lead created by the transaction date) |

### Each file

| Key | Required | Meaning |
|---|---|---|
| `file` | yes | File name, relative to the config. Row references use it, e.g. `revenue/xero_invoices.csv:6` |
| `columns` | yes | Field to column header, exactly as written in the export |
| `fixed` | no | A value for a whole file: `channel` for an ad export with no channel column, or `status` for a CRM export |
| `delimiter` | no | `comma` (default), `semicolon` (Excel in many locales) or `tab` |
| `encoding` | no | `utf-8` (default, byte-order mark allowed), `utf-16`, `windows-1252` or `latin-1` |
| `header_row` | no | The line the column names sit on, 1 by default. Advertising platforms print a report title and a date range above them; declare `3` rather than editing the export. Row references stay the lines of the file as delivered |
| `date_format` | no | How this file writes a date, built from `YYYY`, `YY`, `MMMM` (July), `MMM` (Jul), `MM`, `M`, `DD` and `D` with the separators `-` `/` `.` and space: `YYYY-MM-DD` (default), `DD/MM/YYYY`, `DD.MM.YYYY`, `D MMM YYYY`, `MMM D, YYYY`, `DD-MMM-YY` and so on. Add a time as ` HH:MM`, ` HH:MM:SS`, `THH:MM:SS` or ` HH:MM AM`; a value may also carry `Z` or `+04:00`, and then the instant is converted before the day is taken. Two-digit years read 69–99 as the 1900s and 00–68 as the 2000s. Month names are read in English, Arabic, French, German, Spanish, Portuguese, Italian and Dutch, in full or shortened, with or without accents; a shortening two months share, such as the French `jui` of juin and juillet, is refused rather than guessed. A list declares several for one file, tried in order. Declaring both a day-first and a month-first numeric shape is refused, because `03/07/2026` would be two dates |
| `time_zone_shift_hours` | no | Whole hours from −14 to 14 added before the day is taken, for an export written in another time zone. Every declared `date_format` must carry a time |
| `time_zone` | no | The zone this file's timestamps are written in, as an IANA name such as `America/Sao_Paulo`. Values are then stated in `reporting_time_zone` before the day is taken, so summer time is handled rather than assumed. Every declared `date_format` must carry a time, and a file declares this or `time_zone_shift_hours`, not both |
| `amounts.thousands_separator` | no | `","` or `""` with a decimal point; `"."` or `""` with a decimal comma |
| `amounts.decimal` | no | `point` (default) reads `1,234.56`; `comma` reads `1.234,56`, as Excel writes it in many locales |
| `amounts.currency_label` | no | Text written beside the number, before or after it: `AED`, `USD`, `$` or `R$`. Up to 8 characters and no digits |
| `amounts.currency` | no | `{"code": "USD", "aed_per_unit": "3.6725", "rate_source": "UAE dirham peg to the US dollar"}`. One fixed rate per file, rounded half-up to fils after conversion |
| `amounts.currency` (per row) | no | When each row names its own currency: `{"column": "Currency", "rates": {"USD": "3.6725", "AED": "1"}, "rate_source": "where the rates come from"}`. A code with no declared rate is rejected and named, never converted at a guess |
| `amounts.value_from` | no | Compute the amount where the export carries no total: `{"multiply": ["Quantity", "Unit price"]}` or `{"add": ["Net", "Tax"]}`. The computed field then takes no column of its own |
| `amounts.refunds` | revenue only | `reject` (default), `negative_values` (`-500.00` or `(500.00)` is a refund) or `whole_file` (every row is a refund written as a positive amount) |
| `uppercase` | no | Identifier fields to upper-case, when one system writes `meta-101` and another `META-101` |
| `include_when` | no | Keep only rows whose column holds one of these values, compared without regard to letter case: `{"column": "Status", "values": ["PAID"]}`. Excluded rows get the status `filtered`, keep their amount, and are reported as a finding |
| `columns.row_id` | advertising | The export's own row identifier (ad set ID, ad ID). Without it, two genuinely identical rows count once |
| `columns.line_id` | revenue | Declares a line-item export: the column identifying the line within an invoice (line number, SKU). Declare it in every revenue file or none |
| `lookup` | no | Fill a field from a second export that shares a key, when one system exports the record across two files: `{"file": "deals.csv", "match": {"their_key": "my_key"}, "columns": {"customer_id": "seller_id"}}`. `match` names one or two columns. The second file is read the same way as this one, is fingerprinted into `run_id`, and must be logged at intake. A key it repeats with identical values is used; a key it repeats with different values makes every row pointing at it a conflict, excluded and listed, so file order never decides. A row whose key is not in it keeps an empty value, so it is refused and named rather than quietly filled. Values are as the second file stands now, not as they stood on each row's date, and the brief says so |

Fields per export: advertising `date`, `campaign_id`, `channel`, `spend_aed` (and
optionally `row_id`); CRM
`lead_id`, `campaign_id`, `created_at`, `status` (plus `customer_id` when revenue joins
through customers); revenue `transaction_id`, `lead_id` or `customer_id`, `value_aed`,
`date`.

## 4. Run

```bash
python3 scripts/engagement.py run ~/engagements/acme-2026-09
```

Outputs are published only when the independent checker agrees with the engine. If
the run stops, it prints why:

| Message | What to do |
|---|---|
| `is missing 'Cost' (mapped to spend_aed). Found columns: …` | Correct the header in the config |
| `the revenue export has no usable rows` | Open `outputs/evaluation_results.json`; usually a date or amount format is declared wrongly |
| `was not logged at intake` or `changed since intake` | Log the file, or ask for a corrected export under a new name |

Every failure also prints `hints`: the declaration that would have matched the
rejected rows, for example

```json
"hints": ["ads/meta_ads.csv: 3 of 3 rejected rows would match date_format \"DD/MM/YYYY\" (declared \"YYYY-MM-DD\")."]
```

A hint is guidance, never a change: nothing is reinterpreted until the config says so.

## 5. Read out

- `executive_brief.md`: figures, **Exceptions to resolve** (largest amounts first,
  with who can fix them), channels, sensitivity, findings, questions, and every
  declared interpretation.
- `exceptions.csv`: the full grouped list to send to each owner.
- `row_dispositions.csv`: every input row with its status, reason and file line.

On a large export, `report.json` points at the CSV files rather than repeating
them; `lineage.csv` and `row_dispositions.csv` always hold the full detail.

## 6. Close

After the agreed deletion date, delete the exports and outputs, then:

```bash
python3 scripts/engagement.py deletion-check ~/engagements/acme-2026-09
```

It exits with 1 while any file remains, and otherwise lists every export received for
the deletion record in `records/deletion_record.md`.

## Rehearsal on public data

The procedure above was run on 16 September 2026 with real invoices from the UCI
Online Retail dataset (CC BY 4.0): 25,900 invoices totalled from 541,909 lines, with
month/day/year dates, 3,838 negative invoices and 3,710 without a customer ID,
converted from pounds at a declared illustrative rate. **The CRM and advertising
exports were synthetic**, because no public dataset links a retailer's customers to
ad campaigns, so the attribution figures say nothing about that retailer. The run
covered 34,289 rows, took about 7 seconds, and was confirmed by the independent
checker. Its largest exception was the invoices without a customer ID, which held
AED 7,093,643.80. It publishes an 8.9 MB `report.json` beside a 27 MB `lineage.csv`
holding all 274,801 lineage entries, and a 7 KB brief.

The same dataset was also run as a line-item export, one row per order line:
541,909 lines totalled into invoices, 550,298 rows in 27 seconds, again confirmed
by the checker. It found 10,093 lines whose invoice and line identifier repeat
with different values, and 4,793 exact duplicate lines.

## Size

A run holds its rows in memory in both implementations. Measured here, each run
confirmed by the independent checker: 34,289 rows in about 7 seconds; 108,389 rows
in 4.3 seconds using 0.9 GB; 550,298 rows in 27 seconds using 4.7 GB, writing a
28 KB `report.json` beside a 384 MB `lineage.csv`. Around half a million rows needs
roughly 5 GB of memory; beyond that, run a shorter period per engagement.

## What a config cannot express

- More than one date format or currency within one file.
- Columns computed from others, such as quantity × unit price.
- Invoice status: an unpaid invoice counts as revenue if it is in the export.
- Time zones. The date is taken as written.
- Reading a workbook directly: the conversion above is a step outside the checked
  run, recorded by its manifest.
- Attribution rules other than first and last touch.

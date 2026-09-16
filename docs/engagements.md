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

Open each export's header row and replace the placeholders. The worked example is
`examples/messy-exports/engagement.json`, which reads Meta, Google Ads, HubSpot and
Xero-shaped files.

### Top level

| Key | Required | Meaning |
|---|---|---|
| `config_version` | yes | `1` |
| `engagement` | yes | Name shown in the brief |
| `data_origin` | yes | `client`, `public` or `synthetic`. Sets the brief's status line; `client` also enforces the folder guard |
| `sources` | yes | `ads`, `crm` and `revenue`, each a list of 1 to 20 files |
| `coverage_threshold_percent` | no | The coverage below which the report calls attribution weak, as a string such as `"70"`. Default `"80"`. A planning choice, not a validated threshold |
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
| `date_format` | no | One of `YYYY-MM-DD` (default), `DD/MM/YYYY`, `MM/DD/YYYY`, each optionally followed by ` HH:MM` or ` HH:MM:SS`, or `YYYY-MM-DDTHH:MM:SS`. The time of day is ignored |
| `amounts.thousands_separator` | no | `","` or `""` |
| `amounts.currency_label` | no | Text that may precede or follow the number, such as `AED` or `USD`; must equal the currency |
| `amounts.currency` | no | `{"code": "USD", "aed_per_unit": "3.6725", "rate_source": "UAE dirham peg to the US dollar"}`. One fixed rate per file, rounded half-up to fils after conversion |
| `amounts.refunds` | revenue only | `reject` (default), `negative_values` (`-500.00` or `(500.00)` is a refund) or `whole_file` (every row is a refund written as a positive amount) |
| `uppercase` | no | Identifier fields to upper-case, when one system writes `meta-101` and another `META-101` |
| `include_when` | no | Keep only rows whose column holds one of these values, compared without regard to letter case: `{"column": "Status", "values": ["PAID"]}`. Excluded rows get the status `filtered`, keep their amount, and are reported as a finding |
| `columns.row_id` | advertising | The export's own row identifier (ad set ID, ad ID). Without it, two genuinely identical rows count once |
| `columns.line_id` | revenue | Declares a line-item export: the column identifying the line within an invoice (line number, SKU). Declare it in every revenue file or none |

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

## What a config cannot express

- More than one date format or currency within one file.
- Columns computed from others, such as quantity × unit price.
- Invoice status: an unpaid invoice counts as revenue if it is in the export.
- Time zones. The date is taken as written.
- Reading a workbook directly: the conversion above is a step outside the checked
  run, recorded by its manifest.
- Attribution rules other than first and last touch.

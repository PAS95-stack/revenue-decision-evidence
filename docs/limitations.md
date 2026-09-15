# Limitations and design decisions

## What this demonstrates

- Figures computed deterministically from three synthetic CSV exports, with every
  input row given one status and every figure traceable to the rows it is built
  from.
- A second, separate implementation that reconstructs every published figure
  from the raw exports; outputs are published only when the two agree.
- A narrative gate that rejects any number not equal to the evidence it cites.

Verified with 96 tests on Python 3.13. CI covers Python 3.11–3.13 and has not yet
run on these changes.

## What it does not demonstrate

- Use on real organisational data, external evaluation or paid validation.
- That advertising caused revenue. Attribution follows the CRM campaign ID; it is
  an assumption, not a measure of incrementality.
- That changing budget would improve results. The report never recommends it.

## Known limitations

**Data contract**
- AED only; no currency conversion.
- Refunds and credit notes cannot be represented: negative amounts are rejected.
- Advertising rows have no identifier, so two genuine rows with the same date,
  campaign, channel and amount count once. The excluded amount stays visible in
  `row_dispositions.csv`.
- Spend and revenue periods are not aligned: channel ROAS divides all attributed
  revenue in the files by all accepted spend.
- Column names are fixed. Real exports need a mapping step that is not built.
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
- Numbers must match exactly, so a correct but rounded figure is rejected.
- Tested with fixed text and a mocked model response. No real model output has
  been evaluated.

**Verification**
- The engine and checker implement one written contract separately, by the same
  author. A code bug in either is caught; a misreading shared by both is not.
- The checker verifies the figures and citations in findings, not the rest of
  their wording.

**Scope**
- Not a security review. The code has no access control, encryption or retention
  handling.
- The 80% coverage threshold and the 30, 60 and 90-day windows are planning
  choices, not validated values.

## Repeatability

With the same input bytes, engine version, rule set and as-of date, every output
file is byte-identical and `run_id` is the same. Changing any of them changes
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
   can each be read more than one way, and refusing is safer than guessing. The
   cost is that some exports need cleaning before a run.
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

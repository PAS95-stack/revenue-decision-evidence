# Five-minute walkthrough

Record with `scripts/demo.sh`. It pauses before each step; press Enter to
continue. Each step states the result it expects and the script stops if that
result does not happen. All runs go to a temporary folder, so the repository is
not changed.

Before recording: widen the terminal to about 120 columns, run
`scripts/demo.sh --no-pause` once to confirm it ends with `Demo complete.`, and
open `docs/limitations.md` in a second window.

Timings are targets. The spoken lines are a guide, not a script to read.

---

**0:00–0:30 — The question**

Say: "A business wants to know whether its marketing budget should move between
channels. It has three exports: ad spend, CRM leads and revenue. This tool reports
what those exports can support, and refuses to publish anything it cannot
reproduce from the raw rows. The data here is synthetic."

**0:30–0:50 — Step 1: tests**

Show `Ran 127 tests … OK`. Say: "These include attacks written before the fixes,
200 generated datasets with injected faults, and deliberately broken copies of
the engine that must be blocked."

**0:50–1:20 — Step 2: run from the original CSVs**

Point at `status: PASS` and the empty `diff`. Say: "One command from the raw
files. The output is byte-identical to what is committed, and CI checks that on
Python 3.11 to 3.13. There is no timestamp, so the same inputs give the same run
ID."

**1:20–2:00 — Step 3: row statuses**

Point at the counts: 24 rows, 13 accepted, 4 unattributed, 3 rejected,
2 duplicate, 2 conflict. Say: "Nothing is fixed silently. The duplicate
transaction counts once. LEAD-002 appears twice with different campaigns, so both
rows are excluded rather than letting file order decide. Its AED 9,000 sale still
counts as revenue, but is not credited to any channel."

**2:00–2:50 — Step 4: rebuild the figures**

Say: "Revenue is published as 56,500. Here are the seven raw rows it is built
from; they add to 56,500. Row 8 is the duplicate and row 9 the negative amount, so
neither is in the list." Then: "The independent checker re-derives every row
status and every figure from the CSVs using only Python's standard library. It
cannot import the engine. The engine publishes only if the checker agrees."

**2:50–3:40 — Step 5: make the narrative lie**

Say: "The optional AI summary goes through a gate. The true sentence passes.
65,500 is rejected, and the reason names the real value. So are 15,000 cited as
revenue evidence, the real spend figure described as revenue, an evidence ID that
does not exist, a number with no citation, and a causal claim."

**3:40–4:05 — Step 6: tamper after publishing**

Say: "If someone edits the published report to say 65,500, the checker catches
it against the raw rows."

**4:05–4:30 — Step 7: change the row order**

Say: "Swapping the two contradictory CRM rows changes nothing: every figure,
status and finding is identical. Only the run ID changes, because the file bytes
changed."

**4:30–4:45 — Step 8: missing evidence**

Say: "With no revenue rows the run fails with exit code 2, and the only file
written is the failure record. No report exists to be misread."

**4:45–5:00 — Limitations**

Show `docs/limitations.md`. Say: "What this does not show: real client data,
that advertising caused revenue, or real model output — the gate has only been
tested with a mocked model. The gate checks that numbers match the evidence
cited, not the words around them, and the engine and checker share one author's
reading of the data contract. The report never recommends moving budget; it lists
the questions to answer first, and the budget owner decides."

---

## If a question comes up

| Question | Where to show |
|---|---|
| How is a conflict different from a duplicate? | `docs/data-dictionary.md`, row statuses |
| What does the checker compare? | `evaluation_results.json`, the `checks` list |
| What if the engine itself has a bug? | `tests/test_planted_defects.py` |
| Why no budget recommendation? | `docs/limitations.md`, design decision 4 |

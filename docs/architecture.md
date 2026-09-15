# Architecture

```text
ads.csv ────┐
crm.csv ────┼─> read, keeping each record's physical start line
revenue.csv ┘                    │
                                 v
                   validate every row against the contract
                                 │
                                 v
          one status per row: accepted | accepted-unattributed |
                              rejected | duplicate | conflict
                                 │
                                 v
        figures with lineage (summand, numerator, denominator, join)
        findings and questions — never a budget move
                                 │
                                 v
        optional narrative (off by default) ─> narrative gate
                                 │
                                 v
        write to a staging folder ─> independent checker (reconstruct.py)
                                 │   re-derives every status and figure
                                 │   from the raw exports
                  ┌──────────────┴──────────────┐
                 PASS                          FAIL
                  │                             │
     publish every file, with a        publish evaluation_results.json
     SHA-256 hash of each               only; CLI exits 2
                  │
                  v
     accountable budget owner decides, outside this tool
```

## Components

| Module | Responsibility |
|---|---|
| `engine.py` | Reads exports, assigns row statuses, computes figures and lineage, builds findings and questions, writes and gates outputs |
| `reconstruct.py` | Independent checker. Standard library only; never imports the engine. Runs on its own against any output folder |
| `narrative.py` | Optional Azure OpenAI call and the gate that binds every number in a narrative to the evidence it cites |
| `cli.py` | Command line: clears previous outputs, runs, publishes, and sets the exit code |

## Why two implementations

The engine and the checker implement the same written contract separately. The
checker also finds each record's line with its own quote-aware scanner rather
than the `csv` module the engine uses. A bug in one implementation shows up as a
disagreement and blocks publication. A misreading of the contract shared by both
would not; see [`limitations.md`](limitations.md).

## Identity and repeatability

`run_id` is a hash of the engine version, the full rule-set content, the optional
as-of date and the SHA-256 of each input file. The wall clock is never read, JSON
keys are sorted, and line endings of committed files are pinned in
`.gitattributes`.

## Trust boundary

- Raw organisational records stay in the private processing environment.
- Only computed figures and their qualifiers go to the optional language model:
  no rows, row references or lineage.
- Public artifacts contain synthetic data only.
- The workflow does not connect to advertising platforms, CRMs, payment or
  accounting systems, and cannot execute a budget change.

# Architecture

```text
Approved exports (max 3)
  ads.csv -----+
  crm.csv -----+--> schema + row validation --> deterministic reconciliation
  revenue.csv -+              |                         |
                               +--> rejects/uncertainty  +--> evidence records + lineage
                                                               |
                                  +----------------------------+-------------------+
                                  |                                                |
                            executive output                               optional Azure layer
                                  |                                       computed evidence only
                                  +----------------------------+-------------------+
                                                               |
                                                    named human approval gate
```

The deterministic engine owns every authoritative figure. The optional Azure layer can explain the computed evidence, but its output is accepted only if it cites known evidence IDs, avoids causal/guarantee/autonomy claims, and states the human-approval boundary. Failure disables the narrative without changing the report.

## Trust boundary

- Raw organizational records stay in the private processing environment.
- Only computed evidence records may cross into the optional language-model layer.
- Public artifacts contain synthetic data or a separately permissioned anonymized description.
- The workflow does not connect to ad platforms, CRMs, payments, or operational systems.
- The workflow cannot execute a budget change.

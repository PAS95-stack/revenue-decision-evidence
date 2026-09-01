# Security and data boundary

Do not open an issue with customer, prospect, employee, lead, transaction, or
other personal data. This repository must contain synthetic data only.

For any permissioned organizational delivery:

- keep approved source exports outside this repository;
- use pseudonymous identifiers and the minimum necessary fields;
- restrict access and record a deletion date;
- send only computed aggregates and evidence records to an optional AI layer;
- retain the deterministic report if the AI layer is disabled; and
- require named human approval before any business action.

Report a suspected vulnerability privately to the repository owner through
GitHub's security reporting channel. Do not include real organizational data in
the report.

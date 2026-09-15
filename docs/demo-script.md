# Five-minute demonstration script

**0:00–0:35 — The decision**  
“A finance leader has to decide whether the next marketing budget can be reallocated. Three exports disagree or do not join cleanly. This workflow shows what the records can defend, what they cannot defend, and what must be measured next.”

**0:35–1:20 — The contract**  
Show the three CSVs and data dictionary. Point out that IDs are pseudonymous and the public case is synthetic.

**1:20–2:15 — Fail visibly**  
Run the command. Open `rejected_records.csv`: duplicates, invalid currency, missing IDs, conflicts, orphans, and invalid chronology are not repaired silently.

**2:15–3:10 — Trace the figures**  
Open `report.html`, then `lineage.csv`. Follow `EV-SPEND-001` and `EV-ATTR-001` back to source-row references. Explain the three evidence grades.

**3:10–4:10 — Challenge the attribution**  
Show the 30/60/90-day sensitivity. Explain that a deterministic join can still be assumption-dependent and that this is not causal incrementality.

**4:10–4:45 — Bound the AI**  
Show `narrative.py`: only computed evidence crosses the boundary. Unsupported citations, guarantee language, autonomy, or omission of human approval disables the narrative.

**4:45–5:00 — Close**  
“The output is data-quality findings and the questions to answer, never a budget move or an automated action. The named budget owner approves, rejects, or requests more evidence. The synthetic case proves reproducibility; only a permissioned external delivery can establish real-world usefulness.”

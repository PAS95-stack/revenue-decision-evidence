"""Small commands used by scripts/demo.sh to make each step visible on screen.

Only `narrative` imports the engine, because it demonstrates the engine's own
gate. The other commands read the published files and raw CSVs directly.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "synthetic"
MONEY_COLUMNS = {"ads": "spend_aed", "revenue": "value_aed"}


def statuses(output: Path) -> int:
    with (output / "row_dispositions.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    counts = Counter(row["status"] for row in rows)
    print(f"{len(rows)} input rows, each with exactly one status:")
    for status in ("accepted", "accepted-unattributed", "rejected", "duplicate", "conflict"):
        print(f"  {status:<22} {counts.get(status, 0)}")
    print("\nRows that do not count in full, with the reason recorded:")
    for row in rows:
        if row["status"] != "accepted":
            reference = f"{row['source']}:{row['source_row']}"
            print(f"  {reference:<11} {row['record_id']:<14} {row['status']:<22} {row['reason']}")
    return 0


def _raw_amount(source: str, line_number: int, data: Path) -> str:
    """Read one physical line of a raw export and return its amount column."""
    lines = (data / f"{source}.csv").read_text(encoding="utf-8-sig").splitlines()
    header = next(csv.reader([lines[0]]))
    record = lines[line_number - 1]
    if '"' in record:
        raise SystemExit(f"{source}:{line_number} is quoted; use reconstruct.py, which handles multi-line fields")
    return next(csv.reader([record]))[header.index(MONEY_COLUMNS[source])]


def trace(output: Path, evidence_id: str, data: Path) -> int:
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    claim = next((c for c in report["claims"] if c["evidence_id"] == evidence_id), None)
    if claim is None:
        raise SystemExit(f"{evidence_id} is not in the report")
    with (output / "lineage.csv").open(newline="", encoding="utf-8") as handle:
        lineage = [row for row in csv.DictReader(handle) if row["evidence_id"] == evidence_id]
    print(f"{evidence_id} ({claim['metric']}) published as {claim['value']} {claim['unit']}")
    total = Decimal("0")
    for row in lineage:
        source, line = row["source_ref"].split(":")
        if row["role"] == "summand":
            amount = _raw_amount(source, int(line), data)
            total += Decimal(amount)
            print(f"  {row['source_ref']:<11} summand  raw value {amount}")
        else:
            print(f"  {row['source_ref']:<11} {row['role']:<8} links records, adds nothing")
    matches = total == Decimal(claim["value"])
    print(f"Sum of raw source rows: {total:.2f}  ->  {'MATCH' if matches else 'MISMATCH'}")
    return 0 if matches else 1


def narrative(text: str, data: Path) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from revenue_evidence.engine import EvidenceEngine
    from revenue_evidence.narrative import validate_narrative

    report = EvidenceEngine().run(data / "ads.csv", data / "crm.csv", data / "revenue.csv")
    valid, reason = validate_narrative(text, report)
    print(f"{'ACCEPTED' if valid else 'REJECTED'}: {text}")
    if not valid:
        print(f"  reason: {reason}")
    return 0 if valid else 1


def compare(first: Path, second: Path) -> int:
    reports = [json.loads((folder / "report.json").read_text(encoding="utf-8")) for folder in (first, second)]
    # These two fields identify the input bytes, so they must differ when rows move.
    for report in reports:
        report.pop("run_id")
        report.pop("source_fingerprints")
    differing = sorted(key for key in reports[0] if reports[0][key] != reports[1].get(key))
    if differing:
        print(f"Results differ in: {', '.join(differing)}")
        return 1
    print("Every figure, row status, lineage entry, finding and question is identical.")
    print("Only the input fingerprint and run_id differ, because the file bytes differ.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA, help="Folder holding ads.csv, crm.csv and revenue.csv")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("statuses").add_argument("output", type=Path)
    trace_parser = commands.add_parser("trace")
    trace_parser.add_argument("output", type=Path)
    trace_parser.add_argument("evidence_id")
    commands.add_parser("narrative").add_argument("text")
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("first", type=Path)
    compare_parser.add_argument("second", type=Path)
    args = parser.parse_args()
    if args.command == "statuses":
        return statuses(args.output)
    if args.command == "trace":
        return trace(args.output, args.evidence_id, args.data)
    if args.command == "narrative":
        return narrative(args.text, args.data)
    return compare(args.first, args.second)


if __name__ == "__main__":
    raise SystemExit(main())

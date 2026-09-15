from __future__ import annotations

import argparse
import json
import sys

from .engine import EvidenceEngine, InputContractError, clear_outputs, write_outputs
from .narrative import generate_azure_narrative


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a traceable revenue evidence report")
    parser.add_argument("--ads", required=True)
    parser.add_argument("--crm", required=True)
    parser.add_argument("--revenue", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--as-of",
        help="Optional reporting date (YYYY-MM-DD) recorded in the report; the clock is never read",
    )
    parser.add_argument(
        "--azure-narrative",
        action="store_true",
        help="Send computed evidence records only to a configured Azure OpenAI deployment",
    )
    args = parser.parse_args()
    # Stale outputs go first, so no earlier brief survives a run that stops early.
    clear_outputs(args.output)
    try:
        report = EvidenceEngine().run(args.ads, args.crm, args.revenue, as_of=args.as_of)
    except InputContractError as exc:
        print(json.dumps({"status": "FAIL", "failures": [f"inputs: {exc}"]}, indent=2), file=sys.stderr)
        return 2
    if args.azure_narrative:
        report.narrative = generate_azure_narrative(report)
    evaluation = write_outputs(report, args.output)
    result = {
        "run_id": report.run_id,
        "engine_version": report.engine_version,
        "status": evaluation["status"],
        "output": args.output,
    }
    if evaluation["status"] != "PASS":
        print(json.dumps({**result, "failures": evaluation["failures"]}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

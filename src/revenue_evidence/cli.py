from __future__ import annotations

import argparse
import json

from .engine import EvidenceEngine, write_outputs
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
    report = EvidenceEngine().run(args.ads, args.crm, args.revenue, as_of=args.as_of)
    if args.azure_narrative:
        report.narrative = generate_azure_narrative(report)
    write_outputs(report, args.output)
    print(
        json.dumps(
            {"run_id": report.run_id, "engine_version": report.engine_version, "output": args.output},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import (
    EvidenceEngine,
    InputContractError,
    clear_outputs,
    ensure_outside_repository,
    load_engagement,
    write_outputs,
)
from .narrative import generate_azure_narrative

# The checkout this module runs from, when it runs from one. Client data may not sit inside it.
REPOSITORY = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a traceable revenue evidence report")
    parser.add_argument("--ads", help="Advertising export in the three-file contract")
    parser.add_argument("--crm", help="CRM export in the three-file contract")
    parser.add_argument("--revenue", help="Revenue export in the three-file contract")
    parser.add_argument(
        "--config",
        help="Engagement config declaring the export files, their columns and formats (see docs/engagements.md); "
        "replaces --ads, --crm and --revenue",
    )
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
    parser.add_argument(
        "--model-provider-permission-recorded",
        action="store_true",
        help="Confirms the client's written permission to send computed figures to the model provider; "
        "required with --azure-narrative for client data",
    )
    args = parser.parse_args(argv)
    plain = [args.ads, args.crm, args.revenue]
    if args.config and any(plain):
        parser.error("use either --config or --ads, --crm and --revenue, not both")
    if not args.config and not all(plain):
        parser.error("--ads, --crm and --revenue are required unless --config is given")
    # Stale outputs go first, so no earlier brief survives a run that stops early.
    clear_outputs(args.output)
    try:
        if args.config:
            engagement = load_engagement(args.config)
            if engagement.data_origin == "client":
                exports = [spec.path for specs in engagement.files.values() for spec in specs]
                ensure_outside_repository([engagement.config_path, *exports, Path(args.output)], REPOSITORY)
                if args.azure_narrative and not args.model_provider_permission_recorded:
                    raise InputContractError(
                        "the AI narrative sends computed client figures to a model provider; record the client's "
                        "written permission first and add --model-provider-permission-recorded"
                    )
            report = EvidenceEngine().run_engagement(engagement, as_of=args.as_of)
        else:
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

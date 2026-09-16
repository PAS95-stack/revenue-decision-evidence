#!/usr/bin/env python3
"""Set up, log, run and close an engagement folder for real client exports.

    python3 scripts/engagement.py init ~/engagements/acme-2026-09 --name "Acme clinics, September review"
    python3 scripts/engagement.py intake ~/engagements/acme-2026-09 --received-on 2026-09-16 --received-from "Finance manager"
    python3 scripts/engagement.py propose ~/engagements/acme-2026-09
    python3 scripts/engagement.py run ~/engagements/acme-2026-09
    python3 scripts/engagement.py deletion-check ~/engagements/acme-2026-09

The folder must sit outside this repository. A run refuses exports that were not
logged at intake or that changed afterwards. Nothing here deletes files: deletion is
done, and recorded, by the person responsible for it.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence import cli  # noqa: E402
from revenue_evidence.engine import InputContractError, ensure_outside_repository, load_engagement  # noqa: E402
from revenue_evidence.propose import write_proposal  # noqa: E402

CONFIG_NAME = "engagement.json"
INTAKE_FIELDS = ["file", "sha256", "bytes", "received_on", "received_from"]
ACCESS_FIELDS = ["date", "person", "file_or_folder", "purpose"]


def _template(name: str) -> dict:
    """A config to complete from the client's real column names. It fails to load until it is completed."""
    return {
        "config_version": 1,
        "engagement": name,
        "data_origin": "client",
        "attribution_windows_days": [30, 60, 90],
        "channel_aliases": {},
        "revenue_join": "lead_id",
        "sources": {
            "ads": [
                {
                    "file": "ADVERTISING_EXPORT.csv",
                    "columns": {"date": "DATE COLUMN", "campaign_id": "CAMPAIGN ID COLUMN", "channel": "CHANNEL COLUMN",
                                "spend_aed": "COST COLUMN"},
                    "date_format": "YYYY-MM-DD",
                    "amounts": {"thousands_separator": ""},
                }
            ],
            "crm": [
                {
                    "file": "CRM_EXPORT.csv",
                    "columns": {"lead_id": "LEAD ID COLUMN", "campaign_id": "CAMPAIGN COLUMN", "created_at": "CREATED DATE COLUMN",
                                "status": "STATUS COLUMN"},
                    "date_format": "YYYY-MM-DD",
                }
            ],
            "revenue": [
                {
                    "file": "REVENUE_EXPORT.csv",
                    "columns": {"transaction_id": "INVOICE NUMBER COLUMN", "lead_id": "LEAD ID COLUMN", "value_aed": "TOTAL COLUMN",
                                "date": "INVOICE DATE COLUMN"},
                    "date_format": "YYYY-MM-DD",
                    "amounts": {"thousands_separator": "", "refunds": "reject"},
                }
            ],
        },
    }


DELETION_RECORD = """# Deletion record

Engagement: {name}

Complete this after the agreed deletion date. Run `deletion-check` first: it lists
every export received and confirms none remains in this folder.

- Deleted on:
- Deleted by:
- Method (including backups and downloaded copies):
- Files deleted: paste the list printed by `deletion-check`
- Confirmation sent to the client on:
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exports(folder: Path) -> list[Path]:
    inputs = folder / "inputs"
    return sorted(path for path in inputs.rglob("*") if path.is_file() and path.name != CONFIG_NAME)


def _intake_log(folder: Path) -> dict[str, dict]:
    path = folder / "records" / "intake_log.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["file"]: row for row in csv.DictReader(handle)}


def init(folder: Path, name: str, repository: Path) -> int:
    ensure_outside_repository([folder], repository)
    if folder.exists() and any(folder.iterdir()):
        print(f"{folder} already contains files; choose a new folder for each engagement.", file=sys.stderr)
        return 2
    for part in ("inputs", "outputs", "records"):
        (folder / part).mkdir(parents=True, exist_ok=True)
    (folder / "inputs" / CONFIG_NAME).write_text(json.dumps(_template(name), indent=2) + "\n", encoding="utf-8")
    for filename, fields in (("intake_log.csv", INTAKE_FIELDS), ("access_log.csv", ACCESS_FIELDS)):
        with (folder / "records" / filename).open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(fields)
    (folder / "records" / "deletion_record.md").write_text(DELETION_RECORD.format(name=name), encoding="utf-8")
    print(f"Created {folder}. Put the approved exports in inputs/, log them with intake, "
          f"then run propose to draft inputs/{CONFIG_NAME} from the exports and check it.")
    return 0


def propose(folder: Path, name: str | None, data_origin: str) -> int:
    """Draft a config from the exports themselves, with the evidence for every choice."""
    inputs = folder / "inputs"
    if not inputs.is_dir():
        print(f"{inputs} does not exist; run init first", file=sys.stderr)
        return 2
    existing = inputs / CONFIG_NAME
    if name is None and existing.exists():
        try:
            name = json.loads(existing.read_text(encoding="utf-8")).get("engagement")
        except ValueError:
            name = None
    try:
        draft, notes = write_proposal(inputs, name or folder.name, data_origin)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(notes.read_text(encoding="utf-8"))
    print(f"Draft: {draft}\nCheck it, rename it to {CONFIG_NAME}, then run.")
    return 0


def intake(folder: Path, received_on: str, received_from: str) -> int:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", received_on):
        print("--received-on must be YYYY-MM-DD", file=sys.stderr)
        return 2
    logged = _intake_log(folder)
    changed = [
        name for name, row in logged.items()
        if (folder / "inputs" / name).exists() and _sha256(folder / "inputs" / name) != row["sha256"]
    ]
    if changed:
        print(f"Changed since intake: {', '.join(changed)}. Log a new copy under a new file name instead.", file=sys.stderr)
        return 2
    added = []
    with (folder / "records" / "intake_log.csv").open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INTAKE_FIELDS)
        for path in _exports(folder):
            name = path.relative_to(folder / "inputs").as_posix()
            if name in logged:
                continue
            writer.writerow({"file": name, "sha256": _sha256(path), "bytes": path.stat().st_size,
                             "received_on": received_on, "received_from": received_from})
            added.append(name)
    print(f"Logged {len(added)} new export(s): {', '.join(added) or 'none'}.")
    return 0


def run(folder: Path, as_of: str | None, repository: Path) -> int:
    config = folder / "inputs" / CONFIG_NAME
    try:
        engagement = load_engagement(config)
        ensure_outside_repository([folder], repository)
    except InputContractError as exc:
        print(f"inputs: {exc}", file=sys.stderr)
        return 2
    logged = _intake_log(folder)
    problems = []
    for specs in engagement.files.values():
        for spec in specs:
            row = logged.get(spec.label)
            if row is None:
                problems.append(f"{spec.label} was not logged at intake")
            elif _sha256(spec.path) != row["sha256"]:
                problems.append(f"{spec.label} changed since intake")
    if problems:
        print("Refusing to run: " + "; ".join(problems) + ".", file=sys.stderr)
        return 2
    arguments = ["--config", str(config), "--output", str(folder / "outputs")]
    if as_of:
        arguments += ["--as-of", as_of]
    return cli.main(arguments)


def deletion_check(folder: Path) -> int:
    remaining = [path for part in ("inputs", "outputs") for path in sorted((folder / part).rglob("*")) if path.is_file()]
    if remaining:
        print("Still present:")
        for path in remaining:
            print(f"  {path.relative_to(folder).as_posix()}")
        return 1
    print("No exports, config or outputs remain. Exports received (from records/intake_log.csv):")
    for row in _intake_log(folder).values():
        print(f"  {row['file']}  sha256 {row['sha256']}  received {row['received_on']} from {row['received_from']}")
    return 0


def main(argv: list[str] | None = None, repository: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("init", help="create an engagement folder outside the repository")
    start.add_argument("folder", type=Path)
    start.add_argument("--name", required=True, help="engagement name shown in the brief")
    draft = commands.add_parser("propose", help="read the exports and draft a config, with the evidence for each choice")
    draft.add_argument("folder", type=Path)
    draft.add_argument("--name", help="engagement name; taken from an existing config, or the folder name")
    draft.add_argument("--origin", default="client", choices=("client", "public", "synthetic"))
    log = commands.add_parser("intake", help="record the SHA-256 of every new export in inputs/")
    log.add_argument("folder", type=Path)
    log.add_argument("--received-on", required=True)
    log.add_argument("--received-from", required=True)
    execute = commands.add_parser("run", help="run the logged exports and publish only if the checker agrees")
    execute.add_argument("folder", type=Path)
    execute.add_argument("--as-of")
    close = commands.add_parser("deletion-check", help="confirm no exports or outputs remain")
    close.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    folder = args.folder.expanduser()
    try:
        if args.command == "init":
            return init(folder, args.name, repository)
        if args.command == "propose":
            return propose(folder, args.name, args.origin)
        if args.command == "intake":
            return intake(folder, args.received_on, args.received_from)
        if args.command == "run":
            return run(folder, args.as_of, repository)
        return deletion_check(folder)
    except InputContractError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"{exc.filename} does not exist; run init first", file=sys.stderr)
        return 2


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        raise SystemExit(main())

#!/usr/bin/env python3
"""Independent reconstruction of a revenue evidence report.

This file deliberately re-implements the data contract instead of importing the
engine. It reads the three raw CSV exports, derives every row's status and every
published figure from scratch, and compares the result with what the engine
wrote. Agreement between two separate implementations is the evidence; a check
that reused the engine's own code would repeat the engine's bugs.

Standard library only. It must never import revenue_evidence (a test enforces
this), so it can also be run on its own against published outputs:

    python3 src/revenue_evidence/reconstruct.py --ads ads.csv --crm crm.csv \
        --revenue revenue.csv --output outputs/run

Contract choices applied here independently; any disagreement fails verification:
  * amounts are plain non-negative decimals such as "1000" or "1000.50", rounded
    half-up to fils; exponents, signs and thousands separators are not amounts;
  * dates are YYYY-MM-DD;
  * rows sharing an identifier are one record only when identical (the earliest
    is kept, later copies are duplicates); otherwise every copy is a conflict;
  * percentages round half-even to 0.1 and ratios half-even to 0.01.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

CENT = Decimal("0.01")
PLAIN_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DOWNSTREAM_CHECKS = ("claim_values", "claim_lineage", "summary", "channels_and_sensitivity", "executive_brief")
CITATION = re.compile(r"\[(EV-[A-Z]+-\d{3})\]")
SOURCES = ("ads", "crm", "revenue")
REQUIRED = {
    "ads": ("date", "campaign_id", "channel", "spend_aed"),
    "crm": ("lead_id", "campaign_id", "created_at", "status"),
    "revenue": ("transaction_id", "lead_id", "value_aed", "date"),
}
STATUSES = ("accepted", "accepted-unattributed", "rejected", "duplicate", "conflict")
CHECKS = (
    "sources_match_fingerprints",
    "ruleset_fingerprint_and_run_id",
    "row_dispositions",
    "rejected_records",
    "claim_values",
    "claim_lineage",
    "summary",
    "channels_and_sensitivity",
    "executive_brief",
    "sufficient_evidence",
)
MAX_DETAILS = 5


class ReconstructionError(Exception):
    """Inputs or outputs cannot be read well enough to compare."""


def _text(row: dict, field: str) -> str:
    value = row.get(field)
    return value.strip() if isinstance(value, str) else ""


def _amount(text: str) -> Decimal | None:
    if not PLAIN_DECIMAL.fullmatch(text):
        return None
    try:
        return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _day(text: str) -> date | None:
    if not ISO_DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _limited(items) -> str:
    items = sorted(str(item) for item in items)
    shown = ", ".join(items[:MAX_DETAILS])
    return shown + (f" and {len(items) - MAX_DETAILS} more" if len(items) > MAX_DETAILS else "")


def _record_start_lines(text: str) -> list[int]:
    """Physical start line of every non-blank CSV record, found without the csv module.

    A small quote-aware scanner: newlines inside a quoted field continue the
    record, "\\r\\n", "\\r" and "\\n" each end one physical line, and an empty line
    is not a record. The engine derives the same numbers from the csv module's
    reader position; two methods agreeing is what makes a reference trustworthy.
    """
    starts: list[int] = []
    line = 1
    record_line = 1
    empty = True
    state = "field_start"
    index = 0
    while index < len(text):
        char = text[index]
        crlf = text.startswith("\r\n", index)
        if char in "\r\n" and state != "quoted":
            if not empty:
                starts.append(record_line)
            index += 2 if crlf else 1
            line += 1
            record_line, empty, state = line, True, "field_start"
            continue
        empty = False
        if state == "field_start":
            state = "quoted" if char == '"' else "field_start" if char == "," else "unquoted"
        elif state == "unquoted":
            if char == ",":
                state = "field_start"
        elif state == "quoted":
            if char == '"':
                state = "closing_quote"
            elif char in "\r\n":
                line += 1
                if crlf:
                    index += 1
        elif state == "closing_quote":
            state = "quoted" if char == '"' else "field_start" if char == "," else "unquoted"
        index += 1
    if not empty:
        starts.append(record_line)
    return starts


def _read_rows(path, required) -> list[tuple[int, dict]]:
    name = Path(path).name
    try:
        text = Path(path).read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise ReconstructionError(f"cannot read {name} ({type(exc).__name__})") from exc
    except UnicodeDecodeError as exc:
        raise ReconstructionError(f"{name} is not UTF-8 text") from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fieldnames = reader.fieldnames
        rows = [dict(row) for row in reader]
    except csv.Error as exc:
        raise ReconstructionError(f"{name} is not a readable CSV ({exc})") from exc
    missing = set(required) - set(fieldnames or [])
    if missing:
        raise ReconstructionError(f"{name} is missing required columns: {', '.join(sorted(missing))}")
    starts = _record_start_lines(text)
    if len(starts) != len(rows) + 1:
        raise ReconstructionError(f"cannot align the records of {name} to physical lines")
    return list(zip(starts[1:], rows))


def _read_csv_output(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError as exc:
        raise ReconstructionError(f"cannot read {path.name} ({type(exc).__name__})") from exc


def _resolve(items: list[dict], key: str, signature, status: dict) -> tuple[list[dict], dict[str, list[str]]]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item[key], []).append(item)
    kept: list[dict] = []
    conflicts: dict[str, list[str]] = {}
    for identifier, members in groups.items():
        members.sort(key=lambda item: item["row"])
        if len({signature(item) for item in members}) > 1:
            conflicts[identifier] = [item["ref"] for item in members]
            for item in members:
                status[item["ref"]] = "conflict"
            continue
        kept.append(members[0])
        status[members[0]["ref"]] = "accepted"
        for item in members[1:]:
            status[item["ref"]] = "duplicate"
    kept.sort(key=lambda item: item["row"])
    return kept, conflicts


def derive(sources: dict, windows: list[int]) -> dict:
    """Every row status and every published figure, from the raw exports alone."""
    raw = {name: _read_rows(sources[name], REQUIRED[name]) for name in SOURCES}
    status: dict[str, str] = {}
    amount: dict[str, str] = {}

    valid_ads = []
    for number, row in raw["ads"]:
        ref = f"ads:{number}"
        campaign, channel = _text(row, "campaign_id"), _text(row, "channel")
        day, spend = _day(_text(row, "date")), _amount(_text(row, "spend_aed"))
        amount[ref] = ""
        if not campaign or not channel or any(mark in campaign + channel for mark in "[]") or day is None or spend is None:
            status[ref] = "rejected"
            continue
        amount[ref] = _money(spend)
        valid_ads.append({"ref": ref, "row": number, "date": day, "campaign": campaign, "channel": channel, "spend": spend})
    channels_by_campaign: dict[str, set] = {}
    for item in valid_ads:
        channels_by_campaign.setdefault(item["campaign"], set()).add(item["channel"])
    ads, seen = [], set()
    for item in valid_ads:
        if len(channels_by_campaign[item["campaign"]]) > 1:
            status[item["ref"]] = "conflict"
            continue
        signature = (item["date"], item["campaign"], item["channel"], item["spend"])
        if signature in seen:
            status[item["ref"]] = "duplicate"
            continue
        seen.add(signature)
        status[item["ref"]] = "accepted"
        ads.append(item)

    valid_crm = []
    for number, row in raw["crm"]:
        ref = f"crm:{number}"
        amount[ref] = ""
        lead, campaign = _text(row, "lead_id"), _text(row, "campaign_id")
        created, stage = _day(_text(row, "created_at")), _text(row, "status").lower()
        if not lead or not campaign or created is None or not stage:
            status[ref] = "rejected"
            continue
        valid_crm.append({"ref": ref, "row": number, "lead": lead, "campaign": campaign, "created": created, "stage": stage})
    crm, conflicting_leads = _resolve(
        valid_crm, "lead", lambda item: (item["campaign"], item["created"], item["stage"]), status
    )

    valid_revenue = []
    for number, row in raw["revenue"]:
        ref = f"revenue:{number}"
        amount[ref] = ""
        transaction, lead = _text(row, "transaction_id"), _text(row, "lead_id")
        value, day = _amount(_text(row, "value_aed")), _day(_text(row, "date"))
        if not transaction or not lead or value is None or day is None:
            status[ref] = "rejected"
            continue
        amount[ref] = _money(value)
        valid_revenue.append({"ref": ref, "row": number, "transaction": transaction, "lead": lead, "value": value, "date": day})
    revenue, _ = _resolve(
        valid_revenue, "transaction", lambda item: (item["lead"], item["value"], item["date"]), status
    )

    leads = {item["lead"]: item for item in crm}
    campaign_channel = {item["campaign"]: item["channel"] for item in ads}
    attributed, unattributed, conflicted = [], [], []
    for item in revenue:
        lead = leads.get(item["lead"])
        channel = campaign_channel.get(lead["campaign"]) if lead else None
        if lead is None or channel is None or item["date"] < lead["created"]:
            status[item["ref"]] = "accepted-unattributed"
            unattributed.append(item)
            if lead is None and item["lead"] in conflicting_leads:
                conflicted.append(item)
            continue
        attributed.append(
            {**item, "channel": channel, "crm_ref": lead["ref"], "delay": (item["date"] - lead["created"]).days}
        )

    def total(items, key):
        return sum((item[key] for item in items), Decimal("0"))

    def refs(items):
        return {item["ref"] for item in items}

    def joins(items):
        return {item["crm_ref"] for item in items}

    claims: dict[str, dict] = {}

    def claim(evidence_id, value, unit, **roles):
        claims[evidence_id] = {
            "value": value,
            "unit": unit,
            "roles": {role: set(members) for role, members in roles.items() if members},
        }

    spend_total, revenue_total = total(ads, "spend"), total(revenue, "value")
    attributed_total = total(attributed, "value")
    coverage = Decimal("0") if revenue_total == 0 else attributed_total / revenue_total
    coverage_text = str((coverage * 100).quantize(Decimal("0.1")))
    claim("EV-SPEND-001", _money(spend_total), "AED", summand=refs(ads))
    claim("EV-REV-001", _money(revenue_total), "AED", summand=refs(revenue))
    claim("EV-ATTR-001", _money(attributed_total), "AED", summand=refs(attributed), join=joins(attributed))
    claim(
        "EV-COVER-001", coverage_text, "percent",
        numerator=refs(attributed), denominator=refs(revenue), join=joins(attributed),
    )
    claim("EV-UNMATCH-001", _money(total(unattributed, "value")), "AED", summand=refs(unattributed))
    claim(
        "EV-CONFLICT-001", _money(total(conflicted, "value")), "AED",
        summand=refs(conflicted),
        join={ref for item in conflicted for ref in conflicting_leads[item["lead"]]},
    )

    channels = []
    for index, name in enumerate(sorted({item["channel"] for item in ads}), start=1):
        ids = {
            "spend": f"EV-CHSPEND-{index:03d}",
            "attributed_revenue": f"EV-CHATTR-{index:03d}",
            "roas": f"EV-CHROAS-{index:03d}",
        }
        channel_ads = [item for item in ads if item["channel"] == name]
        channel_attributed = [item for item in attributed if item["channel"] == name]
        spend, income = total(channel_ads, "spend"), total(channel_attributed, "value")
        roas = str((Decimal("0") if spend == 0 else income / spend).quantize(CENT))
        claim(ids["spend"], _money(spend), "AED", summand=refs(channel_ads))
        claim(ids["attributed_revenue"], _money(income), "AED", summand=refs(channel_attributed), join=joins(channel_attributed))
        claim(
            ids["roas"], roas, "ratio",
            numerator=refs(channel_attributed), denominator=refs(channel_ads), join=joins(channel_attributed),
        )
        channels.append(
            {
                "channel": name,
                "spend_aed": _money(spend),
                "attributed_revenue_aed": _money(income),
                "assumption_dependent_roas": roas,
                "evidence_ids": ids,
            }
        )

    sensitivity = []
    for window in windows:
        ids = {"attributed": f"EV-WINATTR-{window:03d}", "excluded": f"EV-WINEXCL-{window:03d}"}
        inside = [item for item in attributed if item["delay"] <= window]
        outside = [item for item in attributed if item["delay"] > window]
        claim(ids["attributed"], _money(total(inside, "value")), "AED", summand=refs(inside), join=joins(inside))
        claim(ids["excluded"], _money(total(outside, "value")), "AED", summand=refs(outside), join=joins(outside))
        sensitivity.append(
            {
                "attribution_window_days": window,
                "attributed_revenue_aed": _money(total(inside, "value")),
                "excluded_delayed_revenue_aed": _money(total(outside, "value")),
                "evidence_ids": ids,
            }
        )

    counts = {name: {"input": len(raw[name]), **{value: 0 for value in STATUSES}} for name in SOURCES}
    for ref, value in status.items():
        counts[ref.split(":", 1)[0]][value] += 1
    summary = {
        "accepted_spend_aed": _money(spend_total),
        "accepted_revenue_aed": _money(revenue_total),
        "attributed_revenue_aed": _money(attributed_total),
        "unattributed_or_invalid_revenue_aed": _money(total(unattributed, "value")),
        "conflicting_lead_revenue_aed": _money(total(conflicted, "value")),
        "attribution_coverage_percent": coverage_text,
        "accepted_rows": {"ads": len(ads), "crm": len(crm), "revenue": len(revenue)},
        "row_status_counts": counts,
        "rejected_or_uncertain_rows": sum(1 for value in status.values() if value != "accepted"),
    }
    return {
        "status": status,
        "amount": amount,
        "claims": claims,
        "channels": channels,
        "sensitivity": sensitivity,
        "summary": summary,
    }


def _role_sets(entries) -> dict[str, set]:
    roles: dict[str, set] = {}
    for entry in entries:
        roles.setdefault(entry["role"], set()).add(entry["source_ref"])
    return roles


def verify_outputs(source_paths: dict | None, output_dir) -> tuple[list[str], list[str]]:
    """Return the checks run and a list of failures; an empty list means every check passed."""
    output = Path(output_dir)
    failures: list[str] = []

    def fail(check: str, message: str) -> None:
        failures.append(f"{check}: {message}")

    try:
        if not source_paths or any(not source_paths.get(name) for name in SOURCES):
            raise ReconstructionError("source CSV paths were not supplied")
        try:
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReconstructionError(f"cannot read report.json ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise ReconstructionError("report.json is not valid JSON") from exc

        for name in SOURCES:
            try:
                actual = hashlib.sha256(Path(source_paths[name]).read_bytes()).hexdigest()
            except OSError as exc:
                raise ReconstructionError(f"cannot read the {name} export ({type(exc).__name__})") from exc
            if report.get("source_fingerprints", {}).get(name) != actual:
                fail("sources_match_fingerprints", f"the {name} export changed since the report was computed")

        ruleset = dict(report.get("ruleset", {}))
        declared = ruleset.pop("fingerprint", None)
        computed = hashlib.sha256(json.dumps(ruleset, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if declared != computed:
            fail("ruleset_fingerprint_and_run_id", "the published rule set does not match its fingerprint")
        identity = "\n".join(
            [f"engine:{report.get('engine_version')}", f"ruleset:{computed}", f"as_of:{report.get('as_of') or ''}"]
            + [f"{name}:{digest}" for name, digest in sorted(report.get("source_fingerprints", {}).items())]
        )
        if report.get("run_id") != hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]:
            fail("ruleset_fingerprint_and_run_id", "run_id does not identify these inputs, engine and rule set")
        windows = ruleset.get("attribution_windows_days")
        if not isinstance(windows, list) or not windows or not all(isinstance(w, int) and w > 0 for w in windows):
            raise ReconstructionError("the rule set does not declare valid attribution windows")

        expected = derive(source_paths, windows)

        dispositions = _read_csv_output(output / "row_dispositions.csv")
        seen_refs = [f"{row['source']}:{row['source_row']}" for row in dispositions]
        duplicated = {ref for ref in seen_refs if seen_refs.count(ref) > 1}
        if duplicated:
            fail("row_dispositions", f"rows listed more than once: {_limited(duplicated)}")
        missing = set(expected["status"]) - set(seen_refs)
        unexpected = set(seen_refs) - set(expected["status"])
        if missing:
            fail("row_dispositions", f"input rows without a status: {_limited(missing)}")
        if unexpected:
            fail("row_dispositions", f"statuses for rows not in the exports: {_limited(unexpected)}")
        wrong_status = [
            f"{ref} is {row['status']}, expected {expected['status'][ref]}"
            for ref, row in zip(seen_refs, dispositions)
            if ref in expected["status"] and row["status"] != expected["status"][ref]
        ]
        if wrong_status:
            fail("row_dispositions", f"status disagrees: {_limited(wrong_status)}")
        wrong_amount = [
            f"{ref} shows '{row['amount_aed']}', expected '{expected['amount'][ref]}'"
            for ref, row in zip(seen_refs, dispositions)
            if ref in expected["amount"] and row["amount_aed"] != expected["amount"][ref]
        ]
        if wrong_amount:
            fail("row_dispositions", f"amount disagrees: {_limited(wrong_amount)}")

        rejected_rows = _read_csv_output(output / "rejected_records.csv")
        published_rejected = {(f"{row['source']}:{row['source_row']}", row["status"]) for row in rejected_rows}
        expected_rejected = {(ref, value) for ref, value in expected["status"].items() if value != "accepted"}
        if published_rejected != expected_rejected:
            difference = published_rejected ^ expected_rejected
            fail("rejected_records", f"rejected_records.csv disagrees on: {_limited(ref for ref, _ in difference)}")

        published = {claim["evidence_id"]: claim for claim in report.get("claims", [])}
        for evidence_id in sorted(set(expected["claims"]) - set(published)):
            fail("claim_values", f"{evidence_id} is missing from the report")
        for evidence_id in sorted(set(published) - set(expected["claims"])):
            fail("claim_values", f"{evidence_id} is published but cannot be reconstructed")
        for evidence_id, want in expected["claims"].items():
            have = published.get(evidence_id)
            if have is None:
                continue
            if (have.get("value"), have.get("unit")) != (want["value"], want["unit"]):
                fail(
                    "claim_values",
                    f"{evidence_id} is {have.get('value')} {have.get('unit')}, reconstructed {want['value']} {want['unit']}",
                )
            if _role_sets(have.get("lineage", [])) != want["roles"]:
                fail("claim_lineage", f"{evidence_id} lineage in report.json does not match its reconstructed rows")

        lineage_rows = _read_csv_output(output / "lineage.csv")
        by_claim: dict[str, list] = {}
        for row in lineage_rows:
            by_claim.setdefault(row["evidence_id"], []).append(row)
        for evidence_id in sorted(set(by_claim) | set(expected["claims"])):
            want = expected["claims"].get(evidence_id, {}).get("roles", {})
            if _role_sets(by_claim.get(evidence_id, [])) != want:
                fail("claim_lineage", f"{evidence_id} lineage in lineage.csv does not match its reconstructed rows")

        supports_expected: dict[str, set] = {}
        for row in lineage_rows:
            supports_expected.setdefault(row["source_ref"], set()).add(f"{row['evidence_id']}:{row['role']}")
        wrong_supports = [
            ref
            for ref, row in zip(seen_refs, dispositions)
            if set(filter(None, row.get("supports", "").split(";"))) != supports_expected.get(ref, set())
        ]
        if wrong_supports:
            fail("row_dispositions", f"supports column disagrees with lineage.csv for: {_limited(wrong_supports)}")

        if report.get("summary") != expected["summary"]:
            keys = sorted(
                key
                for key in set(report.get("summary", {})) | set(expected["summary"])
                if report.get("summary", {}).get(key) != expected["summary"].get(key)
            )
            fail("summary", f"summary disagrees on: {_limited(keys)}")

        channel_fields = ("channel", "spend_aed", "attributed_revenue_aed", "assumption_dependent_roas", "evidence_ids")
        published_channels = [{field: row.get(field) for field in channel_fields} for row in report.get("channels", [])]
        if published_channels != expected["channels"]:
            fail("channels_and_sensitivity", "channel figures in report.json do not match reconstruction")
        window_fields = ("attribution_window_days", "attributed_revenue_aed", "excluded_delayed_revenue_aed", "evidence_ids")
        published_windows = [{field: row.get(field) for field in window_fields} for row in report.get("sensitivity", [])]
        if published_windows != expected["sensitivity"]:
            fail("channels_and_sensitivity", "sensitivity figures in report.json do not match reconstruction")

        brief_path = output / "executive_brief.md"
        brief = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
        if not brief:
            fail("executive_brief", "executive_brief.md is missing or empty")
        for evidence_id, want in expected["claims"].items():
            if brief and f"[{evidence_id}]" not in brief:
                fail("executive_brief", f"the brief omits {evidence_id}")
            elif brief and want["value"] not in brief:
                fail("executive_brief", f"the brief does not show {want['value']} for {evidence_id}")
        for finding in report.get("recommendation", {}).get("findings", []):
            for evidence_id in CITATION.findall(finding):
                want = expected["claims"].get(evidence_id)
                if want is None:
                    fail("executive_brief", f"a finding cites {evidence_id}, which cannot be reconstructed")
                elif want["value"] not in finding:
                    fail("executive_brief", f"a finding citing {evidence_id} does not state {want['value']}")

        counts = expected["summary"]["row_status_counts"]
        for name in SOURCES:
            usable = counts[name]["accepted"] + counts[name]["accepted-unattributed"]
            if usable == 0:
                breakdown = ", ".join(f"{counts[name][value]} {value}" for value in STATUSES if counts[name][value])
                fail(
                    "sufficient_evidence",
                    f"the {name} export has no usable rows ({breakdown or 'no data rows'})",
                )
    except ReconstructionError as exc:
        failures.append(f"inputs: {exc}")

    # One wrong row status changes every figure built on it. List the row-level
    # disagreements in full and summarise what follows from them, so the cause is
    # not buried under dozens of consequences.
    if any(failure.startswith("row_dispositions: status disagrees") for failure in failures):
        collapsed: dict[str, int] = {}
        kept = []
        for failure in failures:
            check = failure.split(":", 1)[0]
            if check in DOWNSTREAM_CHECKS:
                collapsed[check] = collapsed.get(check, 0) + 1
            else:
                kept.append(failure)
        failures = kept + [
            f"{check}: {count} discrepancies follow from the row status disagreements above"
            for check, count in collapsed.items()
        ]
    return list(CHECKS), failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independently reconstruct and verify a revenue evidence report")
    parser.add_argument("--ads", required=True)
    parser.add_argument("--crm", required=True)
    parser.add_argument("--revenue", required=True)
    parser.add_argument("--output", required=True, help="Directory containing the published outputs")
    args = parser.parse_args(argv)
    checks, failures = verify_outputs({"ads": args.ads, "crm": args.crm, "revenue": args.revenue}, args.output)
    print(json.dumps({"status": "FAIL" if failures else "PASS", "checks": checks, "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

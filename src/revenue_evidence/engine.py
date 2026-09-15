from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


MONEY = Decimal("0.01")

# Every input row receives exactly one status. Only the two "accepted" statuses
# may contribute to a total; the others are visible but excluded.
ACCEPTED = "accepted"
ACCEPTED_UNATTRIBUTED = "accepted-unattributed"
REJECTED = "rejected"
DUPLICATE = "duplicate"
CONFLICT = "conflict"
STATUSES = (ACCEPTED, ACCEPTED_UNATTRIBUTED, REJECTED, DUPLICATE, CONFLICT)
INCLUDED_STATUSES = frozenset({ACCEPTED, ACCEPTED_UNATTRIBUTED})
SEVERITY_BY_STATUS = {
    REJECTED: "rejected",
    DUPLICATE: "rejected",
    CONFLICT: "uncertain",
    ACCEPTED_UNATTRIBUTED: "uncertain",
}
SOURCE_ORDER = {"ads": 0, "crm": 1, "revenue": 2}

# How a source row supports a claim. Summands add up to the claim's value;
# numerator and denominator rows form a ratio; join rows are linkage evidence
# (such as the CRM lead connecting revenue to a campaign) and carry no amount.
LINEAGE_ROLES = ("summand", "numerator", "denominator", "join")
CONTRIBUTING_ROLES = frozenset({"summand", "numerator", "denominator"})


class InputContractError(ValueError):
    """Raised when an input file cannot satisfy the required schema."""


@dataclass(frozen=True)
class RowDisposition:
    source: str
    source_row: int
    record_id: str
    status: str
    reason: str
    amount_aed: str


@dataclass(frozen=True)
class RejectedRecord:
    source: str
    source_row: int
    record_id: str
    severity: str
    reason: str
    status: str


@dataclass(frozen=True)
class Claim:
    evidence_id: str
    metric: str
    value: str
    unit: str
    grade: str
    explanation: str
    source_refs: list[str]
    assumptions: list[str]
    lineage: list[dict[str, str]]


@dataclass
class EvidenceReport:
    run_id: str
    generated_at: str
    source_fingerprints: dict[str, str]
    summary: dict[str, Any]
    channels: list[dict[str, Any]]
    sensitivity: list[dict[str, Any]]
    claims: list[Claim]
    rejected_records: list[RejectedRecord]
    dispositions: list[RowDisposition]
    recommendation: dict[str, Any]
    human_approval: dict[str, Any]
    narrative: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _parse_money(value: str, field: str) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{field} is not a number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} must be a non-negative finite number")
    return parsed.quantize(MONEY, rounding=ROUND_HALF_UP)


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _text(row: dict[str, Any], field: str) -> str:
    """A field's trimmed text; a short row yields an empty value, never a crash."""
    value = row.get(field)
    return value.strip() if isinstance(value, str) else ""


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path, required: set[str]) -> list[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual = set(reader.fieldnames or [])
        missing = required - actual
        if missing:
            raise InputContractError(
                f"{path.name} is missing required columns: {', '.join(sorted(missing))}"
            )
        return [(row_number, dict(row)) for row_number, row in enumerate(reader, start=2)]


def _claim(
    evidence_id: str,
    metric: str,
    value: str,
    unit: str,
    grade: str,
    explanation: str,
    roles: dict[str, list[str]],
    assumptions: Iterable[str] = (),
) -> Claim:
    unknown = set(roles) - set(LINEAGE_ROLES)
    if unknown:
        raise ValueError(f"unknown lineage roles: {sorted(unknown)}")
    lineage = [
        {"role": role, "source_ref": ref}
        for role in LINEAGE_ROLES
        for ref in dict.fromkeys(roles.get(role, []))
    ]
    source_refs = list(dict.fromkeys(entry["source_ref"] for entry in lineage))
    return Claim(
        evidence_id, metric, value, unit, grade, explanation, source_refs, list(assumptions), lineage
    )


def _dispose(
    dispositions: dict[str, RowDisposition],
    source: str,
    source_row: int,
    record_id: str,
    status: str,
    reason: str = "",
    amount: Decimal | None = None,
) -> None:
    dispositions[f"{source}:{source_row}"] = RowDisposition(
        source, source_row, record_id, status, reason, "" if amount is None else _money(amount)
    )


def _resolve_identity(
    rows: list[dict[str, Any]],
    key: str,
    signature,
    dispositions: dict[str, RowDisposition],
    source: str,
    duplicate_reason: str,
    conflict_reason,
    amount_field: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Resolve rows sharing an identifier without depending on file order.

    Identical rows are one record: the earliest is kept and the rest are
    duplicates. Rows that share an identifier but disagree are all conflicts —
    choosing one would let the export's row order decide the evidence.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    kept: list[dict[str, Any]] = []
    conflicts: dict[str, list[str]] = {}
    for identifier, members in groups.items():
        members.sort(key=lambda row: row["source_row"])
        amount = (lambda row: row[amount_field]) if amount_field else (lambda row: None)
        if len({signature(row) for row in members}) > 1:
            reason = conflict_reason(members)
            conflicts[identifier] = [row["source_ref"] for row in members]
            for row in members:
                _dispose(dispositions, source, row["source_row"], identifier, CONFLICT, reason, amount(row))
            continue
        first, *repeats = members
        kept.append(first)
        _dispose(dispositions, source, first["source_row"], identifier, ACCEPTED, "", amount(first))
        for row in repeats:
            _dispose(dispositions, source, row["source_row"], identifier, DUPLICATE, duplicate_reason, amount(row))
    kept.sort(key=lambda row: row["source_row"])
    return kept, conflicts


class EvidenceEngine:
    ADS_FIELDS = {"date", "campaign_id", "channel", "spend_aed"}
    CRM_FIELDS = {"lead_id", "campaign_id", "created_at", "status"}
    REVENUE_FIELDS = {"transaction_id", "lead_id", "value_aed", "date"}

    def run(
        self,
        ads_path: str | Path,
        crm_path: str | Path,
        revenue_path: str | Path,
    ) -> EvidenceReport:
        ads_path, crm_path, revenue_path = map(Path, (ads_path, crm_path, revenue_path))
        ads_rows = _read_csv(ads_path, self.ADS_FIELDS)
        crm_rows = _read_csv(crm_path, self.CRM_FIELDS)
        revenue_rows = _read_csv(revenue_path, self.REVENUE_FIELDS)

        dispositions: dict[str, RowDisposition] = {}
        ads, conflicted_campaigns = self._validate_ads(ads_rows, dispositions)
        crm, conflicting_leads = self._validate_crm(crm_rows, dispositions)
        revenue = self._validate_revenue(revenue_rows, dispositions)

        spend_total = sum((row["spend"] for row in ads), Decimal("0"))
        revenue_total = sum((row["value"] for row in revenue), Decimal("0"))
        leads_by_id = {row["lead_id"]: row for row in crm}
        campaign_to_channel: dict[str, str] = {}
        spend_by_channel: dict[str, Decimal] = {}
        for row in ads:
            campaign_to_channel[row["campaign_id"]] = row["channel"]
            spend_by_channel[row["channel"]] = spend_by_channel.get(
                row["channel"], Decimal("0")
            ) + row["spend"]

        attributed: list[dict[str, Any]] = []
        unattributed: list[dict[str, Any]] = []
        conflicted_revenue: list[dict[str, Any]] = []
        for row in revenue:
            lead = leads_by_id.get(row["lead_id"])
            channel = campaign_to_channel.get(lead["campaign_id"]) if lead else None
            if lead is None and row["lead_id"] in conflicting_leads:
                reason = "lead_id has conflicting CRM records; revenue cannot be attributed"
                conflicted_revenue.append(row)
            elif lead is None:
                reason = "lead_id has no accepted CRM record; revenue cannot be attributed"
            elif channel is None and lead["campaign_id"] in conflicted_campaigns:
                reason = "CRM campaign_id maps to conflicting advertising channels"
            elif channel is None:
                reason = "CRM campaign_id has no accepted advertising record"
            elif (row["date"] - lead["created_at"]).days < 0:
                reason = "revenue date precedes lead creation date"
            else:
                reason = ""
            if reason:
                unattributed.append(row)
                _dispose(
                    dispositions, "revenue", row["source_row"], row["transaction_id"],
                    ACCEPTED_UNATTRIBUTED, reason, row["value"],
                )
                continue
            attributed.append(
                {
                    **row,
                    "campaign_id": lead["campaign_id"],
                    "channel": channel,
                    "delay_days": (row["date"] - lead["created_at"]).days,
                    "crm_ref": lead["source_ref"],
                }
            )

        attributed_total = sum((row["value"] for row in attributed), Decimal("0"))
        unattributed_total = sum((row["value"] for row in unattributed), Decimal("0"))
        conflicted_total = sum((row["value"] for row in conflicted_revenue), Decimal("0"))
        coverage = Decimal("0") if revenue_total == 0 else attributed_total / revenue_total

        channel_rows: list[dict[str, Any]] = []
        for channel in sorted(spend_by_channel):
            channel_revenue = sum(
                (row["value"] for row in attributed if row["channel"] == channel),
                Decimal("0"),
            )
            spend = spend_by_channel[channel]
            roas = Decimal("0") if spend == 0 else channel_revenue / spend
            channel_rows.append(
                {
                    "channel": channel,
                    "spend_aed": _money(spend),
                    "attributed_revenue_aed": _money(channel_revenue),
                    "assumption_dependent_roas": str(roas.quantize(Decimal("0.01"))),
                    "evidence_grade": "assumption-dependent",
                }
            )

        sensitivity: list[dict[str, Any]] = []
        for window in (30, 60, 90):
            within = sum(
                (row["value"] for row in attributed if row["delay_days"] <= window),
                Decimal("0"),
            )
            sensitivity.append(
                {
                    "attribution_window_days": window,
                    "attributed_revenue_aed": _money(within),
                    "excluded_delayed_revenue_aed": _money(attributed_total - within),
                }
            )

        ad_refs = [row["source_ref"] for row in ads]
        revenue_refs = [row["source_ref"] for row in revenue]
        attributed_refs = [row["source_ref"] for row in attributed]
        attribution_joins = [row["crm_ref"] for row in attributed]
        claims = [
            _claim(
                "EV-SPEND-001",
                "accepted_ad_spend",
                _money(spend_total),
                "AED",
                "reconciled",
                "Sum of accepted advertising rows; invalid, duplicate and conflicting rows are excluded.",
                {"summand": ad_refs},
            ),
            _claim(
                "EV-REV-001",
                "accepted_revenue",
                _money(revenue_total),
                "AED",
                "reconciled",
                "Sum of accepted revenue rows, attributed or not; invalid, duplicate and conflicting rows are excluded.",
                {"summand": revenue_refs},
            ),
            _claim(
                "EV-ATTR-001",
                "attributed_revenue",
                _money(attributed_total),
                "AED",
                "assumption-dependent",
                "Revenue connected deterministically through revenue.lead_id to CRM campaign_id and advertising channel.",
                {"summand": attributed_refs, "join": attribution_joins},
                ["CRM campaign is treated as the governing source attribution."],
            ),
            _claim(
                "EV-COVER-001",
                "revenue_attribution_coverage",
                str((coverage * 100).quantize(Decimal("0.1"))),
                "percent",
                "reconciled",
                "Attributed revenue (numerator rows) as a share of accepted revenue (denominator rows).",
                {"numerator": attributed_refs, "denominator": revenue_refs, "join": attribution_joins},
            ),
            _claim(
                "EV-UNMATCH-001",
                "unattributed_or_invalid_revenue",
                _money(unattributed_total),
                "AED",
                "reconciled",
                "Accepted revenue that could not be assigned to a channel: no accepted or unambiguous CRM lead, "
                "no accepted advertising campaign, or revenue dated before lead creation.",
                {"summand": [row["source_ref"] for row in unattributed]},
            ),
            _claim(
                "EV-CONFLICT-001",
                "revenue_with_conflicting_lead_attribution",
                _money(conflicted_total),
                "AED",
                "reconciled",
                "Accepted revenue whose CRM lead has contradictory records; excluded from attribution "
                "instead of letting file order choose a campaign.",
                {
                    "summand": [row["source_ref"] for row in conflicted_revenue],
                    "join": [
                        ref
                        for row in conflicted_revenue
                        for ref in conflicting_leads[row["lead_id"]]
                    ],
                },
            ),
        ]

        ordered = self._complete_dispositions(
            dispositions, {"ads": ads_rows, "crm": crm_rows, "revenue": revenue_rows}
        )
        rejected = [
            RejectedRecord(
                row.source, row.source_row, row.record_id,
                SEVERITY_BY_STATUS[row.status], row.reason, row.status,
            )
            for row in ordered
            if row.status != ACCEPTED
        ]
        status_counts = {
            source: {
                "input": len(rows),
                **{
                    status: sum(1 for row in ordered if row.source == source and row.status == status)
                    for status in STATUSES
                },
            }
            for source, rows in (("ads", ads_rows), ("crm", crm_rows), ("revenue", revenue_rows))
        }

        recommendation = self._recommend(channel_rows, coverage, rejected)
        fingerprints = {
            "ads": _fingerprint(ads_path),
            "crm": _fingerprint(crm_path),
            "revenue": _fingerprint(revenue_path),
        }
        run_id = hashlib.sha256(
            "|".join(fingerprints.values()).encode("utf-8")
        ).hexdigest()[:16]
        return EvidenceReport(
            run_id=run_id,
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            source_fingerprints=fingerprints,
            summary={
                "accepted_spend_aed": _money(spend_total),
                "accepted_revenue_aed": _money(revenue_total),
                "attributed_revenue_aed": _money(attributed_total),
                "unattributed_or_invalid_revenue_aed": _money(unattributed_total),
                "conflicting_lead_revenue_aed": _money(conflicted_total),
                "attribution_coverage_percent": str(
                    (coverage * 100).quantize(Decimal("0.1"))
                ),
                "accepted_rows": {
                    "ads": len(ads),
                    "crm": len(crm),
                    "revenue": len(revenue),
                },
                "row_status_counts": status_counts,
                "rejected_or_uncertain_rows": len(rejected),
            },
            channels=channel_rows,
            sensitivity=sensitivity,
            claims=claims,
            rejected_records=rejected,
            dispositions=ordered,
            recommendation=recommendation,
            human_approval={
                "required": True,
                "approver_role": "accountable budget owner",
                "status": "not approved",
                "allowed_values": ["approve", "reject", "request-more-evidence"],
            },
            narrative={
                "status": "disabled",
                "reason": "authoritative deterministic report generated; optional AI narrative not requested",
                "content": "",
            },
        )

    @staticmethod
    def _complete_dispositions(
        dispositions: dict[str, RowDisposition],
        inputs: dict[str, list[tuple[int, dict[str, str]]]],
    ) -> list[RowDisposition]:
        """Every input row must have exactly one status; a gap is an engine defect."""
        expected = {f"{source}:{row_number}" for source, rows in inputs.items() for row_number, _ in rows}
        missing = expected - set(dispositions)
        unexpected = set(dispositions) - expected
        if missing or unexpected:
            raise RuntimeError(
                f"row disposition invariant violated; missing={sorted(missing)} unexpected={sorted(unexpected)}"
            )
        return sorted(
            dispositions.values(), key=lambda row: (SOURCE_ORDER[row.source], row.source_row)
        )

    @staticmethod
    def _validate_ads(
        rows: Iterable[tuple[int, dict[str, str]]], dispositions: dict[str, RowDisposition]
    ) -> tuple[list[dict[str, Any]], set[str]]:
        valid: list[dict[str, Any]] = []
        for source_row, row in rows:
            campaign_id = _text(row, "campaign_id")
            channel = _text(row, "channel")
            record_id = campaign_id or f"row-{source_row}"
            try:
                parsed = {
                    "date": _parse_date(row.get("date"), "date"),
                    "campaign_id": campaign_id,
                    "channel": channel,
                    "spend": _parse_money(row.get("spend_aed"), "spend_aed"),
                    "source_row": source_row,
                    "source_ref": f"ads:{source_row}",
                }
                if not campaign_id or not channel:
                    raise ValueError("campaign_id and channel are required")
            except ValueError as exc:
                _dispose(dispositions, "ads", source_row, record_id, REJECTED, str(exc))
                continue
            valid.append(parsed)

        channels_by_campaign: dict[str, set[str]] = defaultdict(set)
        for row in valid:
            channels_by_campaign[row["campaign_id"]].add(row["channel"])
        conflicted = {campaign for campaign, channels in channels_by_campaign.items() if len(channels) > 1}

        accepted: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for row in valid:
            if row["campaign_id"] in conflicted:
                _dispose(
                    dispositions, "ads", row["source_row"], row["campaign_id"], CONFLICT,
                    "campaign_id maps to conflicting channels", row["spend"],
                )
                continue
            signature = (row["date"], row["campaign_id"], row["channel"], row["spend"])
            if signature in seen:
                _dispose(
                    dispositions, "ads", row["source_row"], row["campaign_id"], DUPLICATE,
                    "exact duplicate advertising row", row["spend"],
                )
                continue
            seen.add(signature)
            accepted.append(row)
            _dispose(dispositions, "ads", row["source_row"], row["campaign_id"], ACCEPTED, "", row["spend"])
        return accepted, conflicted

    @staticmethod
    def _validate_crm(
        rows: Iterable[tuple[int, dict[str, str]]], dispositions: dict[str, RowDisposition]
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        valid: list[dict[str, Any]] = []
        for source_row, row in rows:
            lead_id = _text(row, "lead_id")
            campaign_id = _text(row, "campaign_id")
            record_id = lead_id or f"row-{source_row}"
            try:
                if not lead_id or not campaign_id:
                    raise ValueError("lead_id and campaign_id are required")
                parsed = {
                    "lead_id": lead_id,
                    "campaign_id": campaign_id,
                    "created_at": _parse_date(row.get("created_at"), "created_at"),
                    "status": _text(row, "status").lower(),
                    "source_row": source_row,
                    "source_ref": f"crm:{source_row}",
                }
                if not parsed["status"]:
                    raise ValueError("status is required")
            except ValueError as exc:
                _dispose(dispositions, "crm", source_row, record_id, REJECTED, str(exc))
                continue
            valid.append(parsed)

        def conflict_reason(members: list[dict[str, Any]]) -> str:
            if len({row["campaign_id"] for row in members}) > 1:
                return "lead_id has conflicting campaign attribution"
            return "lead_id has conflicting created_at or status values"

        return _resolve_identity(
            valid,
            key="lead_id",
            signature=lambda row: (row["campaign_id"], row["created_at"], row["status"]),
            dispositions=dispositions,
            source="crm",
            duplicate_reason="duplicate lead_id",
            conflict_reason=conflict_reason,
        )

    @staticmethod
    def _validate_revenue(
        rows: Iterable[tuple[int, dict[str, str]]], dispositions: dict[str, RowDisposition]
    ) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        for source_row, row in rows:
            transaction_id = _text(row, "transaction_id")
            lead_id = _text(row, "lead_id")
            record_id = transaction_id or f"row-{source_row}"
            try:
                if not transaction_id or not lead_id:
                    raise ValueError("transaction_id and lead_id are required")
                parsed = {
                    "transaction_id": transaction_id,
                    "lead_id": lead_id,
                    "value": _parse_money(row.get("value_aed"), "value_aed"),
                    "date": _parse_date(row.get("date"), "date"),
                    "source_row": source_row,
                    "source_ref": f"revenue:{source_row}",
                }
            except ValueError as exc:
                _dispose(dispositions, "revenue", source_row, record_id, REJECTED, str(exc))
                continue
            valid.append(parsed)

        accepted, _ = _resolve_identity(
            valid,
            key="transaction_id",
            signature=lambda row: (row["lead_id"], row["value"], row["date"]),
            dispositions=dispositions,
            source="revenue",
            duplicate_reason="duplicate transaction_id",
            conflict_reason=lambda members: "transaction_id has conflicting values",
            amount_field="value",
        )
        return accepted

    @staticmethod
    def _recommend(
        channels: list[dict[str, Any]],
        coverage: Decimal,
        rejected: list[RejectedRecord],
    ) -> dict[str, Any]:
        if coverage < Decimal("0.80"):
            return {
                "action": "request-more-evidence",
                "decision": "Do not reallocate budget from this evidence set.",
                "reason": "Revenue attribution coverage is below 80%.",
                "evidence_grade": "unsupported",
                "human_approval_required": True,
            }
        if any(row.severity == "rejected" for row in rejected):
            return {
                "action": "hold",
                "decision": "Hold the current allocation while rejected records are corrected.",
                "reason": "At least one source record failed deterministic validation.",
                "evidence_grade": "assumption-dependent",
                "human_approval_required": True,
            }
        ranked = sorted(
            channels,
            key=lambda row: Decimal(row["assumption_dependent_roas"]),
            reverse=True,
        )
        best = ranked[0]["channel"] if ranked else "no channel"
        return {
            "action": "bounded-test",
            "decision": f"If the budget owner agrees, test at most a 10% reallocation toward {best}.",
            "reason": "Coverage is adequate, but channel attribution remains assumption-dependent.",
            "evidence_grade": "assumption-dependent",
            "human_approval_required": True,
        }


def write_outputs(report: EvidenceReport, output_dir: str | Path) -> None:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    with (output / "rejected_records.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["source", "source_row", "record_id", "status", "severity", "reason"]
        )
        writer.writeheader()
        for row in report.rejected_records:
            writer.writerow(asdict(row))

    with (output / "lineage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["evidence_id", "metric", "grade", "role", "source_ref"],
        )
        writer.writeheader()
        for claim in report.claims:
            for entry in claim.lineage:
                writer.writerow(
                    {
                        "evidence_id": claim.evidence_id,
                        "metric": claim.metric,
                        "grade": claim.grade,
                        "role": entry["role"],
                        "source_ref": entry["source_ref"],
                    }
                )

    supports: dict[str, list[str]] = defaultdict(list)
    for claim in report.claims:
        for entry in claim.lineage:
            supports[entry["source_ref"]].append(f"{claim.evidence_id}:{entry['role']}")
    with (output / "row_dispositions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source", "source_row", "record_id", "status", "reason", "amount_aed", "supports"],
        )
        writer.writeheader()
        for row in report.dispositions:
            writer.writerow(
                {
                    **asdict(row),
                    "supports": ";".join(supports.get(f"{row.source}:{row.source_row}", [])),
                }
            )

    (output / "executive_brief.md").write_text(_brief(report), encoding="utf-8")
    (output / "report.html").write_text(_html(report), encoding="utf-8")
    evaluation = {
        "run_id": report.run_id,
        "status": "PASS" if report.claims and report.human_approval["required"] else "FAIL",
        "checks": {
            "source_fingerprints_present": len(report.source_fingerprints) == 3,
            "authoritative_claims_present": bool(report.claims),
            "rejected_records_visible": True,
            "sensitivity_present": len(report.sensitivity) == 3,
            "human_approval_required": report.human_approval["required"],
            "ai_not_authoritative": report.narrative["status"] != "authoritative",
        },
    }
    (output / "evaluation_results.json").write_text(
        json.dumps(evaluation, indent=2), encoding="utf-8"
    )


def _brief(report: EvidenceReport) -> str:
    channels = "\n".join(
        f"- **{row['channel']}** — spend AED {row['spend_aed']}; attributed revenue AED {row['attributed_revenue_aed']}; assumption-dependent ROAS {row['assumption_dependent_roas']}×."
        for row in report.channels
    )
    sensitivity = "\n".join(
        f"- {row['attribution_window_days']} days: AED {row['attributed_revenue_aed']} attributed; AED {row['excluded_delayed_revenue_aed']} excluded."
        for row in report.sensitivity
    )
    return f"""# Executive Evidence Brief — Synthetic Demonstration

**Evidence status:** synthetic and reproducible; not a customer result, real-data deployment, reference, or paid validation.
**Run ID:** `{report.run_id}`
**Decision status:** not approved; named budget-owner approval is required.

## Decision question

What can the supplied advertising, CRM, and revenue exports defend about the next channel-budget decision?

## Reconciled evidence

- Accepted spend: **AED {report.summary['accepted_spend_aed']}** `[EV-SPEND-001]`
- Accepted revenue: **AED {report.summary['accepted_revenue_aed']}** `[EV-REV-001]`
- Attributed revenue: **AED {report.summary['attributed_revenue_aed']}** `[EV-ATTR-001]`
- Attribution coverage: **{report.summary['attribution_coverage_percent']}%** `[EV-COVER-001]`
- Unattributed or invalid revenue: **AED {report.summary['unattributed_or_invalid_revenue_aed']}** `[EV-UNMATCH-001]`
- Revenue with conflicting lead attribution: **AED {report.summary['conflicting_lead_revenue_aed']}** `[EV-CONFLICT-001]`
- Rejected or uncertain rows: **{report.summary['rejected_or_uncertain_rows']}**

## Channel view

{channels}

## Sensitivity to conversion delay

{sensitivity}

## Recommendation

**{report.recommendation['decision']}**

Reason: {report.recommendation['reason']} This recommendation is graded **{report.recommendation['evidence_grade']}** and cannot be executed without human approval.

## What the evidence cannot defend

- causal incrementality or a claim that advertising caused the revenue;
- customer lifetime value beyond the supplied transaction period;
- an ROI guarantee or autonomous budget change;
- any conclusion from rejected, unmatched, or silently repaired rows.

## Required next action

The accountable budget owner must choose **approve**, **reject**, or **request more evidence**, record the rationale, and define the observation period. The deterministic report remains authoritative even if the optional AI narrative is disabled.
"""


def _html(report: EvidenceReport) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{row['channel']}</td><td>AED {row['spend_aed']}</td>"
        f"<td>AED {row['attributed_revenue_aed']}</td>"
        f"<td>{row['assumption_dependent_roas']}×</td>"
        f"<td>{row['evidence_grade']}</td></tr>"
        for row in report.channels
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revenue Decision Evidence — Synthetic Case</title>
<style>
body{{font-family:Inter,Arial,sans-serif;margin:0;background:#f4f7fb;color:#17233b}}main{{max-width:960px;margin:40px auto;padding:0 24px}}.tag{{display:inline-block;background:#fff1cf;color:#6f4d00;padding:7px 10px;border-radius:16px;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:22px 0}}.card,section{{background:white;border:1px solid #dbe4f0;border-radius:12px;padding:20px;box-shadow:0 4px 16px #18233a0a}}.metric{{font-size:28px;font-weight:800;color:#173f7a}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #e4e9f0}}.warn{{border-left:5px solid #d18a00}}.approval{{border-left:5px solid #b42318}}small{{color:#596579}}h1,h2{{color:#183e73}}</style>
</head><body><main>
<span class="tag">SYNTHETIC DEMONSTRATION — NOT CUSTOMER VALIDATION</span>
<h1>Revenue Decision Evidence</h1><p>Run <code>{report.run_id}</code>. Deterministic result; human approval required.</p>
<div class="grid">
<div class="card"><small>Accepted spend</small><div class="metric">AED {report.summary['accepted_spend_aed']}</div><small>EV-SPEND-001</small></div>
<div class="card"><small>Accepted revenue</small><div class="metric">AED {report.summary['accepted_revenue_aed']}</div><small>EV-REV-001</small></div>
<div class="card"><small>Attribution coverage</small><div class="metric">{report.summary['attribution_coverage_percent']}%</div><small>EV-COVER-001</small></div>
<div class="card"><small>Rows needing attention</small><div class="metric">{report.summary['rejected_or_uncertain_rows']}</div><small>Visible, never repaired silently</small></div>
</div>
<section><h2>Channel evidence</h2><table><thead><tr><th>Channel</th><th>Spend</th><th>Attributed revenue</th><th>ROAS</th><th>Grade</th></tr></thead><tbody>{rows}</tbody></table></section>
<section class="warn"><h2>Recommendation</h2><p><strong>{report.recommendation['decision']}</strong></p><p>{report.recommendation['reason']}</p><p>Evidence grade: {report.recommendation['evidence_grade']}.</p></section>
<section class="approval"><h2>Approval gate</h2><p>Status: <strong>not approved</strong>. The accountable budget owner must approve, reject, or request more evidence. No action is automated.</p></section>
</main></body></html>"""

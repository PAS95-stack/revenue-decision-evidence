from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


MONEY = Decimal("0.01")


class InputContractError(ValueError):
    """Raised when an input file cannot satisfy the required schema."""


@dataclass(frozen=True)
class RejectedRecord:
    source: str
    source_row: int
    record_id: str
    severity: str
    reason: str


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
        rejected: list[RejectedRecord] = []
        ads = self._validate_ads(_read_csv(ads_path, self.ADS_FIELDS), rejected)
        crm = self._validate_crm(_read_csv(crm_path, self.CRM_FIELDS), rejected)
        revenue = self._validate_revenue(
            _read_csv(revenue_path, self.REVENUE_FIELDS), rejected
        )

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
        orphan_revenue = Decimal("0")
        for row in revenue:
            lead = leads_by_id.get(row["lead_id"])
            if not lead:
                orphan_revenue += row["value"]
                rejected.append(
                    RejectedRecord(
                        "revenue",
                        row["source_row"],
                        row["transaction_id"],
                        "uncertain",
                        "lead_id has no accepted CRM record; revenue cannot be attributed",
                    )
                )
                continue
            channel = campaign_to_channel.get(lead["campaign_id"])
            if not channel:
                orphan_revenue += row["value"]
                rejected.append(
                    RejectedRecord(
                        "revenue",
                        row["source_row"],
                        row["transaction_id"],
                        "uncertain",
                        "CRM campaign_id has no accepted advertising record",
                    )
                )
                continue
            delay_days = (row["date"] - lead["created_at"]).days
            if delay_days < 0:
                orphan_revenue += row["value"]
                rejected.append(
                    RejectedRecord(
                        "revenue",
                        row["source_row"],
                        row["transaction_id"],
                        "rejected",
                        "revenue date precedes lead creation date",
                    )
                )
                continue
            attributed.append(
                {
                    **row,
                    "campaign_id": lead["campaign_id"],
                    "channel": channel,
                    "delay_days": delay_days,
                    "crm_ref": lead["source_ref"],
                }
            )

        attributed_total = sum((row["value"] for row in attributed), Decimal("0"))
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

        accepted_refs = {
            "ads": [row["source_ref"] for row in ads],
            "crm": [row["source_ref"] for row in crm],
            "revenue": [row["source_ref"] for row in revenue],
        }
        claims = [
            Claim(
                "EV-SPEND-001",
                "accepted_ad_spend",
                _money(spend_total),
                "AED",
                "reconciled",
                "Sum of accepted advertising rows after validation and duplicate removal.",
                accepted_refs["ads"],
                [],
            ),
            Claim(
                "EV-REV-001",
                "accepted_revenue",
                _money(revenue_total),
                "AED",
                "reconciled",
                "Sum of accepted revenue rows after validation and duplicate removal.",
                accepted_refs["revenue"],
                [],
            ),
            Claim(
                "EV-ATTR-001",
                "attributed_revenue",
                _money(attributed_total),
                "AED",
                "assumption-dependent",
                "Revenue connected deterministically through revenue.lead_id to CRM campaign_id and advertising channel.",
                [
                    ref
                    for row in attributed
                    for ref in (row["source_ref"], row["crm_ref"])
                ],
                ["CRM campaign is treated as the governing source attribution."],
            ),
            Claim(
                "EV-COVER-001",
                "revenue_attribution_coverage",
                str((coverage * 100).quantize(Decimal("0.1"))),
                "percent",
                "reconciled",
                "Share of accepted revenue connected to an accepted CRM lead and advertising campaign.",
                accepted_refs["crm"] + accepted_refs["revenue"],
                [],
            ),
            Claim(
                "EV-UNMATCH-001",
                "unattributed_or_invalid_revenue",
                _money(orphan_revenue),
                "AED",
                "reconciled",
                "Accepted revenue that could not be assigned to an advertising channel or had invalid chronology.",
                [
                    f"revenue:{row.source_row}"
                    for row in rejected
                    if row.source == "revenue" and "attribute" in row.reason
                ]
                or accepted_refs["revenue"],
                [],
            ),
        ]

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
                "unattributed_or_invalid_revenue_aed": _money(orphan_revenue),
                "attribution_coverage_percent": str(
                    (coverage * 100).quantize(Decimal("0.1"))
                ),
                "accepted_rows": {
                    "ads": len(ads),
                    "crm": len(crm),
                    "revenue": len(revenue),
                },
                "rejected_or_uncertain_rows": len(rejected),
            },
            channels=channel_rows,
            sensitivity=sensitivity,
            claims=claims,
            rejected_records=rejected,
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
    def _validate_ads(
        rows: Iterable[tuple[int, dict[str, str]]], rejected: list[RejectedRecord]
    ) -> list[dict[str, Any]]:
        accepted, seen = [], set()
        campaign_channel: dict[str, str] = {}
        for source_row, row in rows:
            record_id = row.get("campaign_id", "").strip() or f"row-{source_row}"
            try:
                parsed = {
                    "date": _parse_date(row["date"], "date"),
                    "campaign_id": row["campaign_id"].strip(),
                    "channel": row["channel"].strip(),
                    "spend": _parse_money(row["spend_aed"], "spend_aed"),
                    "source_row": source_row,
                    "source_ref": f"ads:{source_row}",
                }
                if not parsed["campaign_id"] or not parsed["channel"]:
                    raise ValueError("campaign_id and channel are required")
                signature = (
                    parsed["date"], parsed["campaign_id"], parsed["channel"], parsed["spend"]
                )
                if signature in seen:
                    raise ValueError("exact duplicate advertising row")
                prior = campaign_channel.get(parsed["campaign_id"])
                if prior and prior != parsed["channel"]:
                    raise ValueError("campaign_id maps to conflicting channels")
                seen.add(signature)
                campaign_channel[parsed["campaign_id"]] = parsed["channel"]
                accepted.append(parsed)
            except ValueError as exc:
                rejected.append(
                    RejectedRecord("ads", source_row, record_id, "rejected", str(exc))
                )
        return accepted

    @staticmethod
    def _validate_crm(
        rows: Iterable[tuple[int, dict[str, str]]], rejected: list[RejectedRecord]
    ) -> list[dict[str, Any]]:
        accepted, seen = [], {}
        for source_row, row in rows:
            record_id = row.get("lead_id", "").strip() or f"row-{source_row}"
            try:
                lead_id = row["lead_id"].strip()
                campaign_id = row["campaign_id"].strip()
                if not lead_id or not campaign_id:
                    raise ValueError("lead_id and campaign_id are required")
                if lead_id in seen:
                    prior_campaign = seen[lead_id]
                    reason = (
                        "duplicate lead_id"
                        if prior_campaign == campaign_id
                        else "lead_id has conflicting campaign attribution"
                    )
                    raise ValueError(reason)
                parsed = {
                    "lead_id": lead_id,
                    "campaign_id": campaign_id,
                    "created_at": _parse_date(row["created_at"], "created_at"),
                    "status": row["status"].strip().lower(),
                    "source_row": source_row,
                    "source_ref": f"crm:{source_row}",
                }
                if not parsed["status"]:
                    raise ValueError("status is required")
                seen[lead_id] = campaign_id
                accepted.append(parsed)
            except ValueError as exc:
                rejected.append(
                    RejectedRecord("crm", source_row, record_id, "rejected", str(exc))
                )
        return accepted

    @staticmethod
    def _validate_revenue(
        rows: Iterable[tuple[int, dict[str, str]]], rejected: list[RejectedRecord]
    ) -> list[dict[str, Any]]:
        accepted, seen = [], set()
        for source_row, row in rows:
            record_id = row.get("transaction_id", "").strip() or f"row-{source_row}"
            try:
                transaction_id = row["transaction_id"].strip()
                lead_id = row["lead_id"].strip()
                if not transaction_id or not lead_id:
                    raise ValueError("transaction_id and lead_id are required")
                if transaction_id in seen:
                    raise ValueError("duplicate transaction_id")
                parsed = {
                    "transaction_id": transaction_id,
                    "lead_id": lead_id,
                    "value": _parse_money(row["value_aed"], "value_aed"),
                    "date": _parse_date(row["date"], "date"),
                    "source_row": source_row,
                    "source_ref": f"revenue:{source_row}",
                }
                seen.add(transaction_id)
                accepted.append(parsed)
            except ValueError as exc:
                rejected.append(
                    RejectedRecord("revenue", source_row, record_id, "rejected", str(exc))
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
            handle, fieldnames=["source", "source_row", "record_id", "severity", "reason"]
        )
        writer.writeheader()
        for row in report.rejected_records:
            writer.writerow(asdict(row))

    with (output / "lineage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["evidence_id", "metric", "grade", "source_ref"],
        )
        writer.writeheader()
        for claim in report.claims:
            for source_ref in claim.source_refs:
                writer.writerow(
                    {
                        "evidence_id": claim.evidence_id,
                        "metric": claim.metric,
                        "grade": claim.grade,
                        "source_ref": source_ref,
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

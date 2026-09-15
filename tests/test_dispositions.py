"""Phase 1 invariants: one status per input row, and lineage that adds up.

These pin the contract the attack suite relies on. The expected synthetic figures
were derived by hand from data/synthetic before the repair, not copied from the
engine's output.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence.engine import (  # noqa: E402
    ACCEPTED,
    ACCEPTED_UNATTRIBUTED,
    CONFLICT,
    CONTRIBUTING_ROLES,
    DUPLICATE,
    REJECTED,
    EvidenceEngine,
    write_outputs,
)

DATA = ROOT / "data" / "synthetic"
ADS, CRM, REVENUE = DATA / "ads.csv", DATA / "crm.csv", DATA / "revenue.csv"
EXCLUDED = {REJECTED, DUPLICATE, CONFLICT}


def data_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


class DispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_conservation(self, report, inputs: dict[str, Path]) -> None:
        for source, path in inputs.items():
            with self.subTest(source=source):
                rows = [row.source_row for row in report.dispositions if row.source == source]
                expected = list(range(2, data_row_count(path) + 2))
                self.assertEqual(sorted(rows), expected, "each input row needs exactly one status")
                counts = report.summary["row_status_counts"][source]
                self.assertEqual(counts["input"], len(expected))
                self.assertEqual(
                    counts["input"],
                    sum(value for key, value in counts.items() if key != "input"),
                )

    def test_p1_01_every_synthetic_row_has_exactly_one_status(self):
        report = EvidenceEngine().run(ADS, CRM, REVENUE)
        self.assert_conservation(report, {"ads": ADS, "crm": CRM, "revenue": REVENUE})

    def test_p1_02_short_and_blank_rows_are_disposed_not_crashed(self):
        ads = self.root / "ads.csv"
        crm = self.root / "crm.csv"
        revenue = self.root / "revenue.csv"
        ads.write_text("date,campaign_id,channel,spend_aed\n2026-07-01\n\n,,,\n", encoding="utf-8")
        crm.write_text("lead_id,campaign_id,created_at,status\nL1\n", encoding="utf-8")
        revenue.write_text("transaction_id,lead_id,value_aed,date\nT1,L1\n", encoding="utf-8")
        report = EvidenceEngine().run(ads, crm, revenue)
        self.assert_conservation(report, {"ads": ads, "crm": crm, "revenue": revenue})
        self.assertTrue(all(row.status == REJECTED for row in report.dispositions))

    def test_p1_03_excluded_rows_never_contribute_to_any_figure(self):
        report = EvidenceEngine().run(ADS, CRM, REVENUE)
        status = {f"{row.source}:{row.source_row}": row.status for row in report.dispositions}
        for claim in report.claims:
            for entry in claim.lineage:
                if entry["role"] not in CONTRIBUTING_ROLES:
                    continue
                with self.subTest(evidence_id=claim.evidence_id, ref=entry["source_ref"]):
                    self.assertNotIn(status[entry["source_ref"]], EXCLUDED)
        attributed = next(claim for claim in report.claims if claim.evidence_id == "EV-ATTR-001")
        summands = {e["source_ref"] for e in attributed.lineage if e["role"] == "summand"}
        self.assertFalse(
            {ref for ref in summands if status[ref] == ACCEPTED_UNATTRIBUTED},
            "unattributed revenue must not be counted as attributed",
        )

    def test_p1_04_synthetic_figures_after_symmetric_conflict_handling(self):
        report = EvidenceEngine().run(ADS, CRM, REVENUE)
        claims = {claim.evidence_id: claim.value for claim in report.claims}
        self.assertEqual(claims["EV-SPEND-001"], "15000.00")
        self.assertEqual(claims["EV-REV-001"], "56500.00")
        self.assertEqual(claims["EV-ATTR-001"], "32000.00")
        self.assertEqual(claims["EV-COVER-001"], "56.6")
        self.assertEqual(claims["EV-UNMATCH-001"], "24500.00")
        self.assertEqual(claims["EV-CONFLICT-001"], "9000.00")
        channels = {row["channel"]: row for row in report.channels}
        self.assertEqual(channels["Paid Search"]["attributed_revenue_aed"], "16000.00")
        self.assertEqual(channels["Paid Search"]["assumption_dependent_roas"], "2.13")
        self.assertEqual(channels["Paid Social"]["attributed_revenue_aed"], "12500.00")
        self.assertEqual(channels["Email"]["attributed_revenue_aed"], "3500.00")
        windows = {row["attribution_window_days"]: row for row in report.sensitivity}
        self.assertEqual(windows[30]["attributed_revenue_aed"], "28500.00")
        self.assertEqual(windows[60]["attributed_revenue_aed"], "32000.00")
        statuses = {f"{row.source}:{row.source_row}": row.status for row in report.dispositions}
        self.assertEqual(statuses["crm:3"], CONFLICT)
        self.assertEqual(statuses["crm:8"], CONFLICT)
        self.assertEqual(statuses["revenue:10"], ACCEPTED_UNATTRIBUTED)
        self.assertEqual(statuses["revenue:8"], DUPLICATE)
        self.assertEqual(statuses["ads:7"], DUPLICATE)

    def test_p1_05_campaign_channel_conflict_is_symmetric(self):
        def run(rows: list[str]):
            ads = self.root / "ads.csv"
            ads.write_text("date,campaign_id,channel,spend_aed\n" + "\n".join(rows) + "\n", encoding="utf-8")
            return EvidenceEngine().run(ads, CRM, REVENUE)

        search = "2026-07-01,CMP-SEARCH-01,Paid Search,5000.00"
        mislabelled = "2026-07-02,CMP-SEARCH-01,Paid Social,700.00"
        email = "2026-07-01,CMP-EMAIL-01,Email,1000.00"
        forward = run([search, mislabelled, email])
        backward = run([mislabelled, search, email])
        for report in (forward, backward):
            self.assertEqual(report.summary["accepted_spend_aed"], "1000.00")
            self.assertEqual(
                sorted(row.status for row in report.dispositions if row.source == "ads"),
                [ACCEPTED, CONFLICT, CONFLICT],
            )
        self.assertEqual(forward.channels, backward.channels)
        self.assertEqual(
            {claim.evidence_id: claim.value for claim in forward.claims},
            {claim.evidence_id: claim.value for claim in backward.claims},
        )

    def test_p1_06_written_files_reconstruct_each_aed_claim(self):
        report = EvidenceEngine().run(ADS, CRM, REVENUE)
        output = self.root / "out"
        write_outputs(report, output)

        with (output / "row_dispositions.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(
            len(rows), sum(data_row_count(path) for path in (ADS, CRM, REVENUE))
        )
        amounts = {f"{row['source']}:{row['source_row']}": row["amount_aed"] for row in rows}

        with (output / "lineage.csv").open(encoding="utf-8", newline="") as handle:
            lineage = list(csv.DictReader(handle))
        for claim in report.claims:
            if claim.unit != "AED":
                continue
            with self.subTest(evidence_id=claim.evidence_id):
                total = sum(
                    (
                        Decimal(amounts[entry["source_ref"]])
                        for entry in lineage
                        if entry["evidence_id"] == claim.evidence_id and entry["role"] == "summand"
                    ),
                    Decimal("0"),
                )
                self.assertEqual(total, Decimal(claim.value))

        coverage = next(claim for claim in report.claims if claim.evidence_id == "EV-COVER-001")

        def role_sum(role: str) -> Decimal:
            return sum(
                (
                    Decimal(amounts[entry["source_ref"]])
                    for entry in lineage
                    if entry["evidence_id"] == "EV-COVER-001" and entry["role"] == role
                ),
                Decimal("0"),
            )

        self.assertEqual(
            str((role_sum("numerator") / role_sum("denominator") * 100).quantize(Decimal("0.1"))),
            coverage.value,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revenue_evidence.engine import EvidenceEngine, InputContractError, write_outputs
from revenue_evidence.narrative import evidence_payload, validate_narrative


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class EvidenceEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ads = self.root / "ads.csv"
        self.crm = self.root / "crm.csv"
        self.revenue = self.root / "revenue.csv"
        write_csv(
            self.ads,
            [
                {"date": "2026-07-01", "campaign_id": "C1", "channel": "Search", "spend_aed": "100"},
                {"date": "2026-07-01", "campaign_id": "C2", "channel": "Social", "spend_aed": "50"},
            ],
        )
        write_csv(
            self.crm,
            [
                {"lead_id": "L1", "campaign_id": "C1", "created_at": "2026-07-02", "status": "won"},
                {"lead_id": "L2", "campaign_id": "C2", "created_at": "2026-07-03", "status": "won"},
            ],
        )
        write_csv(
            self.revenue,
            [
                {"transaction_id": "T1", "lead_id": "L1", "value_aed": "300", "date": "2026-07-10"},
                {"transaction_id": "T2", "lead_id": "L2", "value_aed": "100", "date": "2026-08-20"},
            ],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_report(self):
        return EvidenceEngine().run(self.ads, self.crm, self.revenue)

    def test_01_valid_files_reconcile_and_trace(self):
        report = self.run_report()
        self.assertEqual(report.summary["accepted_spend_aed"], "150.00")
        self.assertEqual(report.summary["attributed_revenue_aed"], "400.00")
        for claim in report.claims:
            if Decimal(claim.value) == 0:
                # A zero total adds up no rows; listing rows would break reconstruction.
                self.assertFalse(
                    [e for e in claim.lineage if e["role"] == "summand"], claim.evidence_id
                )
            else:
                self.assertTrue(claim.source_refs, claim.evidence_id)

    def test_02_missing_ids_are_rejected(self):
        write_csv(
            self.crm,
            [{"lead_id": "", "campaign_id": "C1", "created_at": "2026-07-02", "status": "won"}],
        )
        report = self.run_report()
        self.assertTrue(any("lead_id" in row.reason for row in report.rejected_records))

    def test_03_duplicate_transaction_is_rejected(self):
        write_csv(
            self.revenue,
            [
                {"transaction_id": "T1", "lead_id": "L1", "value_aed": "300", "date": "2026-07-10"},
                {"transaction_id": "T1", "lead_id": "L1", "value_aed": "300", "date": "2026-07-10"},
            ],
        )
        report = self.run_report()
        self.assertEqual(report.summary["accepted_revenue_aed"], "300.00")
        self.assertTrue(any("duplicate transaction_id" in row.reason for row in report.rejected_records))

    def test_04_schema_drift_stops_run(self):
        write_csv(self.ads, [{"date": "2026-07-01", "campaign_id": "C1", "channel": "Search"}])
        with self.assertRaises(InputContractError):
            self.run_report()

    def test_05_invalid_currency_is_rejected(self):
        write_csv(
            self.ads,
            [{"date": "2026-07-01", "campaign_id": "C1", "channel": "Search", "spend_aed": "-5"}],
        )
        report = self.run_report()
        self.assertEqual(report.summary["accepted_spend_aed"], "0.00")
        self.assertTrue(any("non-negative" in row.reason for row in report.rejected_records))

    def test_06_delayed_conversion_changes_sensitivity(self):
        report = self.run_report()
        day_30 = next(row for row in report.sensitivity if row["attribution_window_days"] == 30)
        day_60 = next(row for row in report.sensitivity if row["attribution_window_days"] == 60)
        self.assertEqual(day_30["attributed_revenue_aed"], "300.00")
        self.assertEqual(day_60["attributed_revenue_aed"], "400.00")

    def test_07_conflicting_attribution_is_rejected(self):
        write_csv(
            self.crm,
            [
                {"lead_id": "L1", "campaign_id": "C1", "created_at": "2026-07-02", "status": "won"},
                {"lead_id": "L1", "campaign_id": "C2", "created_at": "2026-07-02", "status": "won"},
            ],
        )
        report = self.run_report()
        self.assertTrue(any("conflicting campaign" in row.reason for row in report.rejected_records))

    def test_08_orphan_revenue_remains_visible(self):
        write_csv(
            self.revenue,
            [{"transaction_id": "T9", "lead_id": "UNKNOWN", "value_aed": "80", "date": "2026-07-10"}],
        )
        report = self.run_report()
        self.assertEqual(report.summary["accepted_revenue_aed"], "80.00")
        self.assertEqual(report.summary["attributed_revenue_aed"], "0.00")
        self.assertEqual(report.recommendation["action"], "request-more-evidence")

    def test_09_output_totals_equal_source_rows(self):
        report = self.run_report()
        channel_sum = sum(Decimal(row["spend_aed"]) for row in report.channels)
        self.assertEqual(channel_sum, Decimal(report.summary["accepted_spend_aed"]))
        output = self.root / "out"
        write_outputs(report, output)
        payload = json.loads((output / "report.json").read_text())
        self.assertEqual(payload["run_id"], report.run_id)
        self.assertTrue((output / "lineage.csv").exists())

    def test_10_unsupported_ai_narrative_is_disabled(self):
        report = self.run_report()
        valid, _ = validate_narrative(
            "Search caused revenue and automatically execute the change [EV-FAKE-999].",
            report,
        )
        self.assertFalse(valid)

    def test_11_supported_ai_narrative_passes(self):
        report = self.run_report()
        valid, reason = validate_narrative(
            "Accepted spend is recorded [EV-SPEND-001]. Human approval is required.",
            report,
        )
        self.assertTrue(valid, reason)
        self.assertNotIn("source_rows", evidence_payload(report))

    def test_12_revenue_before_lead_is_not_attributed(self):
        write_csv(
            self.revenue,
            [{"transaction_id": "T1", "lead_id": "L1", "value_aed": "300", "date": "2026-07-01"}],
        )
        report = self.run_report()
        self.assertEqual(report.summary["attributed_revenue_aed"], "0.00")
        self.assertTrue(any("precedes" in row.reason for row in report.rejected_records))


if __name__ == "__main__":
    unittest.main()

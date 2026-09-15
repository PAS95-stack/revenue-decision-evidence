"""Phase 5: named data attacks.

Each attack checks the specific row outcome and then publishes, so every usable
dataset must also pass independent reconstruction: the engine and the checker
have to agree on dirty data, not only on the synthetic case.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence.engine import (  # noqa: E402
    ACCEPTED,
    ACCEPTED_UNATTRIBUTED,
    CONFLICT,
    DUPLICATE,
    REJECTED,
    EvidenceEngine,
    InputContractError,
    write_outputs,
)

HEADERS = {
    "ads": "date,campaign_id,channel,spend_aed",
    "crm": "lead_id,campaign_id,created_at,status",
    "revenue": "transaction_id,lead_id,value_aed,date",
}
BASE = {
    "ads": ["2026-07-01,C1,Search,100", "2026-07-01,C2,Social,50"],
    "crm": ["L1,C1,2026-07-02,won", "L2,C2,2026-07-03,won"],
    "revenue": ["T1,L1,300,2026-07-10", "T2,L2,100,2026-08-20"],
}
BAD_AMOUNTS = ("-5", "NaN", "Infinity", "1e3", "1E3", "1,000", "+5", "5.", ".5", "1_000", "", "abc", "٥٠", "1" * 40)
GOOD_AMOUNTS = {"50": "50.00", " 50.5 ": "50.50", "50.005": "50.01", "0": "0.00", "007": "7.00"}
BAD_DATES = ("2026/07/03", "20260703", "2026-7-3", "2026-02-30", "", "03-07-2026", "2026-07-03T00:00", "٢٠٢٦-٠٧-٠٣")


def line(*fields: str) -> str:
    """One CSV data line, quoted where a field needs it."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(fields)
    return buffer.getvalue()


class DataAttackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, *, newline: str = "\n", bom: bool = False, raw: dict | None = None, **rows) -> dict[str, Path]:
        folder = Path(tempfile.mkdtemp(dir=self.root))
        paths = {}
        for name in ("ads", "crm", "revenue"):
            text = (raw or {}).get(name)
            if text is None:
                text = newline.join([HEADERS[name], *rows.get(name, BASE[name])]) + newline
            path = folder / f"{name}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(("﻿" if bom else "") + text)
            paths[name] = path
        return paths

    def publish(self, **options):
        paths = self.write(**options)
        report = EvidenceEngine().run(paths["ads"], paths["crm"], paths["revenue"])
        output = paths["ads"].parent / "out"
        evaluation = write_outputs(report, output)
        statuses = {f"{row.source}:{row.source_row}": row for row in report.dispositions}
        return report, evaluation, statuses, output

    def assert_published(self, evaluation) -> None:
        self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])

    @staticmethod
    def values(report) -> dict[str, str]:
        return {claim.evidence_id: claim.value for claim in report.claims}

    def test_d01_exact_duplicate_transaction_counts_once(self):
        report, evaluation, statuses, _ = self.publish(revenue=[*BASE["revenue"], "T1,L1,300,2026-07-10"])
        self.assert_published(evaluation)
        self.assertEqual(statuses["revenue:4"].status, DUPLICATE)
        self.assertEqual(self.values(report)["EV-REV-001"], "400.00")

    def test_d02_same_transaction_with_different_values_is_excluded_as_conflict(self):
        report, evaluation, statuses, _ = self.publish(revenue=[*BASE["revenue"], "T1,L1,301,2026-07-10"])
        self.assert_published(evaluation)
        self.assertEqual((statuses["revenue:2"].status, statuses["revenue:4"].status), (CONFLICT, CONFLICT))
        self.assertEqual(self.values(report)["EV-REV-001"], "100.00")

    def test_d03_exact_duplicate_ad_row_is_excluded_with_its_amount_visible(self):
        report, evaluation, statuses, _ = self.publish(ads=[*BASE["ads"], "2026-07-01,C1,Search,100"])
        self.assert_published(evaluation)
        self.assertEqual((statuses["ads:4"].status, statuses["ads:4"].amount_aed), (DUPLICATE, "100.00"))
        self.assertEqual(self.values(report)["EV-SPEND-001"], "150.00")

    def test_d04_conflicting_crm_rows_give_identical_results_in_every_order(self):
        crm_rows = ["L1,C1,2026-07-02,won", "L1,C2,2026-07-02,won", "L2,C2,2026-07-03,won", "L3,C1,2026-07-04,won"]
        revenue = ["T1,L1,300,2026-07-10", "T2,L2,100,2026-08-20", "T3,L3,50,2026-07-12"]
        results = set()
        for order in itertools.permutations(crm_rows):
            report, evaluation, statuses, _ = self.publish(crm=list(order), revenue=revenue)
            self.assert_published(evaluation)
            self.assertEqual(sum(1 for row in statuses.values() if row.status == CONFLICT), 2)
            results.add(
                json.dumps(
                    {"claims": self.values(report), "channels": report.channels, "summary": report.summary},
                    sort_keys=True,
                )
            )
        self.assertEqual(len(results), 1, "24 row orders must produce one result")
        self.assertEqual(self.values(report)["EV-CONFLICT-001"], "300.00")
        self.assertEqual(self.values(report)["EV-ATTR-001"], "150.00")

    def test_d05_missing_identifiers_are_rejected_in_every_export(self):
        report, evaluation, statuses, _ = self.publish(
            ads=[*BASE["ads"], "2026-07-02,,Search,10"],
            crm=[*BASE["crm"], ",C1,2026-07-02,won"],
            revenue=[*BASE["revenue"], ",L1,10,2026-07-10"],
        )
        self.assert_published(evaluation)
        for ref in ("ads:4", "crm:4", "revenue:4"):
            self.assertEqual(statuses[ref].status, REJECTED, ref)

    def test_d06_amounts_must_be_plain_non_negative_decimals(self):
        for text in BAD_AMOUNTS:
            with self.subTest(amount=text):
                report, evaluation, statuses, _ = self.publish(ads=[BASE["ads"][0], line("2026-07-01", "C2", "Social", text)])
                self.assertEqual(statuses["ads:3"].status, REJECTED)
                self.assertIn("spend_aed", statuses["ads:3"].reason)
                self.assertEqual(report.summary["accepted_spend_aed"], "100.00")
                self.assert_published(evaluation)
        for text, expected in GOOD_AMOUNTS.items():
            with self.subTest(amount=text):
                _, evaluation, statuses, _ = self.publish(ads=[BASE["ads"][0], line("2026-07-01", "C2", "Social", text)])
                self.assertEqual((statuses["ads:3"].status, statuses["ads:3"].amount_aed), (ACCEPTED, expected))
                self.assert_published(evaluation)

    def test_d07_dates_must_be_valid_iso_calendar_dates(self):
        for text in BAD_DATES:
            with self.subTest(date=text):
                _, evaluation, statuses, _ = self.publish(crm=[BASE["crm"][0], line("L2", "C2", text, "won")])
                self.assertEqual(statuses["crm:3"].status, REJECTED)
                self.assertEqual(statuses["revenue:3"].status, ACCEPTED_UNATTRIBUTED)
                self.assert_published(evaluation)

    def test_d08_orphan_revenue_is_counted_but_not_attributed(self):
        report, evaluation, statuses, _ = self.publish(revenue=[*BASE["revenue"], "T3,L9,10,2026-07-10"])
        self.assert_published(evaluation)
        self.assertEqual(statuses["revenue:4"].status, ACCEPTED_UNATTRIBUTED)
        self.assertIn("no accepted CRM record", statuses["revenue:4"].reason)
        self.assertEqual(self.values(report)["EV-REV-001"], "410.00")

    def test_d09_unknown_campaign_revenue_is_counted_but_not_attributed(self):
        _, evaluation, statuses, _ = self.publish(
            crm=[*BASE["crm"], "L3,C9,2026-07-02,won"], revenue=[*BASE["revenue"], "T3,L3,10,2026-07-10"]
        )
        self.assert_published(evaluation)
        self.assertEqual(statuses["revenue:4"].status, ACCEPTED_UNATTRIBUTED)
        self.assertIn("no accepted advertising record", statuses["revenue:4"].reason)

    def test_d10_revenue_before_lead_creation_is_revenue_not_a_rejected_row(self):
        report, evaluation, statuses, _ = self.publish(revenue=[*BASE["revenue"], "T3,L1,10,2026-07-01"])
        self.assert_published(evaluation)
        self.assertEqual(statuses["revenue:4"].status, ACCEPTED_UNATTRIBUTED)
        self.assertIn("precedes", statuses["revenue:4"].reason)
        revenue_claim = next(claim for claim in report.claims if claim.evidence_id == "EV-REV-001")
        self.assertIn({"role": "summand", "source_ref": "revenue:4"}, revenue_claim.lineage)

    def test_d11_schema_drift_stops_with_the_file_and_column_named(self):
        for name in ("ads", "crm", "revenue"):
            with self.subTest(export=name):
                header, dropped = HEADERS[name].rsplit(",", 1)
                rows = [row.rsplit(",", 1)[0] for row in BASE[name]]
                paths = self.write(raw={name: "\n".join([header, *rows]) + "\n"})
                with self.assertRaisesRegex(InputContractError, rf"{name}\.csv is missing required columns: {dropped}"):
                    EvidenceEngine().run(paths["ads"], paths["crm"], paths["revenue"])

    def test_d12_header_only_export_publishes_nothing_but_the_failure(self):
        for name in ("ads", "crm", "revenue"):
            with self.subTest(export=name):
                _, evaluation, _, output = self.publish(raw={name: HEADERS[name] + "\n"})
                self.assertEqual(evaluation["status"], "FAIL")
                self.assertIn(f"the {name} export has no usable rows (no data rows)", " ".join(evaluation["failures"]))
                self.assertEqual(sorted(path.name for path in output.iterdir()), ["evaluation_results.json"])

    def test_d13_byte_order_mark_and_crlf_do_not_change_results(self):
        plain, plain_evaluation, plain_statuses, _ = self.publish()
        windows, windows_evaluation, windows_statuses, _ = self.publish(newline="\r\n", bom=True)
        self.assert_published(plain_evaluation)
        self.assert_published(windows_evaluation)
        self.assertEqual(self.values(plain), self.values(windows))
        self.assertEqual(sorted(plain_statuses), sorted(windows_statuses))

    def test_d14_blank_lines_and_multi_line_fields_keep_references_on_the_right_line(self):
        crm_text = HEADERS["crm"] + "\n" + 'L1,C1,2026-07-02,"won\nsecond line"\n' + "\n" + "L2,C2,2026-07-03,won\n"
        ads_text = HEADERS["ads"] + "\n\n" + "2026-07-01,C1,Search,100\n" + "2026-07-01,C2,Social,50\n"
        report, evaluation, statuses, output = self.publish(raw={"crm": crm_text, "ads": ads_text})
        self.assert_published(evaluation)
        self.assertEqual(sorted(ref for ref in statuses if ref.startswith("crm:")), ["crm:2", "crm:5"])
        self.assertEqual(sorted(ref for ref in statuses if ref.startswith("ads:")), ["ads:3", "ads:4"])
        crm_lines = (output.parent / "crm.csv").read_text(encoding="utf-8").splitlines()
        ads_lines = (output.parent / "ads.csv").read_text(encoding="utf-8").splitlines()
        self.assertTrue(crm_lines[1].startswith("L1,") and crm_lines[4].startswith("L2,"))
        self.assertTrue(ads_lines[2].startswith("2026-07-01,C1") and ads_lines[3].startswith("2026-07-01,C2"))
        # Both leads survive the blank line and the multi-line status: T1 300 + T2 100.
        self.assertEqual(self.values(report)["EV-ATTR-001"], "400.00")

    def test_d15_unreadable_exports_stop_with_a_named_error(self):
        paths = self.write()
        paths["crm"].write_bytes(b"lead_id,campaign_id,created_at,status\nL1,C1,2026-07-02,\xff\xfe\n")
        with self.assertRaisesRegex(InputContractError, r"crm\.csv is not UTF-8 text"):
            EvidenceEngine().run(paths["ads"], paths["crm"], paths["revenue"])


if __name__ == "__main__":
    unittest.main()

"""Stage 2: an engagement config lets real exports run without losing traceability.

The example in examples/messy-exports is synthetic but shaped like Meta, Google Ads,
HubSpot and Xero exports. Its figures below were calculated by hand before the code
was written.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence import cli  # noqa: E402
from revenue_evidence.engine import (  # noqa: E402
    EvidenceEngine,
    InputContractError,
    ensure_outside_repository,
    load_engagement,
    write_outputs,
)
from revenue_evidence.reconstruct import verify_outputs  # noqa: E402

EXAMPLE = ROOT / "examples" / "messy-exports"


def ref(row) -> str:
    return f"{row.source}/{row.source_file}:{row.source_row}" if row.source_file else f"{row.source}:{row.source_row}"


def edit_config(path: Path, change) -> None:
    config = json.loads(path.read_text(encoding="utf-8"))
    change(config)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def replace_text(path: Path, before: str, after: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(before) != 1:
        raise AssertionError(f"expected one {before!r} in {path.name}")
    path.write_text(text.replace(before, after), encoding="utf-8")


class EngagementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def example(self, name: str = "example") -> Path:
        shutil.copytree(EXAMPLE, self.root / name)
        return self.root / name / "engagement.json"

    def run_config(self, config: Path):
        return EvidenceEngine().run_engagement(load_engagement(config))

    def publish(self, config: Path, name: str = "out"):
        report = self.run_config(config)
        return report, write_outputs(report, self.root / name)


class ExampleEngagementTests(EngagementTestCase):
    def test_e01_example_publishes_the_hand_calculated_figures(self):
        report, evaluation = self.publish(self.example())
        self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])
        summary = report.summary
        self.assertEqual(summary["accepted_spend_aed"], "13550.50")
        self.assertEqual(summary["accepted_revenue_aed"], "29400.00")
        self.assertEqual(summary["attributed_revenue_aed"], "21250.00")
        self.assertEqual(summary["attribution_coverage_percent"], "72.3")
        self.assertEqual(summary["unattributed_or_invalid_revenue_aed"], "8150.00")
        self.assertEqual(summary["refunded_revenue_aed"], "-500.00")
        channels = {row["channel"]: row for row in report.channels}
        self.assertEqual(
            (channels["Meta"]["spend_aed"], channels["Meta"]["attributed_revenue_aed"], channels["Meta"]["assumption_dependent_roas"]),
            ("9950.50", "15250.00", "1.53"),
        )
        self.assertEqual(
            (channels["Paid Search"]["spend_aed"], channels["Paid Search"]["assumption_dependent_roas"]), ("3600.00", "1.67")
        )
        statuses = {ref(row): row.status for row in report.dispositions}
        self.assertEqual(statuses["ads/google_ads.csv:4"], "rejected", "a USD cost in an AED export must not count")
        self.assertEqual(statuses["ads/google_ads.csv:5"], "duplicate")
        self.assertEqual(statuses["crm/hubspot_contacts.csv:5"], "rejected", "a lead with no campaign")
        self.assertEqual(statuses["revenue/xero_invoices.csv:6"], "accepted", "the credit note is a declared refund")
        for line in (5, 7, 8):
            self.assertEqual(statuses[f"revenue/xero_invoices.csv:{line}"], "accepted-unattributed")
        self.assertEqual(len(statuses), 20)

    def test_e02_cli_and_standalone_checker_run_from_the_config(self):
        output = self.root / "cli"
        run = subprocess.run(
            [sys.executable, "-m", "revenue_evidence.cli", "--config", str(EXAMPLE / "engagement.json"), "--output", str(output)],
            cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        check = subprocess.run(
            [sys.executable, "-I", str(ROOT / "src" / "revenue_evidence" / "reconstruct.py"),
             "--config", str(EXAMPLE / "engagement.json"), "--output", str(output)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertIn("exceptions.csv", json.loads((output / "evaluation_results.json").read_text())["published_outputs"])

    def test_e03_native_exports_without_a_config_name_the_columns_found(self):
        with self.assertRaises(InputContractError) as caught:
            EvidenceEngine().run(EXAMPLE / "meta_ads.csv", EXAMPLE / "hubspot_contacts.csv", EXAMPLE / "xero_invoices.csv")
        message = str(caught.exception)
        self.assertIn("meta_ads.csv is missing required columns", message)
        self.assertIn("'Reporting starts'", message)
        self.assertIn("--config", message)

    def test_e04_a_config_column_that_is_absent_names_file_field_and_columns_found(self):
        config = self.example()
        edit_config(config, lambda c: c["sources"]["ads"][0]["columns"].update(spend_aed="Amount spent"))
        with self.assertRaises(InputContractError) as caught:
            self.run_config(config)
        message = str(caught.exception)
        for fragment in ("meta_ads.csv is missing 'Amount spent' (mapped to spend_aed)", "Found columns", "'Amount spent (AED)'"):
            self.assertIn(fragment, message)

    def test_e05_values_that_break_the_declared_format_are_still_rejected(self):
        config = self.example()
        replace_text(config.parent / "meta_ads.csv", "01/07/2026,31/07/2026,Retargeting", "2026-07-01,31/07/2026,Retargeting")
        edit_config(config, lambda c: c["sources"]["revenue"][0]["amounts"].update(refunds="reject"))
        report = self.run_config(config)
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual(rows["ads/meta_ads.csv:3"].status, "rejected")
        self.assertIn("declared format DD/MM/YYYY", rows["ads/meta_ads.csv:3"].reason)
        self.assertEqual(rows["revenue/xero_invoices.csv:6"].status, "rejected")
        self.assertIn("no negative amounts", rows["revenue/xero_invoices.csv:6"].reason)
        self.assertNotIn("-500", rows["revenue/xero_invoices.csv:6"].reason, "reasons must not copy row values")


class ConfigValidationTests(EngagementTestCase):
    def assert_refused(self, change, fragment: str) -> None:
        config = self.example(f"case-{len(list(self.root.iterdir()))}")
        edit_config(config, change)
        with self.assertRaises(InputContractError) as caught:
            load_engagement(config)
        self.assertIn(fragment, str(caught.exception))

    def test_e06_invalid_configs_are_refused_with_the_reason(self):
        ads = lambda c: c["sources"]["ads"]  # noqa: E731
        cases = [
            (lambda c: c.update(extra=1), "unknown keys: extra"),
            (lambda c: c.update(config_version=2), "config_version must be 1"),
            (lambda c: c.update(data_origin="real"), "data_origin must be one of"),
            (lambda c: c.update(attribution_windows_days=[90, 30]), "attribution_windows_days"),
            (lambda c: c.update(attribution_windows_days=[0, 30]), "attribution_windows_days"),
            (lambda c: c.update(revenue_join="email"), "revenue_join"),
            (lambda c: c["sources"].pop("crm"), "exactly ads, crm and revenue"),
            (lambda c: ads(c)[0].update(file="../meta_ads.csv"), "relative path"),
            (lambda c: ads(c)[0].update(file="missing.csv"), "does not exist"),
            (lambda c: ads(c)[0]["columns"].update(amount="Amount spent (AED)"), "cannot declare amount"),
            (lambda c: ads(c)[1]["columns"].pop("channel"), "exactly one column or fixed value: channel"),
            (lambda c: ads(c)[0]["fixed"].update(campaign_id="META-101"), "cannot declare campaign_id"),
            (lambda c: ads(c)[0].update(date_format="DD.MM.YYYY"), "date_format must be one of"),
            (lambda c: ads(c)[0]["amounts"].update(currency={"code": "usd"}), "three-letter code"),
            (lambda c: ads(c)[0]["amounts"].update(currency={"code": "USD", "aed_per_unit": "3.6725"}), "rate_source"),
            (lambda c: ads(c)[0]["amounts"].update(currency={"code": "USD", "aed_per_unit": 3.6725, "rate_source": "peg"}), "in quotes"),
            (lambda c: ads(c)[0]["amounts"].update(currency={"code": "AED", "aed_per_unit": "1.1"}), "no conversion rate"),
            (lambda c: ads(c)[1]["amounts"].update(currency_label="USD"), "currency_label must be AED"),
            (lambda c: ads(c)[0]["amounts"].update(refunds="negative_values"), "only revenue exports hold refunds"),
            (lambda c: c["sources"]["crm"][0].update(uppercase=["status"]), "uppercase may list only"),
            (lambda c: c.update(revenue_join="customer_id"), "exactly one column or fixed value: customer_id"),
            (lambda c: c.update(channel_aliases={"Search": "Paid Search", "search": "Paid Search"}), "letter case"),
        ]
        for change, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assert_refused(change, fragment)

    def test_e07_duplicate_keys_in_the_config_are_refused(self):
        config = self.example()
        text = config.read_text(encoding="utf-8")
        config.write_text(text.replace('"data_origin": "synthetic",', '"data_origin": "synthetic", "data_origin": "client",'), encoding="utf-8")
        with self.assertRaisesRegex(InputContractError, "duplicate keys: data_origin"):
            load_engagement(config)


class TamperingTests(EngagementTestCase):
    def test_e08_changes_after_publication_are_detected(self):
        config = self.example()
        _, evaluation = self.publish(config)
        self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])
        output = self.root / "out"
        pristine = {path: path.read_bytes() for path in [*output.iterdir(), *config.parent.iterdir()]}

        def restore() -> None:
            for path, content in pristine.items():
                path.write_bytes(content)

        cases = [
            ("config changed", lambda: edit_config(config, lambda c: c["sources"]["crm"][0].update(uppercase=[])),
             "engagement config changed"),
            ("export changed", lambda: replace_text(config.parent / "xero_invoices.csv", '"12,500.00"', '"12,500.01"'),
             "revenue/xero_invoices.csv export changed"),
            ("exception amount edited", lambda: edit_config(output / "report.json", lambda r: r["exceptions"][0].update(amount_aed="4000.00")),
             "exceptions:"),
            ("exceptions.csv edited", lambda: replace_text(output / "exceptions.csv", "4400.00", "440.00"),
             "exceptions.csv does not match"),
            ("brief exception row removed", lambda: replace_text(output / "executive_brief.md", "| 1 | 4,400.00 |", "| 1 | 4,000.00 |"),
             "executive_brief:"),
        ]
        for label, tamper, fragment in cases:
            with self.subTest(case=label):
                restore()
                tamper()
                _, failures = verify_outputs({"config": str(config)}, output)
                self.assertTrue(any(fragment in failure for failure in failures), failures)
        restore()
        self.assertEqual(verify_outputs({"config": str(config)}, output)[1], [])

    def test_e09_a_config_report_cannot_be_verified_as_the_plain_contract(self):
        config = self.example()
        self.publish(config)
        _, failures = verify_outputs(
            {"ads": str(config.parent / "meta_ads.csv"), "crm": str(config.parent / "hubspot_contacts.csv"),
             "revenue": str(config.parent / "xero_invoices.csv")},
            self.root / "out",
        )
        self.assertTrue(failures)


class BehaviourTests(EngagementTestCase):
    def test_e10_file_order_in_the_config_changes_no_figure_or_status(self):
        first = self.example("first")
        second = self.example("second")
        edit_config(second, lambda c: c["sources"]["ads"].reverse())
        a, b = self.run_config(first), self.run_config(second)
        self.assertEqual({c.evidence_id: c.value for c in a.claims}, {c.evidence_id: c.value for c in b.claims})
        self.assertEqual(a.summary, b.summary)
        self.assertEqual({ref(row): row.status for row in a.dispositions}, {ref(row): row.status for row in b.dispositions})
        self.assertNotEqual(a.run_id, b.run_id)

    def test_e11_exceptions_group_rows_by_reason_largest_first(self):
        config = self.example()
        report, evaluation = self.publish(config)
        self.assertEqual(evaluation["status"], "PASS")
        self.assertEqual([item["amount_aed"] for item in report.exceptions[:4]], ["4400.00", "2000.00", "1750.00", "1100.00"])
        self.assertEqual(
            [item["who_can_fix"] for item in report.exceptions[:4]],
            ["Advertising account owner", "CRM owner", "CRM owner", "Advertising account owner"],
        )
        self.assertEqual(report.exceptions[4]["rows_without_amount"], 1)
        brief = (self.root / "out" / "executive_brief.md").read_text(encoding="utf-8")
        self.assertIn("| 1 | 4,400.00 | accepted-unattributed |", brief)
        self.assertIn("| 1 | — | rejected |", brief)
        self.assertIn("## Declared interpretations", brief)
        self.assertIn("Synthetic Example", brief)

    def test_e12_attribution_windows_come_from_the_config(self):
        config = self.example()
        edit_config(config, lambda c: c.update(attribution_windows_days=[7, 180]))
        report, evaluation = self.publish(config)
        self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])
        values = {claim.evidence_id: claim.value for claim in report.claims}
        self.assertEqual(values["EV-WINATTR-007"], "3250.00")
        self.assertEqual(values["EV-WINEXCL-007"], "18000.00")
        self.assertEqual(values["EV-WINATTR-180"], "21250.00")
        self.assertNotIn("EV-WINATTR-030", values)
        self.assertEqual(report.ruleset["attribution_windows_days"], [7, 180])


class ClientDataGuardTests(EngagementTestCase):
    def test_e13_paths_inside_a_checkout_are_refused(self):
        checkout = self.root / "checkout"
        (checkout / "data").mkdir(parents=True)
        (checkout / "pyproject.toml").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(InputContractError, "outside the code repository"):
            ensure_outside_repository([checkout / "data" / "client.csv"], checkout)
        ensure_outside_repository([self.root / "elsewhere" / "client.csv"], checkout)
        ensure_outside_repository([self.root / "plain" / "client.csv"], self.root / "plain")

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(list(arguments))
        return code, errors.getvalue()

    def test_e14_the_cli_refuses_client_exports_inside_the_repository(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        (checkout / "pyproject.toml").write_text("", encoding="utf-8")
        shutil.copytree(EXAMPLE, checkout / "engagement")
        config = checkout / "engagement" / "engagement.json"
        edit_config(config, lambda c: c.update(data_origin="client"))
        with mock.patch.object(cli, "REPOSITORY", checkout):
            code, errors = self.run_cli("--config", str(config), "--output", str(self.root / "out"))
        self.assertEqual(code, 2)
        self.assertIn("outside the code repository", errors)

    def test_e15_the_ai_narrative_needs_recorded_permission_for_client_data(self):
        config = self.example()
        edit_config(config, lambda c: c.update(data_origin="client"))
        with mock.patch.object(cli, "REPOSITORY", self.root / "checkout"):
            code, errors = self.run_cli("--config", str(config), "--output", str(self.root / "out"), "--azure-narrative")
        self.assertEqual(code, 2)
        self.assertIn("written permission", errors)


if __name__ == "__main__":
    unittest.main()

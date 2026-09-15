"""Stage 2: refunds, currencies, customer joins and declared formats.

Each case publishes through the independent checker, so every figure below is agreed
by both implementations, not only computed by the engine.
"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import random
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from revenue_evidence.engine import (  # noqa: E402
    REASON_CHRONOLOGY,
    REASON_CONFLICTING_CUSTOMER,
    REASON_CUSTOMER_CAMPAIGNS,
    REASON_NO_CUSTOMER,
    EvidenceEngine,
    InputContractError,
    load_engagement,
    write_outputs,
)
from test_generated import generate  # noqa: E402

EXAMPLE = ROOT / "examples" / "messy-exports"
DATA = ROOT / "data" / "synthetic"


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def ref(row) -> str:
    return f"{row.source}/{row.source_file}:{row.source_row}" if row.source_file else f"{row.source}:{row.source_row}"


def edit_json(path: Path, change) -> None:
    content = json.loads(path.read_text(encoding="utf-8"))
    change(content)
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")


class FormatTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def publish(self, config: Path, name: str = "out"):
        report = EvidenceEngine().run_engagement(load_engagement(config))
        evaluation = write_outputs(report, self.root / name)
        self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])
        return report

    def example(self) -> Path:
        shutil.copytree(EXAMPLE, self.root / "example")
        return self.root / "example" / "engagement.json"


class RefundAndCurrencyTests(FormatTestCase):
    def test_f01_refunds_in_parentheses_and_in_a_credit_note_export(self):
        config = self.example()
        invoices = config.parent / "xero_invoices.csv"
        invoices.write_text(invoices.read_text(encoding="utf-8").replace(",-500.00,", ",(500.00),"), encoding="utf-8")
        write_csv(
            config.parent / "credit_notes.csv",
            ["CreditNoteNumber", "Reference", "Date", "Amount"],
            [["CN-0002", "L-1002", "05/08/2026", "1,000.00"], ["CN-0003", "L-1003", "20/07/2026", "-50.00"]],
        )
        edit_json(config, lambda c: c["sources"]["revenue"].append({
            "file": "credit_notes.csv",
            "columns": {"transaction_id": "CreditNoteNumber", "lead_id": "Reference", "value_aed": "Amount", "date": "Date"},
            "date_format": "DD/MM/YYYY",
            "amounts": {"thousands_separator": ",", "refunds": "whole_file"},
        }))
        report = self.publish(config)
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual((rows["revenue/xero_invoices.csv:6"].status, rows["revenue/xero_invoices.csv:6"].amount_aed), ("accepted", "-500.00"))
        self.assertEqual((rows["revenue/credit_notes.csv:2"].status, rows["revenue/credit_notes.csv:2"].amount_aed), ("accepted", "-1000.00"))
        self.assertEqual(rows["revenue/credit_notes.csv:3"].status, "rejected", "a credit-note file cannot also hold a minus sign")
        self.assertEqual(report.summary["accepted_revenue_aed"], "28400.00")
        self.assertEqual(report.summary["refunded_revenue_aed"], "-1500.00")
        self.assertEqual(report.summary["attributed_revenue_aed"], "20250.00")
        refund = next(claim for claim in report.claims if claim.evidence_id == "EV-REFUND-001")
        self.assertEqual(set(refund.source_refs), {"revenue/xero_invoices.csv:6", "revenue/credit_notes.csv:2"})

    def test_f02_a_usd_ad_account_is_converted_at_the_declared_rate(self):
        config = self.example()
        write_csv(
            config.parent / "tiktok_ads_usd.csv",
            ["Date", "Campaign", "Spend (USD)"],
            [["2026-07-03", "TT-301", "1,000.00"], ["2026-07-04", "TT-301", "333.33"],
             ["2026-07-05", "TT-301", "USD 50.00"], ["2026-07-06", "TT-301", "AED 50.00"]],
        )
        edit_json(config, lambda c: c["sources"]["ads"].append({
            "file": "tiktok_ads_usd.csv",
            "columns": {"date": "Date", "campaign_id": "Campaign", "spend_aed": "Spend (USD)"},
            "fixed": {"channel": "TikTok"},
            "amounts": {"thousands_separator": ",", "currency_label": "USD",
                        "currency": {"code": "USD", "aed_per_unit": "3.6725", "rate_source": "UAE dirham peg to the US dollar"}},
        }))
        report = self.publish(config)
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual([rows[f"ads/tiktok_ads_usd.csv:{line}"].amount_aed for line in (2, 3, 4)], ["3672.50", "1224.15", "183.63"])
        self.assertEqual(rows["ads/tiktok_ads_usd.csv:5"].status, "rejected", "an AED amount in a USD export is not converted")
        channels = {row["channel"]: row for row in report.channels}
        self.assertEqual(channels["TikTok"]["spend_aed"], "5080.28")
        brief = (self.root / "out" / "executive_brief.md").read_text(encoding="utf-8")
        self.assertIn("USD converted at 3.6725 AED per unit", brief)

    def test_f03_rejected_rows_keep_readable_amounts_for_the_owner(self):
        config = self.example()
        invoices = config.parent / "xero_invoices.csv"
        invoices.write_text(invoices.read_text(encoding="utf-8").replace("INV-0004,L-1004,", "INV-0004,,"), encoding="utf-8")
        report = self.publish(config)
        row = next(row for row in report.dispositions if ref(row) == "revenue/xero_invoices.csv:5")
        self.assertEqual((row.status, row.amount_aed), ("rejected", "2000.00"))
        group = next(item for item in report.exceptions if item["reason"] == "transaction_id and lead_id are required")
        self.assertEqual((group["amount_aed"], group["rows_without_amount"]), ("2000.00", 0))
        self.assertNotIn("2000.00", [claim.value for claim in report.claims if claim.evidence_id == "EV-REV-001"])


class CustomerJoinTests(FormatTestCase):
    def build(self, rule: str | None = None) -> Path:
        folder = self.root / f"customers-{rule or 'default'}"
        folder.mkdir()
        write_csv(folder / "ads.csv", ["date", "campaign_id", "channel", "spend_aed"],
                  [["2026-07-01", "C-SEARCH", "Paid Search", "1000"], ["2026-07-01", "C-SOCIAL", "Paid Social", "500"]])
        write_csv(folder / "crm.csv", ["lead_id", "campaign_id", "created_at", "status", "customer_id"], [
            ["L1", "C-SEARCH", "2026-07-02", "won", "CUST-1"],
            ["L2", "C-SEARCH", "2026-07-20", "won", "CUST-1"],
            ["L3", "C-SEARCH", "2026-07-03", "won", "CUST-2"],
            ["L4", "C-SOCIAL", "2026-07-12", "won", "CUST-2"],
            ["L5", "C-SOCIAL", "2026-07-05", "won", "CUST-3"],
            ["L5", "C-SEARCH", "2026-07-05", "won", "CUST-3"],
            ["L6", "C-SOCIAL", "2026-07-06", "won", ""],
        ])
        write_csv(folder / "orders.csv", ["order_id", "customer_id", "value_aed", "date"], [
            ["O1", "CUST-1", "300", "2026-07-10"], ["O2", "CUST-1", "200", "2026-07-25"],
            ["O3", "CUST-2", "400", "2026-07-10"], ["O4", "CUST-3", "250", "2026-07-10"],
            ["O5", "CUST-9", "100", "2026-07-10"], ["O6", "CUST-1", "50", "2026-07-01"],
            ["O7", "CUST-2", "600", "2026-07-15"],
        ])
        config = {
            "config_version": 1, "engagement": "Customer join", "data_origin": "synthetic", "revenue_join": "customer_id",
            "sources": {
                "ads": [{"file": "ads.csv", "columns": {name: name for name in ("date", "campaign_id", "channel", "spend_aed")}}],
                "crm": [{"file": "crm.csv", "columns": {name: name for name in ("lead_id", "campaign_id", "created_at", "status", "customer_id")}}],
                "revenue": [{"file": "orders.csv", "columns": {"transaction_id": "order_id", "customer_id": "customer_id",
                                                               "value_aed": "value_aed", "date": "date"}}],
            },
        }
        if rule:
            config["multiple_campaigns"] = rule
        (folder / "engagement.json").write_text(json.dumps(config), encoding="utf-8")
        return folder / "engagement.json"

    def test_f04_revenue_joins_crm_leads_through_the_customer(self):
        report = self.publish(self.build())
        rows = {ref(row): row for row in report.dispositions}
        reasons = {line: rows[f"revenue/orders.csv:{line}"].reason for line in range(2, 9)}
        self.assertEqual(rows["revenue/orders.csv:2"].status, "accepted")
        self.assertEqual(reasons[4], REASON_CUSTOMER_CAMPAIGNS)
        self.assertEqual(reasons[5], REASON_CONFLICTING_CUSTOMER)
        self.assertEqual(reasons[6], REASON_NO_CUSTOMER)
        self.assertEqual(reasons[7], REASON_CHRONOLOGY)
        self.assertEqual(rows["crm/crm.csv:8"].reason, "customer_id is required")
        self.assertEqual(report.summary["accepted_revenue_aed"], "1900.00")
        self.assertEqual(report.summary["attributed_revenue_aed"], "500.00")
        self.assertEqual(report.summary["conflicting_lead_revenue_aed"], "250.00")
        self.assertEqual(report.summary["attribution_coverage_percent"], "26.3")
        claims = {claim.evidence_id: claim for claim in report.claims}
        joins = {entry["source_ref"] for entry in claims["EV-ATTR-001"].lineage if entry["role"] == "join"}
        self.assertEqual(joins, {"crm/crm.csv:2", "crm/crm.csv:3"}, "every lead of the customer is join evidence")
        conflict_joins = {entry["source_ref"] for entry in claims["EV-CONFLICT-001"].lineage if entry["role"] == "join"}
        self.assertEqual(conflict_joins, {"crm/crm.csv:6", "crm/crm.csv:7"})

    def test_f05_declared_first_and_last_touch_rules_credit_repeat_customers(self):
        first = self.publish(self.build("first_touch"), "first")
        last = self.publish(self.build("last_touch"), "last")
        channel = lambda report: {row["channel"]: row["attributed_revenue_aed"] for row in report.channels}  # noqa: E731
        self.assertEqual(channel(first), {"Paid Search": "1500.00", "Paid Social": "0.00"})
        self.assertEqual(channel(last), {"Paid Search": "900.00", "Paid Social": "600.00"})
        for report in (first, last):
            self.assertEqual(report.summary["attribution_coverage_percent"], "78.9")
            self.assertIn("credited to the", " ".join(next(c for c in report.claims if c.evidence_id == "EV-ATTR-001").assumptions))

    def test_f06_a_campaign_rule_needs_a_customer_join(self):
        config = self.example()
        edit_json(config, lambda c: c.update(multiple_campaigns="first_touch"))
        with self.assertRaisesRegex(InputContractError, "applies only with revenue_join customer_id"):
            load_engagement(config)


def iso_to(text: str, fmt: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text.strip()):
        return text
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        return text
    return {
        "DD/MM/YYYY": f"{day.day}/{day.month}/{day.year}",
        "MM/DD/YYYY": f"{day.month:02d}/{day.day:02d}/{day.year}",
        "MM/DD/YYYY HH:MM": f"{day.month}/{day.day}/{day.year} 9:30",
        "YYYY-MM-DD HH:MM:SS": f"{day.isoformat()} 08:15:00",
    }[fmt]


def money_to(text: str, separator: str = ",", label: str = "AED") -> str:
    plain = text.strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", plain):
        return text
    whole, point, fraction = plain.partition(".")
    grouped = f"{int(whole):,}" if separator else str(int(whole))
    return f"{label} {grouped}{point}{fraction}".strip()


class EquivalenceTests(FormatTestCase):
    def test_f07_the_synthetic_case_as_messy_native_exports_gives_identical_results(self):
        folder = self.root / "messy"
        folder.mkdir()

        def rows(name: str) -> list[list[str]]:
            with (DATA / f"{name}.csv").open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.reader(handle))[1:]

        write_csv(folder / "ads.csv", ["Day", "Campaign", "Type", "Cost"],
                  [[iso_to(d, "DD/MM/YYYY"), c.lower(), ch.lower(), money_to(s)] for d, c, ch, s in rows("ads")])
        write_csv(folder / "crm.csv", ["Record", "Campaign", "Created", "Stage"],
                  [[lead.lower(), c.lower(), iso_to(created, "MM/DD/YYYY HH:MM"), status] for lead, c, created, status in rows("crm")])
        write_csv(folder / "revenue.csv", ["Invoice", "Lead", "Amount", "Invoice date"],
                  [[t.lower(), lead.lower(), money_to(v), iso_to(d, "DD/MM/YYYY")] for t, lead, v, d in rows("revenue")])
        config = {
            "config_version": 1, "engagement": "Synthetic case as messy exports", "data_origin": "synthetic",
            "channel_aliases": {"paid search": "Paid Search", "paid social": "Paid Social", "email": "Email"},
            "sources": {
                "ads": [{"file": "ads.csv", "columns": {"date": "Day", "campaign_id": "Campaign", "channel": "Type", "spend_aed": "Cost"},
                         "date_format": "DD/MM/YYYY", "amounts": {"thousands_separator": ",", "currency_label": "AED"},
                         "uppercase": ["campaign_id"]}],
                "crm": [{"file": "crm.csv", "columns": {"lead_id": "Record", "campaign_id": "Campaign", "created_at": "Created", "status": "Stage"},
                         "date_format": "MM/DD/YYYY HH:MM", "uppercase": ["lead_id", "campaign_id"]}],
                "revenue": [{"file": "revenue.csv", "columns": {"transaction_id": "Invoice", "lead_id": "Lead", "value_aed": "Amount", "date": "Invoice date"},
                             "date_format": "DD/MM/YYYY", "amounts": {"thousands_separator": ",", "currency_label": "AED"},
                             "uppercase": ["transaction_id", "lead_id"]}],
            },
        }
        (folder / "engagement.json").write_text(json.dumps(config), encoding="utf-8")
        messy = self.publish(folder / "engagement.json")
        plain = EvidenceEngine().run(DATA / "ads.csv", DATA / "crm.csv", DATA / "revenue.csv")
        self.assertEqual({c.evidence_id: c.value for c in messy.claims}, {c.evidence_id: c.value for c in plain.claims})
        self.assertEqual(messy.summary, plain.summary)
        self.assertEqual(
            {(row.source, row.source_row): (row.status, row.amount_aed) for row in messy.dispositions},
            {(row.source, row.source_row): (row.status, row.amount_aed) for row in plain.dispositions},
        )
        shape = lambda report: sorted((i["source"], i["status"], i["rows"], i["amount_aed"]) for i in report.exceptions)  # noqa: E731
        self.assertEqual(shape(messy), shape(plain))

    def test_f08_engine_and_checker_agree_on_generated_data_under_declared_formats(self):
        rng = random.Random(20260916)
        published = refused = 0
        for index in range(60):
            data = generate(rng)
            folder = self.root / f"set{index}"
            folder.mkdir()
            fmt = rng.choice(["DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD HH:MM:SS"])
            separator = rng.choice(["", ","])
            label = rng.choice(["", "AED"])
            refunds = rng.choice(["reject", "negative_values"])

            def amount(text: str) -> str:
                cell = money_to(text, separator, label) if rng.random() < 0.8 else text
                if refunds == "negative_values" and rng.random() < 0.1 and re.search(r"[0-9]", cell):
                    cell = rng.choice([f"-{cell}", f"({cell})"])
                return cell

            write_csv(folder / "ads.csv", ["Day", "Campaign", "Channel", "Cost"],
                      [[iso_to(d, fmt), c, ch, amount(s)] for d, c, ch, s in data["ads"]])
            write_csv(folder / "crm.csv", ["Lead", "Campaign", "Created", "Status"],
                      [[lead, c, iso_to(created, fmt), status] for lead, c, created, status in data["crm"]])
            write_csv(folder / "revenue.csv", ["Transaction", "Lead", "Value", "Date"],
                      [[t, lead, amount(v), iso_to(d, fmt)] for t, lead, v, d in data["revenue"]])
            amounts = {"thousands_separator": separator, "currency_label": label}
            config = {
                "config_version": 1, "engagement": f"Generated {index}", "data_origin": "synthetic",
                "sources": {
                    "ads": [{"file": "ads.csv", "columns": {"date": "Day", "campaign_id": "Campaign", "channel": "Channel", "spend_aed": "Cost"},
                             "date_format": fmt, "amounts": amounts}],
                    "crm": [{"file": "crm.csv", "columns": {"lead_id": "Lead", "campaign_id": "Campaign", "created_at": "Created", "status": "Status"},
                             "date_format": fmt}],
                    "revenue": [{"file": "revenue.csv", "columns": {"transaction_id": "Transaction", "lead_id": "Lead", "value_aed": "Value", "date": "Date"},
                                 "date_format": fmt, "amounts": {**amounts, "refunds": refunds}}],
                },
            }
            (folder / "engagement.json").write_text(json.dumps(config), encoding="utf-8")
            with self.subTest(dataset=index):
                report = EvidenceEngine().run_engagement(load_engagement(folder / "engagement.json"))
                evaluation = write_outputs(report, folder / "out")
                if evaluation["status"] == "PASS":
                    published += 1
                else:
                    refused += 1
                    self.assertTrue(all(f.startswith("sufficient_evidence") for f in evaluation["failures"]), evaluation["failures"])
        self.assertGreaterEqual(published, 20, "too few datasets published for the agreement check to mean anything")
        self.assertGreaterEqual(refused, 1)


class EngagementScriptTests(FormatTestCase):
    def load_script(self):
        spec = importlib.util.spec_from_file_location("engagement_script", ROOT / "scripts" / "engagement.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def call(self, module, *arguments: str, repository: Path) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return module.main(list(arguments), repository=repository)

    def test_f09_init_intake_run_and_deletion_check(self):
        module = self.load_script()
        checkout = self.root / "checkout"
        checkout.mkdir()
        (checkout / "pyproject.toml").write_text("", encoding="utf-8")
        self.assertEqual(self.call(module, "init", str(checkout / "engagement"), "--name", "Inside", repository=checkout), 2)

        folder = self.root / "engagement"
        self.assertEqual(self.call(module, "init", str(folder), "--name", "Rehearsal client", repository=checkout), 0)
        with self.assertRaises(InputContractError):
            load_engagement(folder / "inputs" / "engagement.json")
        for path in EXAMPLE.glob("*.csv"):
            shutil.copy(path, folder / "inputs" / path.name)
        config = json.loads((EXAMPLE / "engagement.json").read_text(encoding="utf-8"))
        config.update(data_origin="client", engagement="Rehearsal client")
        (folder / "inputs" / "engagement.json").write_text(json.dumps(config), encoding="utf-8")

        self.assertEqual(self.call(module, "run", str(folder), repository=checkout), 2, "unlogged exports must not run")
        self.assertEqual(self.call(module, "intake", str(folder), "--received-on", "2026-09-16", "--received-from", "Finance", repository=checkout), 0)
        with (folder / "records" / "intake_log.csv").open(encoding="utf-8", newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 4)
        self.assertEqual(self.call(module, "run", str(folder), repository=checkout), 0)
        evaluation = json.loads((folder / "outputs" / "evaluation_results.json").read_text(encoding="utf-8"))
        self.assertEqual(evaluation["status"], "PASS")

        with (folder / "inputs" / "xero_invoices.csv").open("a", encoding="utf-8") as handle:
            handle.write("INV-0099,L-1001,21/07/2026,10.00,PAID\n")
        self.assertEqual(self.call(module, "run", str(folder), repository=checkout), 2, "a changed export must not run")

        self.assertEqual(self.call(module, "deletion-check", str(folder), repository=checkout), 1)
        for part in ("inputs", "outputs"):
            for path in (folder / part).iterdir():
                path.unlink()
        self.assertEqual(self.call(module, "deletion-check", str(folder), repository=checkout), 0)


if __name__ == "__main__":
    unittest.main()

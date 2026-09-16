"""Stage 2: refunds, currencies, customer joins and declared formats.

Each case publishes through the independent checker, so every figure below is agreed
by both implementations, not only computed by the engine.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import random
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from unittest import mock  # noqa: E402

from revenue_evidence import cli, engine, reconstruct  # noqa: E402
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


class LineItemAndFilterTests(FormatTestCase):
    """Exports as they actually arrive: one row per order line, unpaid invoices, ad set rows."""

    def build(self, name: str = "lines", threshold: str | None = None, filter_status: bool = True,
              unattributed_invoice: bool = False) -> Path:
        folder = self.root / name
        folder.mkdir()
        write_csv(folder / "ads.csv", ["Day", "Ad set ID", "Campaign", "Type", "Cost"], [
            ["2026-07-01", "AS-1", "C-1", "Search", "100"],
            ["2026-07-01", "AS-2", "C-1", "Search", "100"],
            ["2026-07-01", "AS-2", "C-1", "Search", "100"],
            ["2026-07-02", "AS-3", "C-1", "Search", "50"],
            ["2026-07-02", "AS-3", "C-1", "Search", "60"],
        ])
        write_csv(folder / "crm.csv", ["lead_id", "campaign_id", "created_at", "status"], [["L1", "C-1", "2026-07-01", "won"]])
        orders = [
            ["INV-1", "1", "L1", "100.00", "2026-07-05", "PAID"],
            ["INV-1", "2", "L1", "50.00", "2026-07-05", "PAID"],
            ["INV-2", "1", "L1", "80.00", "2026-07-06", "PAID"],
            ["INV-2", "2", "L1", "20.00", "2026-07-07", "PAID"],
            ["INV-3", "1", "L1", "30.00", "2026-07-08", "AUTHORISED"],
            ["INV-4", "1", "L1", "-25.00", "2026-07-09", "PAID"],
            ["INV-1", "2", "L1", "50.00", "2026-07-05", "PAID"],
        ]
        if unattributed_invoice:
            # A sale for a lead the CRM does not contain: revenue without attribution.
            orders.append(["INV-5", "1", "L9", "25.00", "2026-07-10", "PAID"])
        write_csv(folder / "orders.csv", ["Invoice", "Line", "Lead", "Amount", "Date", "Status"], orders)
        revenue = {
            "file": "orders.csv",
            "columns": {"transaction_id": "Invoice", "line_id": "Line", "lead_id": "Lead", "value_aed": "Amount", "date": "Date"},
            "amounts": {"refunds": "negative_values"},
        }
        if filter_status:
            revenue["include_when"] = {"column": "Status", "values": ["PAID"]}
        config = {
            "config_version": 1, "engagement": "Line items", "data_origin": "synthetic",
            "sources": {
                "ads": [{"file": "ads.csv", "columns": {"date": "Day", "row_id": "Ad set ID", "campaign_id": "Campaign",
                                                        "channel": "Type", "spend_aed": "Cost"}}],
                "crm": [{"file": "crm.csv", "columns": {name: name for name in ("lead_id", "campaign_id", "created_at", "status")}}],
                "revenue": [revenue],
            },
        }
        if threshold:
            config["coverage_threshold_percent"] = threshold
        (folder / "engagement.json").write_text(json.dumps(config), encoding="utf-8")
        return folder / "engagement.json"

    def test_f10_line_items_become_invoices_without_losing_the_lines(self):
        report = self.publish(self.build())
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual(rows["revenue/orders.csv:2"].status, "accepted")
        self.assertEqual(rows["revenue/orders.csv:8"].status, "duplicate", "the same invoice line twice is a duplicate")
        for line in (4, 5):
            self.assertEqual(rows[f"revenue/orders.csv:{line}"].status, "conflict", "lines of one invoice must agree")
            self.assertIn("across its lines", rows[f"revenue/orders.csv:{line}"].reason)
        self.assertEqual(report.summary["accepted_revenue_aed"], "125.00", "two lines of INV-1 less the refund")
        claims = {claim.evidence_id: claim for claim in report.claims}
        self.assertEqual(claims["EV-REV-001"].source_refs,
                         ["revenue/orders.csv:2", "revenue/orders.csv:3", "revenue/orders.csv:7"])
        self.assertEqual(claims["EV-REFUND-001"].value, "-25.00")
        self.assertEqual(report.summary["attributed_revenue_aed"], "125.00")

    def test_f11_a_declared_filter_excludes_unpaid_invoices_and_says_so(self):
        report = self.publish(self.build())
        row = next(row for row in report.dispositions if ref(row) == "revenue/orders.csv:6")
        self.assertEqual((row.status, row.amount_aed), ("filtered", "30.00"))
        self.assertIn("Status is not one of the declared values", row.reason)
        finding = " ".join(report.recommendation["findings"])
        self.assertIn("excluded by a declared filter", finding)
        self.assertIn("AED 30.00", finding)
        unfiltered = self.publish(self.build("unfiltered", filter_status=False), "out2")
        self.assertEqual(unfiltered.summary["accepted_revenue_aed"], "155.00", "without the filter the unpaid invoice counts")

    def test_f12_an_advertising_row_identifier_separates_genuine_repeats(self):
        report = self.publish(self.build())
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual([rows[f"ads/ads.csv:{line}"].status for line in range(2, 7)],
                         ["accepted", "accepted", "duplicate", "conflict", "conflict"])
        self.assertEqual(report.summary["accepted_spend_aed"], "200.00", "identical rows with different ids both count")

    def test_f13_the_coverage_threshold_comes_from_the_config(self):
        lenient = self.publish(self.build("lenient", threshold="50", unattributed_invoice=True), "lenient")
        strict = self.publish(self.build("strict", threshold="99", unattributed_invoice=True), "strict")
        self.assertEqual(lenient.summary["attribution_coverage_percent"], "83.3")
        self.assertEqual(lenient.ruleset["coverage_threshold_percent"], "50")
        self.assertNotIn("below the rule set's reporting threshold", " ".join(lenient.recommendation["findings"]))
        self.assertIn("below the rule set's reporting threshold", " ".join(strict.recommendation["findings"]))
        self.assertNotEqual(lenient.run_id, strict.run_id)

    def test_f15_a_wrong_declaration_is_diagnosed_rather_than_left_silent(self):
        config = self.example()
        edit_json(config, lambda c: c["sources"]["ads"][0].update(date_format="YYYY-MM-DD"))
        edit_json(config, lambda c: c["sources"]["ads"][1].update(date_format="DD/MM/YYYY"))
        edit_json(config, lambda c: c["sources"]["ads"][1]["amounts"].update(thousands_separator=""))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["--config", str(config), "--output", str(self.root / "out")])
        self.assertEqual(code, 2, "no advertising row can be read, so nothing may publish")
        payload = json.loads(errors.getvalue())
        hints = " ".join(payload["hints"])
        self.assertIn('ads/meta_ads.csv: 3 of 3 rejected rows would match date_format "DD/MM/YYYY"', hints)
        self.assertIn('ads/google_ads.csv', hints)
        self.assertIn('date_format "YYYY-MM-DD"', hints)
        self.assertIn('thousands_separator ","', hints)

    def test_f14_optional_identifiers_must_be_declared_for_every_file_of_a_source(self):
        config = self.build("partial")
        shutil.copy(config.parent / "orders.csv", config.parent / "orders_2.csv")
        edit_json(config, lambda c: c["sources"]["revenue"].append({
            "file": "orders_2.csv",
            "columns": {"transaction_id": "Invoice", "lead_id": "Lead", "value_aed": "Amount", "date": "Date"},
        }))
        with self.assertRaisesRegex(InputContractError, "declare line_id in every file"):
            load_engagement(config)


SHEET_XML = """<?xml version="1.0"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>INV-1</t></is></c><c r="B2" t="s"><v>4</v></c><c r="C2"><v>100.5</v></c><c r="D2" s="1"><v>45658</v></c></row>
<row r="4"><c r="A4" t="inlineStr"><is><t>INV-2</t></is></c><c r="B4" t="s"><v>4</v></c><c r="C4"><v>50</v></c><c r="D4" s="1"><v>45659</v></c></row>
</sheetData></worksheet>"""
STRINGS_XML = """<?xml version="1.0"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="5" uniqueCount="5">
<si><t>Invoice</t></si><si><t>Lead</t></si><si><t>Amount</t></si><si><t>Date</t></si><si><t>L1</t></si></sst>"""
STYLES_XML = """<?xml version="1.0"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs></styleSheet>"""
WORKBOOK_XML = """<?xml version="1.0"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Invoices" sheetId="1" r:id="rId1"/></sheets></workbook>"""
WORKBOOK_RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>"""


class SpreadsheetAndSeparatorTests(FormatTestCase):
    """Clients send .xlsx workbooks, and Excel writes semicolons in many locales."""

    def workbook(self, path: Path) -> Path:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", WORKBOOK_XML)
            archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
            archive.writestr("xl/worksheets/sheet1.xml", SHEET_XML)
            archive.writestr("xl/sharedStrings.xml", STRINGS_XML)
            archive.writestr("xl/styles.xml", STYLES_XML)
        return path

    def converter(self):
        spec = importlib.util.spec_from_file_location("xlsx_to_csv", ROOT / "scripts" / "xlsx_to_csv.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_f18_a_workbook_becomes_a_csv_whose_lines_are_the_sheet_rows(self):
        module = self.converter()
        workbook = self.workbook(self.root / "Orders.xlsx")
        target = self.root / "invoices.csv"
        manifest = module.convert(workbook, target)
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "Invoice,Lead,Amount,Date")
        self.assertEqual(lines[1], "INV-1,L1,100.5,2025-01-01", "a date-formatted cell is written as a date")
        self.assertEqual(lines[2], "", "an empty sheet row keeps the numbering")
        self.assertEqual(lines[3], "INV-2,L1,50,2025-01-02")
        self.assertEqual((manifest["rows_written"], manifest["cells_read_as_dates"], manifest["sheet"]), (4, 2, "Invoices"))
        self.assertEqual(manifest["workbook_sha256"], hashlib.sha256(workbook.read_bytes()).hexdigest())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main([str(workbook), "--out", str(self.root / "again.csv")]), 0)
        self.assertTrue((self.root / "again.csv.conversion.json").exists())

    def test_f19_a_converted_workbook_runs_and_its_references_are_sheet_rows(self):
        module = self.converter()
        folder = self.root / "converted"
        folder.mkdir()
        module.convert(self.workbook(self.root / "Orders.xlsx"), folder / "invoices.csv")
        write_csv(folder / "ads.csv", ["date", "campaign_id", "channel", "spend_aed"],
                  [["2024-12-01", "C-1", "Paid Search", "100"]])
        write_csv(folder / "crm.csv", ["lead_id", "campaign_id", "created_at", "status"],
                  [["L1", "C-1", "2024-12-15", "won"]])
        config = {
            "config_version": 1, "engagement": "Converted workbook", "data_origin": "synthetic",
            "sources": {
                "ads": [{"file": "ads.csv", "columns": {name: name for name in ("date", "campaign_id", "channel", "spend_aed")}}],
                "crm": [{"file": "crm.csv", "columns": {name: name for name in ("lead_id", "campaign_id", "created_at", "status")}}],
                "revenue": [{"file": "invoices.csv",
                             "columns": {"transaction_id": "Invoice", "lead_id": "Lead", "value_aed": "Amount", "date": "Date"}}],
            },
        }
        (folder / "engagement.json").write_text(json.dumps(config), encoding="utf-8")
        report = self.publish(folder / "engagement.json")
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual(rows["revenue/invoices.csv:4"].record_id, "INV-2", "sheet row 4 is CSV line 4")
        self.assertEqual(report.summary["accepted_revenue_aed"], "150.50")

    def test_f20_semicolon_and_windows_1252_exports_are_read_when_declared(self):
        folder = self.root / "separated"
        folder.mkdir()
        (folder / "ads.csv").write_bytes(
            "date;campaign_id;channel;spend_aed\n2026-07-01;C-1;Paid Search;100\n".encode("cp1252"))
        (folder / "crm.csv").write_bytes(
            'lead_id;campaign_id;created_at;status\nL1;C-1;2026-07-02;"won;\nfollow-up"\nL2;C-1;2026-07-03;won\n'.encode("cp1252"))
        (folder / "revenue.csv").write_bytes(
            "transaction_id;lead_id;value_aed;date\nT1;L2;300;2026-07-10\n".encode("cp1252"))
        config = {
            "config_version": 1, "engagement": "Semicolon exports", "data_origin": "synthetic",
            "sources": {
                source: [{"file": f"{source}.csv", "delimiter": "semicolon", "encoding": "windows-1252",
                          "columns": {name: name for name in columns}}]
                for source, columns in (
                    ("ads", ("date", "campaign_id", "channel", "spend_aed")),
                    ("crm", ("lead_id", "campaign_id", "created_at", "status")),
                    ("revenue", ("transaction_id", "lead_id", "value_aed", "date")),
                )
            },
        }
        (folder / "engagement.json").write_text(json.dumps(config), encoding="utf-8")
        report = self.publish(folder / "engagement.json")
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual(rows["crm/crm.csv:2"].status, "accepted")
        self.assertEqual(rows["crm/crm.csv:4"].record_id, "L2", "a quoted newline keeps later rows on their own line")
        self.assertEqual(report.summary["attributed_revenue_aed"], "300.00")
        self.assertIn("semicolon-separated windows-1252 text",
                      (self.root / "out" / "executive_brief.md").read_text(encoding="utf-8"))


class DeclaredFormatTests(FormatTestCase):
    """The last shapes real exports arrive in: mixed date styles, totals in parts, per-row currency, offsets."""

    def build(self, folder_name: str, revenue_rows: list[list[str]], revenue_columns: dict, extra: dict | None = None) -> Path:
        folder = self.root / folder_name
        folder.mkdir()
        write_csv(folder / "ads.csv", ["date", "campaign_id", "channel", "spend_aed"],
                  [["2026-07-01", "C-1", "Paid Search", "1000"]])
        write_csv(folder / "crm.csv", ["lead_id", "campaign_id", "created_at", "status"],
                  [["L1", "C-1", "2026-07-01", "won"]])
        write_csv(folder / "revenue.csv", list(revenue_rows[0]), revenue_rows[1:])
        revenue = {"file": "revenue.csv", "columns": revenue_columns, **(extra or {})}
        config = {
            "config_version": 1, "engagement": folder_name, "data_origin": "synthetic",
            "sources": {
                "ads": [{"file": "ads.csv", "columns": {name: name for name in ("date", "campaign_id", "channel", "spend_aed")}}],
                "crm": [{"file": "crm.csv", "columns": {name: name for name in ("lead_id", "campaign_id", "created_at", "status")}}],
                "revenue": [revenue],
            },
        }
        (folder / "engagement.json").write_text(json.dumps(config), encoding="utf-8")
        return folder / "engagement.json"

    def test_f21_a_file_may_declare_more_than_one_date_format(self):
        config = self.build(
            "mixed-dates",
            [["Invoice", "Lead", "Amount", "Date"],
             ["T1", "L1", "100", "2026-07-05"],
             ["T2", "L1", "200", "06/07/2026"],
             ["T3", "L1", "300", "07-07-2026"]],
            {"transaction_id": "Invoice", "lead_id": "Lead", "value_aed": "Amount", "date": "Date"},
            {"date_format": ["YYYY-MM-DD", "DD/MM/YYYY"]},
        )
        report = self.publish(config)
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual(rows["revenue/revenue.csv:2"].status, "accepted")
        self.assertEqual(rows["revenue/revenue.csv:3"].status, "accepted")
        self.assertEqual(rows["revenue/revenue.csv:4"].status, "rejected", "a third style is still not declared")
        edit_json(config, lambda c: c["sources"]["revenue"][0].update(date_format=["DD/MM/YYYY", "MM/DD/YYYY"]))
        with self.assertRaisesRegex(InputContractError, "would be two dates"):
            load_engagement(config)

    def test_f22_a_total_can_be_computed_from_quantity_and_unit_price(self):
        config = self.build(
            "computed",
            [["Invoice", "Line", "Lead", "Quantity", "Unit price", "Date"],
             ["T1", "1", "L1", "3", "10.50", "2026-07-05"],
             ["T1", "2", "L1", "-1", "10.50", "2026-07-06"],
             ["T2", "1", "L1", "2", "5", "2026-07-07"]],
            {"transaction_id": "Invoice", "line_id": "Line", "lead_id": "Lead", "date": "Date"},
            {"amounts": {"value_from": {"multiply": ["Quantity", "Unit price"]}, "refunds": "negative_values"}},
        )
        report = self.publish(config)
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual(rows["revenue/revenue.csv:2"].amount_aed, "31.50")
        self.assertEqual(rows["revenue/revenue.csv:3"].status, "conflict", "lines of one invoice must share a date")
        self.assertEqual(rows["revenue/revenue.csv:4"].amount_aed, "10.00")
        self.assertEqual(report.summary["accepted_revenue_aed"], "10.00")
        edit_json(config, lambda c: c["sources"]["revenue"][0]["amounts"].update(value_from={"multiply": ["Quantity", "Missing"]}))
        with self.assertRaisesRegex(InputContractError, "missing the declared column 'Missing'"):
            EvidenceEngine().run_engagement(load_engagement(config))

    def test_f23_a_currency_column_uses_the_declared_rate_for_each_code(self):
        config = self.build(
            "currencies",
            [["Invoice", "Lead", "Amount", "Currency", "Date"],
             ["T1", "L1", "100", "USD", "2026-07-05"],
             ["T2", "L1", "100", "AED", "2026-07-06"],
             ["T3", "L1", "100", "INR", "2026-07-07"]],
            {"transaction_id": "Invoice", "lead_id": "Lead", "value_aed": "Amount", "date": "Date"},
            {"amounts": {"currency": {"column": "Currency", "rates": {"USD": "3.6725", "AED": "1"},
                                      "rate_source": "UAE dirham peg to the US dollar"}}},
        )
        report = self.publish(config)
        rows = {ref(row): row for row in report.dispositions}
        self.assertEqual(rows["revenue/revenue.csv:2"].amount_aed, "367.25")
        self.assertEqual(rows["revenue/revenue.csv:3"].amount_aed, "100.00")
        self.assertEqual(rows["revenue/revenue.csv:4"].status, "rejected")
        self.assertIn("INR currency, which the config declares no rate for", rows["revenue/revenue.csv:4"].reason)
        self.assertIn("currency from 'Currency'", (self.root / "out" / "executive_brief.md").read_text(encoding="utf-8"))

    def test_f24_a_declared_time_shift_moves_the_date_it_belongs_to(self):
        config = self.build(
            "timezone",
            [["Invoice", "Lead", "Amount", "Date"],
             ["T1", "L1", "100", "2026-07-05 23:30"],
             ["T2", "L1", "200", "2026-07-06 10:00"]],
            {"transaction_id": "Invoice", "lead_id": "Lead", "value_aed": "Amount", "date": "Date"},
            {"date_format": "YYYY-MM-DD HH:MM", "time_zone_shift_hours": 4},
        )
        report = self.publish(config)
        claims = {claim.evidence_id: claim.value for claim in report.claims}
        self.assertEqual(claims["EV-REV-001"], "300.00")
        self.assertEqual(claims["EV-WINATTR-030"], "300.00")
        brief = (self.root / "out" / "executive_brief.md").read_text(encoding="utf-8")
        self.assertIn("times shifted +4 hours", brief)
        edit_json(config, lambda c: c["sources"]["revenue"][0].update(date_format="YYYY-MM-DD"))
        with self.assertRaisesRegex(InputContractError, "needs every declared date_format to include a time"):
            load_engagement(config)


class ProposeTests(FormatTestCase):
    """A person should check a draft config, not write one from scratch."""

    def test_f25_the_draft_reads_the_exports_and_runs(self):
        from revenue_evidence.propose import write_proposal

        folder = self.root / "inputs"
        folder.mkdir()
        for path in EXAMPLE.glob("*.csv"):
            shutil.copy(path, folder / path.name)
        draft, notes = write_proposal(folder, "Proposed example", "synthetic")
        report_text = notes.read_text(encoding="utf-8")
        proposal = json.loads(draft.read_text(encoding="utf-8"))

        files = {entry["file"]: (source, entry) for source, entries in proposal["sources"].items() for entry in entries}
        self.assertEqual(files["meta_ads.csv"][0], "ads")
        self.assertEqual(files["hubspot_contacts.csv"][0], "crm")
        self.assertEqual(files["xero_invoices.csv"][0], "revenue")
        self.assertEqual(files["meta_ads.csv"][1]["date_format"], "DD/MM/YYYY")
        self.assertEqual(files["meta_ads.csv"][1]["columns"]["spend_aed"], "Amount spent (AED)")
        self.assertEqual(files["hubspot_contacts.csv"][1]["columns"]["lead_id"], "Record ID")
        self.assertIn("reads 100% of sampled values", report_text)

        (folder / "engagement.json").write_text(json.dumps(proposal), encoding="utf-8")
        engagement = load_engagement(folder / "engagement.json")
        report = EvidenceEngine().run_engagement(engagement)
        evaluation = write_outputs(report, self.root / "out")
        self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])
        self.assertEqual(report.summary["accepted_spend_aed"], "13550.50")

    def test_f26_the_draft_says_where_a_person_must_decide(self):
        from revenue_evidence.propose import write_proposal

        folder = self.root / "inputs"
        folder.mkdir()
        write_csv(folder / "orders.csv", ["Invoice", "Reference", "Quantity", "Unit price", "Currency", "Date"],
                  [["T1", "L1", "2", "10.00", "USD", "2026-07-05"]])
        write_csv(folder / "contacts.csv", ["Record ID", "Create Date", "Lifecycle Stage", "UTM Campaign"],
                  [["L1", "2026-07-01", "customer", "C-1"]])
        write_csv(folder / "ads.csv", ["Day", "Campaign ID", "Campaign type", "Cost"],
                  [["2026-07-01", "C-1", "Search", "100"]])
        draft, notes = write_proposal(folder, "Needs decisions", "client")
        proposal = json.loads(draft.read_text(encoding="utf-8"))
        revenue = proposal["sources"]["revenue"][0]
        self.assertEqual(revenue["amounts"]["value_from"], {"multiply": ["Quantity", "Unit price"]})
        self.assertEqual(revenue["amounts"]["currency"]["column"], "Currency")
        self.assertIn("needs you", notes.read_text(encoding="utf-8"))
        (folder / "engagement.json").write_text(json.dumps(proposal), encoding="utf-8")
        with self.assertRaises(InputContractError):
            load_engagement(folder / "engagement.json")


class ReportSizeTests(FormatTestCase):
    """report.json repeats the CSVs; on a large export that repetition makes it unopenable."""

    def test_f16_detail_moves_into_the_csv_files_above_the_published_limits(self):
        report = EvidenceEngine().run_engagement(load_engagement(self.example()))
        self.assertEqual(write_outputs(report, self.root / "small")["status"], "PASS")
        small = json.loads((self.root / "small" / "report.json").read_text(encoding="utf-8"))
        self.assertIn("dispositions", small)
        self.assertIn("lineage", small["claims"][0])
        self.assertNotIn("detail_in_files", small)

        limits = [
            mock.patch.object(module, name, value)
            for module in (engine, reconstruct)
            for name, value in (("REPORT_ROW_LIMIT", 2), ("REPORT_LINEAGE_LIMIT", 5))
        ]
        with contextlib.ExitStack() as stack:
            for limit in limits:
                stack.enter_context(limit)
            evaluation = write_outputs(report, self.root / "large")
        self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])
        large = json.loads((self.root / "large" / "report.json").read_text(encoding="utf-8"))
        self.assertNotIn("dispositions", large)
        self.assertNotIn("rejected_records", large)
        self.assertEqual(large["detail_in_files"]["dispositions"], len(report.dispositions))
        self.assertEqual(large["claims"][0]["lineage_entries"], len(report.claims[0].lineage))
        self.assertEqual(
            len((self.root / "large" / "lineage.csv").read_text(encoding="utf-8").splitlines()),
            len((self.root / "small" / "lineage.csv").read_text(encoding="utf-8").splitlines()),
            "the evidence itself is unchanged; only its repetition in report.json goes",
        )

    def test_f17_a_report_that_drops_detail_it_could_publish_is_refused(self):
        report = EvidenceEngine().run_engagement(load_engagement(self.example()))
        with mock.patch.object(engine, "REPORT_ROW_LIMIT", 2), mock.patch.object(engine, "REPORT_LINEAGE_LIMIT", 5):
            evaluation = write_outputs(report, self.root / "out")
        self.assertEqual(evaluation["status"], "FAIL")
        self.assertTrue(any("omits row dispositions" in failure for failure in evaluation["failures"]), evaluation["failures"])


if __name__ == "__main__":
    unittest.main()

"""Adversarial reproduction suite for P1 (claim integrity).

Every test here asserts the behaviour the workflow is supposed to have. Each one
was observed to fail against the engine as published on 2 September 2026, so this
file is committed red first: the git history then shows the defect existed before
any repair. A later phase turns each test green by fixing the engine, never by
weakening the assertion.

Narrative attacks are paired with a control sentence that must pass, so a
rejection can only be attributed to the attacked number or wording — not to some
unrelated rule rejecting everything.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence import engine as engine_module  # noqa: E402
from revenue_evidence.engine import EvidenceEngine, write_outputs  # noqa: E402
from revenue_evidence.narrative import validate_narrative  # noqa: E402

DATA = ROOT / "data" / "synthetic"
ADS, CRM, REVENUE = DATA / "ads.csv", DATA / "crm.csv", DATA / "revenue.csv"
MONEY_FIELDS = {"ads": "spend_aed", "revenue": "value_aed"}
OUTPUT_FILES = (
    "report.json",
    "lineage.csv",
    "rejected_records.csv",
    "executive_brief.md",
    "report.html",
    "evaluation_results.json",
)


def narrative(body: str) -> str:
    """Model text only.

    The approval boundary is appended by the application (finalize_narrative)
    and may not appear in model text, so nothing is added here.
    """
    return body


def source_amounts(ads: Path = ADS, revenue: Path = REVENUE) -> dict[str, Decimal]:
    """Map a lineage reference such as "revenue:10" to the raw amount in that row.

    Deliberately independent of the engine: it reads the CSV text itself.
    """
    amounts: dict[str, Decimal] = {}
    for source, path in (("ads", ads), ("revenue", revenue)):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                try:
                    amounts[f"{source}:{row_number}"] = Decimal(row[MONEY_FIELDS[source]])
                except Exception:  # an unparseable amount cannot support any total
                    continue
    return amounts


def replace_lines(source: Path, target: Path, transform) -> Path:
    lines = source.read_text(encoding="utf-8").splitlines()
    target.write_text("\n".join(transform(lines)) + "\n", encoding="utf-8")
    return target


class NarrativeAttackTests(unittest.TestCase):
    """The narrative gate must bind each quoted number to the evidence it cites."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = EvidenceEngine().run(ADS, CRM, REVENUE)

    def assert_attack_rejected(self, control: str, attack: str) -> None:
        valid, reason = validate_narrative(narrative(control), self.report)
        self.assertTrue(valid, f"control sentence must pass, got: {reason}")
        valid, reason = validate_narrative(narrative(attack), self.report)
        self.assertFalse(valid, f"attack was accepted ({reason}): {attack!r}")

    def test_a01_false_value_with_correct_citation_is_rejected(self):
        self.assert_attack_rejected(
            "Accepted revenue is AED 56,500 [EV-REV-001].",
            "Accepted revenue is AED 65,500 [EV-REV-001].",
        )

    def test_a02_true_value_cited_to_the_wrong_evidence_is_rejected(self):
        # AED 15,000 is accepted spend (EV-SPEND-001), not revenue.
        self.assert_attack_rejected(
            "Accepted spend is AED 15,000 [EV-SPEND-001].",
            "Accepted revenue is AED 15,000 [EV-REV-001].",
        )

    def test_a03_uncited_invented_number_is_rejected(self):
        self.assert_attack_rejected(
            "Accepted spend is AED 15,000 [EV-SPEND-001].",
            "Accepted spend is AED 15,000 [EV-SPEND-001]. Paid Search returns 9.9x ROAS.",
        )

    def test_a04_paraphrased_causal_claim_is_rejected(self):
        self.assert_attack_rejected(
            "Paid Search attributed revenue is recorded [EV-ATTR-001].",
            "Paid Search drove the revenue [EV-ATTR-001].",
        )

    def test_a05_negated_approval_boundary_is_rejected(self):
        self.assert_attack_rejected(
            "Accepted spend is AED 15,000 [EV-SPEND-001].",
            "Accepted spend is AED 15,000 [EV-SPEND-001]; no human approval is needed.",
        )


class LineageAttackTests(unittest.TestCase):
    """Every authoritative figure must be reconstructable from its own source rows."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = EvidenceEngine().run(ADS, CRM, REVENUE)
        cls.amounts = source_amounts()

    def test_a06_every_aed_claim_equals_the_sum_of_its_lineage_rows(self):
        for claim in self.report.claims:
            if claim.unit != "AED":
                continue
            with self.subTest(evidence_id=claim.evidence_id):
                money_refs = [ref for ref in claim.source_refs if ref.split(":")[0] in MONEY_FIELDS]
                reconstructed = sum((self.amounts[ref] for ref in money_refs), Decimal("0"))
                self.assertEqual(
                    reconstructed,
                    Decimal(claim.value),
                    f"{claim.evidence_id} claims {claim.value} but its lineage rows "
                    f"{money_refs} sum to {reconstructed}",
                )

    def test_a07_no_rejected_row_supports_any_claim(self):
        rejected = {
            f"{row.source}:{row.source_row}"
            for row in self.report.rejected_records
            if row.severity == "rejected"
        }
        for claim in self.report.claims:
            with self.subTest(evidence_id=claim.evidence_id):
                self.assertFalse(
                    rejected & set(claim.source_refs),
                    f"{claim.evidence_id} is supported by rows listed as rejected: "
                    f"{sorted(rejected & set(claim.source_refs))}",
                )


class IdentityAttackTests(unittest.TestCase):
    """Conflicts and duplicates must not be resolved by file order."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def fingerprint(report) -> dict:
        return {
            "claims": {claim.evidence_id: claim.value for claim in report.claims},
            "channels": report.channels,
            "sensitivity": report.sensitivity,
            "recommendation": report.recommendation["action"],
        }

    def test_a08_swapping_conflicting_crm_rows_does_not_change_results(self):
        def swap(lines: list[str]) -> list[str]:
            first = next(i for i, line in enumerate(lines) if line.startswith("LEAD-002,CMP-SEARCH-01"))
            second = next(i for i, line in enumerate(lines) if line.startswith("LEAD-002,CMP-SOCIAL-01"))
            lines[first], lines[second] = lines[second], lines[first]
            return lines

        swapped = replace_lines(CRM, self.root / "crm.csv", swap)
        original = EvidenceEngine().run(ADS, CRM, REVENUE)
        reordered = EvidenceEngine().run(ADS, swapped, REVENUE)
        self.assertEqual(self.fingerprint(original), self.fingerprint(reordered))

    def test_a09_same_transaction_id_with_different_values_is_a_conflict(self):
        # Replace the exact TX-003 duplicate with a contradictory one.
        exact_duplicate = "TX-003,LEAD-003,12500.00,2026-07-29"

        def contradict(lines: list[str]) -> list[str]:
            last = max(i for i, line in enumerate(lines) if line == exact_duplicate)
            lines[last] = "TX-003,LEAD-003,99999.00,2026-07-29"
            return lines

        revenue = replace_lines(REVENUE, self.root / "revenue.csv", contradict)
        report = EvidenceEngine().run(ADS, CRM, revenue)
        reasons = [row.reason for row in report.rejected_records if row.record_id == "TX-003"]
        self.assertTrue(
            any("conflict" in reason for reason in reasons),
            f"contradictory TX-003 rows were not flagged as a conflict: {reasons}",
        )
        # Neither contradictory amount may count: 56,500 without TX-003's 12,500.
        self.assertEqual(report.summary["accepted_revenue_aed"], "44000.00")

    def test_a10_scientific_notation_amount_is_rejected(self):
        ads = self.root / "ads.csv"
        ads.write_text(
            "date,campaign_id,channel,spend_aed\n2026-07-01,C1,Search,1e3\n", encoding="utf-8"
        )
        report = EvidenceEngine().run(ads, CRM, REVENUE)
        self.assertEqual(
            report.summary["accepted_spend_aed"],
            "0.00",
            "spend written as 1e3 was silently reinterpreted as a plain amount",
        )


class OutputIntegrityAttackTests(unittest.TestCase):
    """Outputs must be repeatable and the evaluation must be able to fail."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a11_identical_inputs_produce_byte_identical_outputs(self):
        class FixedClock:
            instant = datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.instant

        outputs = []
        for label, instant in (
            ("first", datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc)),
            ("second", datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
        ):
            FixedClock.instant = instant
            with mock.patch.object(engine_module, "datetime", FixedClock):
                report = EvidenceEngine().run(ADS, CRM, REVENUE)
            target = self.root / label
            write_outputs(report, target)
            outputs.append(target)

        for name in OUTPUT_FILES:
            with self.subTest(output=name):
                self.assertEqual(
                    (outputs[0] / name).read_bytes(),
                    (outputs[1] / name).read_bytes(),
                    f"{name} changes when only the wall clock changes",
                )

    def test_a12_unusable_input_cannot_produce_a_passing_evaluation(self):
        ads = self.root / "ads.csv"
        crm = self.root / "crm.csv"
        revenue = self.root / "revenue.csv"
        ads.write_text("date,campaign_id,channel,spend_aed\nx,,,-1\n", encoding="utf-8")
        crm.write_text("lead_id,campaign_id,created_at,status\n,,,\n", encoding="utf-8")
        revenue.write_text("transaction_id,lead_id,value_aed,date\n,,,\n", encoding="utf-8")

        target = self.root / "out"
        try:
            write_outputs(EvidenceEngine().run(ads, crm, revenue), target)
        except Exception:
            # Refusing to produce outputs is an acceptable way to fail closed.
            return
        evaluation = target / "evaluation_results.json"
        if evaluation.exists():
            status = json.loads(evaluation.read_text(encoding="utf-8"))["status"]
            self.assertNotEqual(status, "PASS", "every row was unusable yet the evaluation passed")


if __name__ == "__main__":
    unittest.main()

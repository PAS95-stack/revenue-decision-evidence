"""Phase 2: the narrative gate binds every quoted number to the evidence it cites.

The gate is tested directly with fixed text. No language model is called: what is
under test is whether unsupported text can pass, not whether a model writes well.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence.engine import REJECTED, EvidenceEngine  # noqa: E402
from revenue_evidence.narrative import (  # noqa: E402
    BOUNDARY_STATEMENT,
    METRIC_VOCABULARY,
    generate_azure_narrative,
    validate_narrative,
)

DATA = ROOT / "data" / "synthetic"
ADS, CRM, REVENUE = DATA / "ads.csv", DATA / "crm.csv", DATA / "revenue.csv"
UNIT_SUFFIX = {"AED": "", "percent": "%", "ratio": "×"}
STEP = {"AED": Decimal("0.01"), "percent": Decimal("0.1"), "ratio": Decimal("0.01")}


# How an accurate narrative describes each metric. The gate requires a figure to be
# described as its own metric, and a channel or window figure to name its channel or window.
DESCRIPTIONS = {
    "accepted_ad_spend": "Accepted spend",
    "channel_accepted_ad_spend": "{channel} spend",
    "accepted_revenue": "Accepted revenue",
    "attributed_revenue": "Attributed revenue",
    "channel_attributed_revenue": "{channel} attributed revenue",
    "window_attributed_revenue": "Revenue attributed within {window_days} days",
    "revenue_attribution_coverage": "Attribution coverage",
    "unattributed_or_invalid_revenue": "Unattributed revenue",
    "revenue_with_conflicting_lead_attribution": "Revenue with conflicting lead attribution",
    "channel_assumption_dependent_roas": "{channel} assumption-dependent ROAS",
    "window_excluded_delayed_revenue": "Attributed revenue arriving after {window_days} days",
}


def sentence_for(claim, value: str) -> str:
    prefix = "AED " if claim.unit == "AED" else ""
    subject = DESCRIPTIONS[claim.metric].format(**claim.dimensions)
    return f"{subject} is {prefix}{value}{UNIT_SUFFIX[claim.unit]} [{claim.evidence_id}]."


class NarrativeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = EvidenceEngine().run(ADS, CRM, REVENUE)

    def accepts(self, text: str) -> None:
        valid, reason = validate_narrative(text, self.report)
        self.assertTrue(valid, f"expected acceptance ({reason}): {text!r}")

    def rejects(self, text: str, fragment: str | None = None) -> None:
        valid, reason = validate_narrative(text, self.report)
        self.assertFalse(valid, f"expected rejection: {text!r}")
        if fragment:
            self.assertIn(fragment, reason)

    def test_g01_every_claim_value_passes_and_the_next_value_fails(self):
        for claim in self.report.claims:
            with self.subTest(evidence_id=claim.evidence_id):
                self.accepts(sentence_for(claim, claim.value))
                nudged = str(Decimal(claim.value) + STEP[claim.unit])
                self.rejects(sentence_for(claim, nudged), "does not match")

    def test_g02_equivalent_spellings_of_the_same_value_pass(self):
        for text in (
            "Accepted revenue is AED 56,500 [EV-REV-001].",
            "Accepted revenue is 56500 [EV-REV-001].",
            "Accepted revenue is AED 56,500.00 [EV-REV-001].",
            "Accepted revenue is 56,500.00 AED [EV-REV-001].",
            "Coverage is 56.6% [EV-COVER-001].",
            "Coverage is 56.6 percent [EV-COVER-001].",
        ):
            with self.subTest(text=text):
                self.accepts(text)

    def test_g03_off_by_one_fils_is_rejected(self):
        self.rejects("Accepted revenue is AED 56,500.01 [EV-REV-001].", "does not match")

    def test_g04_rounded_percentage_is_rejected(self):
        self.rejects("Coverage is 57% [EV-COVER-001].", "does not match")

    def test_g05_explicit_unit_must_match_the_claim(self):
        self.rejects("Coverage is AED 56.6 [EV-COVER-001].", "does not match")
        self.rejects("Accepted revenue is 56500.00% [EV-REV-001].", "does not match")

    def test_g06_abbreviated_amounts_are_rejected(self):
        self.rejects("Accepted revenue is AED 56.5k [EV-REV-001].", "abbreviated")
        self.rejects("Accepted revenue is 0.0565 million [EV-REV-001].", "abbreviated")

    def test_g07_dates_are_rejected(self):
        self.rejects("On 2026-07-20 revenue was AED 56,500 [EV-REV-001].", "date")

    def test_g08_malformed_citations_are_rejected(self):
        for text in (
            "Accepted revenue is AED 56,500 [EV-REV-1].",
            "Accepted revenue is AED 56,500 (EV-REV-001).",
            "Accepted revenue is AED 56,500 [ev-rev-001].",
        ):
            with self.subTest(text=text):
                self.rejects(text)

    def test_g09_citation_must_be_in_the_same_sentence(self):
        self.rejects("Accepted revenue is AED 56,500. [EV-REV-001]", "without citing")

    def test_g10_signed_numbers_are_rejected(self):
        self.rejects("Accepted spend is AED -15,000 [EV-SPEND-001].", "does not match")

    def test_g11_channel_figures_bind_to_their_own_channel(self):
        search = next(row for row in self.report.channels if row["channel"] == "Paid Search")
        social = next(row for row in self.report.channels if row["channel"] == "Paid Social")
        self.accepts(
            f"Paid Search has assumption-dependent ROAS of {search['assumption_dependent_roas']}× "
            f"[{search['evidence_ids']['roas']}]."
        )
        self.rejects(
            f"Paid Search has assumption-dependent ROAS of {search['assumption_dependent_roas']}× "
            f"[{social['evidence_ids']['roas']}].",
            "does not match",
        )

    def test_g12_window_length_is_allowed_only_beside_a_window_claim(self):
        self.accepts("Within 30 days, AED 28,500 was attributed [EV-WINATTR-030].")
        self.rejects("Within 30 days, AED 56,500 was accepted [EV-REV-001].", "does not match")

    def test_g13_forecast_recommendation_and_roi_language_is_rejected(self):
        for text in (
            "Accepted revenue will grow beyond AED 56,500 [EV-REV-001].",
            "Reallocate AED 7,500 [EV-CHSPEND-002].",
            "ROI is 2.13 [EV-CHROAS-002].",
            "Paid Search produced incremental revenue of AED 16,000 [EV-CHATTR-002].",
            "The figures prove AED 16,000 came from search [EV-CHATTR-002].",
        ):
            with self.subTest(text=text):
                self.rejects(text, "prohibited")

    def test_g14_approval_or_execution_language_is_rejected(self):
        for text in (
            "Accepted revenue is AED 56,500 [EV-REV-001]. Human approval is required.",
            "The owner may authorise a change to AED 7,500 [EV-CHSPEND-002].",
            "Execute the plan for AED 7,500 [EV-CHSPEND-002].",
        ):
            with self.subTest(text=text):
                self.rejects(text, "approval or execution")

    def test_g15_narrative_without_any_citation_is_rejected(self):
        self.rejects("Revenue looks healthy.", "no evidence citations")
        self.rejects("   ", "empty")

    def test_g16_spelled_out_or_relative_quantities_are_rejected(self):
        # Found by probing the first phase 2 gate: these passed numeric matching entirely.
        for text in (
            "Accepted revenue is sixty-five thousand five hundred dirhams [EV-REV-001].",
            "Unattributed revenue is zero [EV-UNMATCH-001].",
            "Paid Search attributed revenue is twice that of Paid Social [EV-CHATTR-002].",
        ):
            with self.subTest(text=text):
                self.rejects(text, "quantities")

    def test_g17_ranking_or_judging_channels_is_rejected(self):
        for text in (
            "Paid Search is clearly the best channel at 2.13× [EV-CHROAS-002].",
            "Paid Search outperforms Paid Social [EV-CHROAS-002].",
            "Paid Social has the lowest ROAS at 1.92× [EV-CHROAS-003].",
        ):
            with self.subTest(text=text):
                self.rejects(text, "rank")


    def test_g18_a_correct_number_described_as_another_metric_is_rejected(self):
        # Found while writing the walkthrough: the spend figure described as revenue passed.
        for text in (
            "Accepted revenue is AED 15,000 [EV-SPEND-001].",
            "Accepted revenue is AED 32,000 [EV-ATTR-001].",
            "Attributed revenue is AED 56,500 [EV-REV-001].",
            "Attributed revenue is AED 24,500 [EV-UNMATCH-001].",
            "Attributed revenue is AED 9,000 [EV-CONFLICT-001].",
            "Revenue share is 56.6% [EV-COVER-001].",
            "Paid Search spend is 2.13× [EV-CHROAS-002].",
            "Takings are AED 15,000 [EV-SPEND-001].",
        ):
            with self.subTest(text=text):
                self.rejects(text, "described as")

    def test_g19_channel_figures_must_name_their_own_channel(self):
        self.accepts("Paid Search spend is AED 7,500 [EV-CHSPEND-002].")
        self.rejects("Paid Social spend is AED 7,500 [EV-CHSPEND-002].", "described as Paid Social")
        self.rejects("Spend is AED 7,500 [EV-CHSPEND-002].", "does not name Paid Search")

    def test_g20_window_figures_must_state_their_window(self):
        self.rejects("Attributed revenue is AED 28,500 [EV-WINATTR-030].", "window")
        self.rejects("Within 60 days, AED 28,500 was attributed [EV-WINATTR-030].", "does not match")

    def test_g21_a_figure_cannot_borrow_a_neighbouring_citation(self):
        self.rejects(
            "Paid Search spend is AED 16,000 [EV-CHATTR-002] and attributed revenue is AED 7,500 [EV-CHSPEND-002].",
            "described as spend",
        )
        self.rejects("Accepted revenue is AED 15,000 [EV-REV-001] [EV-SPEND-001].", "described as")
        self.rejects(
            "Accepted revenue is AED 15,000 and spend is AED 56,500 [EV-REV-001] [EV-SPEND-001].",
            "cite each figure",
        )
        self.rejects(
            "Accepted revenue is AED 56,500 [EV-REV-001], and Paid Search spend is AED 15,000 [EV-SPEND-001].",
            "described as Paid Search",
        )

    def test_g22_accurate_descriptions_in_natural_sentences_pass(self):
        for text in (
            "Accepted revenue is AED 56,500 [EV-REV-001] and AED 32,000 of it is attributed [EV-ATTR-001].",
            "AED 24,500 of accepted revenue could not be assigned to a channel [EV-UNMATCH-001].",
            "Paid Search has spend of AED 7,500 [EV-CHSPEND-002], attributed revenue of AED 16,000 "
            "[EV-CHATTR-002] and assumption-dependent return on ad spend of 2.13× [EV-CHROAS-002].",
            "Accepted revenue [EV-REV-001] is AED 56,500.",
            "Attributed revenue is AED 32,000 [EV-ATTR-001] [EV-WINATTR-060].",
            "Within 30 days, AED 28,500 was attributed [EV-WINATTR-030] and AED 3,500 arrived later [EV-WINEXCL-030].",
        ):
            with self.subTest(text=text):
                self.accepts(text)

    def test_g23_every_engine_metric_has_a_description_vocabulary(self):
        self.assertEqual({claim.metric for claim in self.report.claims}, set(METRIC_VOCABULARY))
        self.assertEqual(set(DESCRIPTIONS), set(METRIC_VOCABULARY))
        for allowed, required in METRIC_VOCABULARY.values():
            self.assertTrue(required <= allowed)


class LabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_with_channel(self, channel: str):
        ads = self.root / "ads.csv"
        crm = self.root / "crm.csv"
        revenue = self.root / "revenue.csv"
        ads.write_text(f"date,campaign_id,channel,spend_aed\n2026-07-01,C1,{channel},100\n", encoding="utf-8")
        crm.write_text("lead_id,campaign_id,created_at,status\nL1,C1,2026-07-02,won\n", encoding="utf-8")
        revenue.write_text("transaction_id,lead_id,value_aed,date\nT1,L1,300,2026-07-10\n", encoding="utf-8")
        return EvidenceEngine().run(ads, crm, revenue)

    def test_l01_digits_in_a_cited_channel_name_are_not_figures(self):
        report = self.run_with_channel("Channel 2")
        valid, reason = validate_narrative("Channel 2 spend is AED 100 [EV-CHSPEND-001].", report)
        self.assertTrue(valid, reason)
        # Without the channel claim cited, the 2 is an unsupported number.
        valid, _ = validate_narrative("Channel 2 spend is AED 100 [EV-SPEND-001].", report)
        self.assertFalse(valid)

    def test_l02_bracketed_labels_are_rejected_at_ingest(self):
        report = self.run_with_channel("Search [EV-REV-001]")
        ads_rows = [row for row in report.dispositions if row.source == "ads"]
        self.assertEqual([row.status for row in ads_rows], [REJECTED])
        self.assertIn("square brackets", ads_rows[0].reason)


class AzureWiringTests(unittest.TestCase):
    """The optional model call is wired through the gate; the model itself is mocked."""

    ENV = {
        "AZURE_OPENAI_ENDPOINT": "https://example.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "test",
        "AZURE_OPENAI_API_KEY": "not-a-real-key",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = EvidenceEngine().run(ADS, CRM, REVENUE)

    def call_with_model_text(self, text: str):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": text}}]}
        ).encode("utf-8")
        opened = mock.MagicMock()
        opened.__enter__.return_value = response
        with mock.patch.dict(os.environ, self.ENV), mock.patch(
            "urllib.request.urlopen", return_value=opened
        ) as urlopen:
            result = generate_azure_narrative(self.report)
        return result, urlopen

    def test_w01_false_model_output_disables_the_narrative(self):
        result, _ = self.call_with_model_text("Accepted revenue is AED 65,500 [EV-REV-001].")
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["content"], "")
        self.assertIn("does not match", result["reason"])

    def test_w02_valid_model_output_carries_the_application_boundary(self):
        text = "Accepted revenue is AED 56,500 [EV-REV-001]."
        result, _ = self.call_with_model_text(text)
        self.assertEqual(result["status"], "validated")
        self.assertTrue(result["content"].startswith(text))
        self.assertTrue(result["content"].endswith(BOUNDARY_STATEMENT))

    def test_w03_request_contains_values_but_no_row_references(self):
        _, urlopen = self.call_with_model_text("Accepted revenue is AED 56,500 [EV-REV-001].")
        body = urlopen.call_args[0][0].data.decode("utf-8")
        self.assertIn("EV-REV-001", body)
        for forbidden in ("source_refs", "lineage", "revenue:", "crm:", "ads:"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()

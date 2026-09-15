"""Phase 5: two hundred seeded random datasets with injected faults.

Every dataset must keep five invariants: each record gets one status, identical
runs give identical reports, shuffling rows changes no figure, the engine and the
independent checker agree (publication fails only for missing evidence, never
for a disagreement), and unusable evidence is never published. A tally proves
the generator really produced each kind of fault, so the test cannot pass by
generating only clean data.
"""

from __future__ import annotations

import csv
import io
import random
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence.engine import EvidenceEngine, write_outputs  # noqa: E402

SEED = 20260915
DATASETS = 200
SOURCES = ("ads", "crm", "revenue")
HEADERS = {
    "ads": ["date", "campaign_id", "channel", "spend_aed"],
    "crm": ["lead_id", "campaign_id", "created_at", "status"],
    "revenue": ["transaction_id", "lead_id", "value_aed", "date"],
}
CHANNELS = ["Paid Search", "Paid Social", "Email", "Display 2", "Affiliate"]
BAD_AMOUNTS = ["-5", "NaN", "1e3", "1,000", "+5", "5.", ".5", "1_000", "", "abc", "1" * 40]
BAD_DATES = ["2026/07/01", "20260701", "2026-7-1", "2026-02-30", "", "tomorrow"]


def amount_text(rng: random.Random) -> str:
    if rng.random() < 0.08:
        return rng.choice(BAD_AMOUNTS)
    whole = rng.randint(0, 20000)
    style = rng.random()
    if style < 0.4:
        return str(whole)
    if style < 0.8:
        return f"{whole}.{rng.randint(0, 99):02d}"
    if style < 0.9:
        return f" {whole}.{rng.randint(0, 9)} "
    return f"{whole}.{rng.randint(0, 999):03d}"


def date_text(rng: random.Random, spread: int) -> str:
    if rng.random() < 0.05:
        return rng.choice(BAD_DATES)
    return (date(2026, 7, 1) + timedelta(days=rng.randint(0, spread))).isoformat()


def generate(rng: random.Random) -> dict[str, list[list[str]]]:
    campaigns = [f"C{index}" for index in range(rng.randint(1, 6))]
    channel_of = {campaign: rng.choice(CHANNELS) for campaign in campaigns}

    ads = []
    for _ in range(rng.randint(0, 12)):
        campaign = rng.choice(campaigns + ["", "C[9]"]) if rng.random() < 0.08 else rng.choice(campaigns)
        channel = channel_of.get(campaign, rng.choice(CHANNELS))
        if rng.random() < 0.05:
            channel = rng.choice(CHANNELS)
        ads.append([date_text(rng, 80), campaign, channel, amount_text(rng)])
    if ads and rng.random() < 0.3:
        ads.append(list(rng.choice(ads)))

    leads = []
    for index in range(rng.randint(0, 10)):
        status = rng.choice(["won", "qualified", "WON", "new"]) if rng.random() > 0.1 else "won\nfollow-up call"
        if rng.random() < 0.03:
            status = ""
        leads.append(
            [f"L{index}" if rng.random() > 0.05 else "", rng.choice(campaigns + ["C-UNKNOWN"]), date_text(rng, 80), status]
        )
    if leads and rng.random() < 0.3:
        leads.append(list(rng.choice(leads)))
    if leads and rng.random() < 0.25:
        contradiction = list(rng.choice(leads))
        contradiction[1] = rng.choice(campaigns + ["C-OTHER"])
        leads.append(contradiction)

    lead_ids = [lead[0] for lead in leads if lead[0]] + ["L-ORPHAN"]
    revenue = []
    for index in range(rng.randint(0, 12)):
        revenue.append(
            [f"T{index}" if rng.random() > 0.04 else "", rng.choice(lead_ids), amount_text(rng), date_text(rng, 140)]
        )
    if revenue and rng.random() < 0.3:
        revenue.append(list(rng.choice(revenue)))
    if revenue and rng.random() < 0.25:
        contradiction = list(rng.choice(revenue))
        contradiction[2] = str(rng.randint(1, 9999))
        revenue.append(contradiction)
    return {"ads": ads, "crm": leads, "revenue": revenue}


def render(name: str, rows: list[list[str]], rng: random.Random, style: dict) -> str:
    def encode(fields: list[str]) -> str:
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator=style["newline"]).writerow(fields)
        return buffer.getvalue()

    parts = [encode(HEADERS[name])]
    for row in rows:
        if rng.random() < style["blank_rate"]:
            parts.append(style["newline"])
        parts.append(encode(row))
    return ("﻿" if style["bom"] else "") + "".join(parts)


def write_dataset(folder: Path, data: dict, rng: random.Random, style: dict) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in SOURCES:
        path = folder / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(render(name, data[name], rng, style))
        paths[name] = path
    return paths


def record_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for record in csv.reader(handle) if record) - 1


def comparable(report) -> dict:
    return {
        "claims": {claim.evidence_id: (claim.value, claim.unit) for claim in report.claims},
        "channels": report.channels,
        "sensitivity": report.sensitivity,
        "summary": report.summary,
        "recommendation": report.recommendation,
    }


class GeneratedDatasetTests(unittest.TestCase):
    def test_f01_generated_datasets_hold_every_invariant(self):
        tally: Counter = Counter()
        with tempfile.TemporaryDirectory() as temp:
            for index in range(DATASETS):
                rng = random.Random(SEED * 1000 + index)
                data = generate(rng)
                style = {
                    "newline": rng.choice(["\n", "\r\n"]),
                    "bom": rng.random() < 0.2,
                    "blank_rate": rng.choice([0.0, 0.0, 0.1]),
                }
                folder = Path(temp) / f"dataset-{index:03d}"
                paths = write_dataset(folder / "original", data, rng, style)
                with self.subTest(dataset=index):
                    engine = EvidenceEngine()
                    report = engine.run(paths["ads"], paths["crm"], paths["revenue"])
                    counts = report.summary["row_status_counts"]

                    for name in SOURCES:
                        records = record_count(paths[name])
                        self.assertEqual(counts[name]["input"], records, f"{name} input count")
                        self.assertEqual(sum(v for k, v in counts[name].items() if k != "input"), records)

                    again = engine.run(paths["ads"], paths["crm"], paths["revenue"])
                    self.assertEqual(report.to_dict(), again.to_dict(), "identical runs differ")

                    shuffled = {name: rng.sample(rows, len(rows)) for name, rows in data.items()}
                    shuffled_paths = write_dataset(folder / "shuffled", shuffled, rng, style)
                    reordered = engine.run(shuffled_paths["ads"], shuffled_paths["crm"], shuffled_paths["revenue"])
                    self.assertEqual(comparable(report), comparable(reordered), "row order changed a result")

                    evaluation = write_outputs(report, folder / "out")
                    disagreements = [f for f in evaluation["failures"] if not f.startswith("sufficient_evidence:")]
                    self.assertEqual(disagreements, [], "engine and independent checker disagree")
                    usable = all(counts[name]["accepted"] + counts[name]["accepted-unattributed"] for name in SOURCES)
                    self.assertEqual(evaluation["status"], "PASS" if usable else "FAIL")
                    if not usable:
                        self.assertFalse((folder / "out" / "executive_brief.md").exists())

                    for name in SOURCES:
                        for status, count in counts[name].items():
                            if status != "input":
                                tally[status] += count
                    tally["published" if usable else "refused"] += 1
                    tally["crlf"] += style["newline"] == "\r\n"
                    tally["bom"] += style["bom"]
                    tally["multi_line_field"] += any("\n" in lead[3] for lead in data["crm"])
                    tally["blank_lines"] += "\n\n" in paths["ads"].read_text(encoding="utf-8-sig").replace("\r\n", "\n")

        minimums = {
            "rejected": 40,
            "duplicate": 15,
            "conflict": 15,
            "accepted-unattributed": 40,
            "published": 100,
            "refused": 3,
            "crlf": 50,
            "bom": 20,
            "multi_line_field": 10,
            "blank_lines": 10,
        }
        for key, minimum in minimums.items():
            self.assertGreaterEqual(tally[key], minimum, f"generator produced too few '{key}' cases: {dict(tally)}")


if __name__ == "__main__":
    unittest.main()

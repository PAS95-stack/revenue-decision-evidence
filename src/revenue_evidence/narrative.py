from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict
from typing import Any

from .engine import EvidenceReport


CITATION_PATTERN = re.compile(r"\[(EV-[A-Z]+-\d{3})\]")


def evidence_payload(report: EvidenceReport) -> dict[str, Any]:
    """Return computed evidence only; no raw source rows are included."""
    return {
        "claims": [asdict(claim) for claim in report.claims],
        "recommendation": report.recommendation,
        "human_approval": report.human_approval,
        "sensitivity": report.sensitivity,
    }


def validate_narrative(text: str, report: EvidenceReport) -> tuple[bool, str]:
    allowed_ids = {claim.evidence_id for claim in report.claims}
    cited_ids = set(CITATION_PATTERN.findall(text))
    if not text.strip():
        return False, "narrative is empty"
    if not cited_ids:
        return False, "narrative contains no evidence citations"
    unknown = cited_ids - allowed_ids
    if unknown:
        return False, f"narrative cites unknown evidence IDs: {sorted(unknown)}"
    if re.search(r"\b(guarantee[sd]?|caused|proves causation|automatically execute)\b", text, re.I):
        return False, "narrative contains a prohibited causal, guarantee, or autonomy claim"
    if "human approval" not in text.lower():
        return False, "narrative omits the human-approval boundary"
    return True, "validated"


def generate_azure_narrative(report: EvidenceReport) -> dict[str, str]:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
    api_key = os.getenv("AZURE_OPENAI_API_KEY", "")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    if not all((endpoint, deployment, api_key)):
        return {
            "status": "disabled",
            "reason": "Azure configuration is incomplete",
            "content": "",
        }
    system = (
        "Explain only the supplied computed evidence. Cite every quantitative statement "
        "with an evidence ID in square brackets. Do not imply causation, guarantee ROI, "
        "repair data, invent facts, or authorize action. State that human approval is required."
    )
    body = json.dumps(
        {
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(evidence_payload(report), ensure_ascii=False),
                },
            ],
            "temperature": 0,
            "max_tokens": 650,
        }
    ).encode("utf-8")
    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        return {"status": "disabled", "reason": f"Azure request failed: {exc}", "content": ""}
    valid, reason = validate_narrative(text, report)
    return {
        "status": "validated" if valid else "disabled",
        "reason": reason,
        "content": text if valid else "",
    }

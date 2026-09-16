#!/usr/bin/env python3
"""Independent reconstruction of a revenue evidence report.

This file deliberately re-implements the data contract instead of importing the
engine. It reads the three raw CSV exports, derives every row's status and every
published figure from scratch, and compares the result with what the engine
wrote. Agreement between two separate implementations is the evidence; a check
that reused the engine's own code would repeat the engine's bugs.

Standard library only. It must never import revenue_evidence (a test enforces
this), so it can also be run on its own against published outputs:

    python3 src/revenue_evidence/reconstruct.py --ads ads.csv --crm crm.csv \
        --revenue revenue.csv --output outputs/run
    python3 src/revenue_evidence/reconstruct.py --config engagement.json --output outputs/run

Contract choices applied here independently; any disagreement fails verification:
  * amounts are plain non-negative decimals such as "1000" or "1000.50", rounded
    half-up to fils; exponents, signs and thousands separators are not amounts;
  * dates are YYYY-MM-DD;
  * rows sharing an identifier are one record only when identical (the earliest
    is kept, later copies are duplicates); otherwise every copy is a conflict;
  * percentages round half-even to 0.1 and ratios half-even to 0.01.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

CENT = Decimal("0.01")
PLAIN_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# 1.234,56 as Excel writes it in many locales, beside 1,234.56.
DECIMAL_MARKS = {"point": ",", "comma": "."}
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DOWNSTREAM_CHECKS = ("claim_values", "claim_lineage", "summary", "channels_and_sensitivity", "exceptions", "executive_brief")
CITATION = re.compile(r"\[(EV-[A-Z]+-\d{3})\]")
SOURCES = ("ads", "crm", "revenue")
REQUIRED = {
    "ads": ("date", "campaign_id", "channel", "spend_aed"),
    "crm": ("lead_id", "campaign_id", "created_at", "status"),
    "revenue": ("transaction_id", "lead_id", "value_aed", "date"),
}
STATUSES = ("accepted", "accepted-unattributed", "rejected", "duplicate", "conflict", "filtered")
CHECKS = (
    "sources_match_fingerprints",
    "engagement_config",
    "ruleset_fingerprint_and_run_id",
    "row_dispositions",
    "rejected_records",
    "exceptions",
    "claim_values",
    "claim_lineage",
    "summary",
    "channels_and_sensitivity",
    "executive_brief",
    "sufficient_evidence",
)
MAX_DETAILS = 5
# The same rule the engine applies: above these sizes report.json leaves the detail in
# the CSVs. Checked here so detail cannot go missing from a report small enough to hold it.
REPORT_ROW_LIMIT = 50_000
REPORT_LINEAGE_LIMIT = 200_000


class ReconstructionError(Exception):
    """Inputs or outputs cannot be read well enough to compare."""


def _text(row: dict, field: str) -> str:
    value = row.get(field)
    return value.strip() if isinstance(value, str) else ""


def _amount(text: str) -> Decimal | None:
    if not PLAIN_DECIMAL.fullmatch(text):
        return None
    try:
        return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _day(text: str) -> date | None:
    if not ISO_DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _grouped(value: str) -> str:
    """Thousands separators for a two-decimal money string, without the format mini-language."""
    sign = "-" if value.startswith("-") else ""
    whole, _, fraction = value.lstrip("-").partition(".")
    digits = whole.lstrip("0") or "0"
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return f"{sign}{','.join(groups)}.{fraction}"


def _shown(claim: dict) -> str:
    return _grouped(claim["value"]) if claim["unit"] == "AED" else claim["value"]


def _exception_cell(item: dict) -> str:
    if item["rows_without_amount"] == item["rows"]:
        return "—"
    extra = item["rows_without_amount"]
    return _grouped(item["amount_aed"]) + (f" (+{extra} without a readable amount)" if extra else "")


def _limited(items) -> str:
    items = sorted(str(item) for item in items)
    shown = ", ".join(items[:MAX_DETAILS])
    return shown + (f" and {len(items) - MAX_DETAILS} more" if len(items) > MAX_DETAILS else "")


# --- Engagement configs -------------------------------------------------------
# A config declares, per export file, which column holds each field and how dates
# and amounts are written. These readers interpret those declarations with their
# own code; the engine's interpretation must produce the same values.

DEFAULT_WINDOWS = [30, 60, 90]
DATE_FORMAT_NAMES = tuple(
    f"{day}{time}" for day in ("YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY") for time in ("", " HH:MM", " HH:MM:SS")
) + ("YYYY-MM-DDTHH:MM:SS",)
ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?", re.ASCII)
CUSTOMER_REQUIRED = {
    "ads": ("date", "campaign_id", "channel", "spend_aed"),
    "crm": ("lead_id", "campaign_id", "created_at", "status", "customer_id"),
    "revenue": ("transaction_id", "customer_id", "value_aed", "date"),
}
FIXABLE = {"ads": ("channel",), "crm": ("status",), "revenue": ()}
IDENTIFIERS = {
    "ads": ("campaign_id",),
    "crm": ("lead_id", "campaign_id", "customer_id"),
    "revenue": ("transaction_id", "lead_id", "customer_id"),
}
REFUND_MODES = ("reject", "negative_values", "whole_file")
DELIMITERS = {"comma": ",", "semicolon": ";", "tab": "\t"}
ENCODINGS = {"utf-8": "utf-8-sig", "utf-16": "utf-16", "windows-1252": "cp1252", "latin-1": "latin-1"}
OPTIONAL = {"ads": ("row_id",), "crm": (), "revenue": ("line_id",)}
AMOUNT = {"ads": ("spend_aed",), "crm": (), "revenue": ("value_aed",)}


def _included(row: dict, spec: dict) -> bool:
    if not spec["filter_column"]:
        return True
    return _text(row, spec["filter_column"]).casefold() in spec["filter_values"]


def _ref(source: str, label: str, line: int) -> str:
    return f"{source}/{label}:{line}" if label else f"{source}:{line}"


def _field(row: dict, spec: dict, name: str) -> str:
    value = spec["fixed"][name] if name in spec["fixed"] else _text(row, spec["columns"][name])
    return value.upper() if name in spec["uppercase"] else value


def _one_declared_date(text: str, fmt: str, shift: int) -> date | None:
    hour = minute = 0
    if fmt == "YYYY-MM-DDTHH:MM:SS":
        if not ISO_DATETIME.fullmatch(text):
            return None
        day_text, hour, minute = text[:10], int(text[11:13]), int(text[14:16])
    else:
        day_format, _, time_format = fmt.partition(" ")
        day_text, _, time_text = text.partition(" ")
        if bool(time_format) != bool(time_text):
            return None
        if time_format:
            pieces = time_text.split(":")
            if (
                len(pieces) != time_format.count(":") + 1
                or not all(piece.isascii() and piece.isdigit() for piece in pieces)
                or not 1 <= len(pieces[0]) <= 2
                or any(len(piece) != 2 for piece in pieces[1:])
            ):
                return None
            hour, minute = int(pieces[0]), int(pieces[1])
        if day_format != "YYYY-MM-DD":
            parts = day_text.split("/")
            if len(parts) != 3 or not all(part.isascii() and part.isdigit() for part in parts):
                return None
            first, second, year = parts
            if not (1 <= len(first) <= 2 and 1 <= len(second) <= 2 and len(year) == 4):
                return None
            day, month = (first, second) if day_format == "DD/MM/YYYY" else (second, first)
            day_text = f"{year}-{int(month):02d}-{int(day):02d}"
    calendar = _day(day_text)
    if calendar is None:
        return None
    if not shift:
        return calendar
    return (datetime(calendar.year, calendar.month, calendar.day, hour, minute) + timedelta(hours=shift)).date()


def _declared_date(text: str, formats, shift: int = 0) -> date | None:
    for fmt in ([formats] if isinstance(formats, str) else formats):
        found = _one_declared_date(text, fmt, shift)
        if found is not None:
            return found
    return None


def _one_decimal(text: str, spec: dict) -> Decimal | None:
    label = spec["currency_label"]
    if label and text.startswith(label):
        text = text[len(label):].lstrip()
    elif label and text.endswith(label):
        text = text[: -len(label)].rstrip()
    negative = False
    if len(text) >= 3 and text.startswith("(") and text.endswith(")"):
        text, negative = text[1:-1].strip(), True
    elif text[:1] == "-":
        text, negative = text[1:].lstrip(), True
    if negative and spec["refunds"] != "negative_values":
        return None
    if spec["decimal"] == "comma":
        whole, point, fraction = text.partition(",")
        groups = whole.split(".")
        if len(groups) > 1:
            if not spec["thousands_separator"] or not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:]):
                return None
        text = "".join(groups) + ("." if point else "") + fraction
    elif spec["thousands_separator"]:
        whole, point, fraction = text.partition(".")
        groups = whole.split(",")
        if len(groups) > 1 and (not 1 <= len(groups[0]) <= 3 or any(len(group) != 3 for group in groups[1:])):
            return None
        text = "".join(groups) + point + fraction
    if not PLAIN_DECIMAL.fullmatch(text) or len(text.partition(".")[0]) > 15:
        return None
    return -Decimal(text) if negative else Decimal(text)


def _declared_amount(row: dict, spec: dict, field: str) -> Decimal | None:
    if spec["value_from"]:
        operation, columns = spec["value_from"]["operation"], spec["value_from"]["columns"]
        parts = [_one_decimal(_text(row, column), spec) for column in columns]
        if any(part is None for part in parts):
            return None
        raw = parts[0] * parts[1] if operation == "multiply" else parts[0] + parts[1]
    else:
        raw = _one_decimal(_field(row, spec, field), spec)
        if raw is None:
            return None
    if raw < 0 and spec["refunds"] != "negative_values":
        return None
    if spec["refunds"] == "whole_file":
        raw = -raw
    if spec["currency_column"]:
        rate = spec["currency_rates"].get(_text(row, spec["currency_column"]).upper())
        if rate is None:
            return None
    else:
        rate = spec["aed_per_unit"]
    value = (raw * Decimal(rate)).quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal("0.00") if value == 0 else value


def _plain_spec(name: str, path) -> dict:
    """The three-file contract expressed as a spec: canonical columns, ISO dates, plain amounts."""
    return {
        "path": Path(path), "label": "", "columns": {field: field for field in REQUIRED[name]}, "fixed": {},
        "date_formats": ("YYYY-MM-DD",), "time_shift": 0, "value_from": None, "currency_column": "",
        "currency_rates": {}, "thousands_separator": "", "decimal": "point", "header_row": 1,
        "currency_label": "", "currency": "AED",
        "aed_per_unit": "1", "rate_source": "", "refunds": "reject", "uppercase": set(), "required": REQUIRED[name],
        "filter_column": "", "filter_values": (), "delimiter": "comma", "encoding": "utf-8",
    }


def _load_config(path) -> dict:
    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
        config = json.loads(raw.decode("utf-8"))
    except OSError as exc:
        raise ReconstructionError(f"cannot read the engagement config ({type(exc).__name__})") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReconstructionError("the engagement config is not valid JSON") from exc

    def need(condition, message: str) -> None:
        if not condition:
            raise ReconstructionError(f"engagement config: {message}")

    def amounts_of(entry: dict) -> dict:
        found = entry.get("amounts", {})
        return found if isinstance(found, dict) else {}

    need(isinstance(config, dict), "must be a JSON object")
    need(type(config.get("config_version")) is int and config.get("config_version") == 1, "config_version must be 1")
    join = config.get("revenue_join", "lead_id")
    need(join in ("lead_id", "customer_id"), "revenue_join must be lead_id or customer_id")
    required = REQUIRED if join == "lead_id" else CUSTOMER_REQUIRED
    rule = config.get("multiple_campaigns", "unattributed")
    need(rule in ("unattributed", "first_touch", "last_touch"), "multiple_campaigns is invalid")
    need(rule == "unattributed" or join == "customer_id", "multiple_campaigns needs revenue_join customer_id")
    threshold = config.get("coverage_threshold_percent", "80")
    need(isinstance(threshold, str) and PLAIN_DECIMAL.fullmatch(threshold) is not None, "coverage_threshold_percent is invalid")
    windows = config.get("attribution_windows_days", DEFAULT_WINDOWS)
    need(
        isinstance(windows, list) and 1 <= len(windows) <= 5
        and all(type(window) is int and 1 <= window <= 999 for window in windows)
        and windows == sorted(set(windows)),
        "attribution_windows_days is invalid",
    )
    aliases = config.get("channel_aliases", {})
    need(isinstance(aliases, dict) and all(isinstance(value, str) for value in aliases.values()), "channel_aliases is invalid")
    sources = config.get("sources")
    need(isinstance(sources, dict) and sorted(sources) == sorted(SOURCES), "sources must declare ads, crm and revenue")
    files: dict[str, list[dict]] = {}
    described = []
    for name in SOURCES:
        entries = sources[name]
        need(isinstance(entries, list) and entries, f"sources.{name} must list at least one file")
        files[name] = []
        for entry in entries:
            need(isinstance(entry, dict), f"sources.{name} entries must be objects")
            label = entry.get("file")
            need(
                isinstance(label, str) and label and not label.startswith("/")
                and all(part not in ("", ".", "..") for part in label.split("/"))
                and not any(mark in label for mark in ":\\|`"),
                f"sources.{name} names an invalid file",
            )
            columns, fixed = entry.get("columns", {}), entry.get("fixed", {})
            need(isinstance(columns, dict) and isinstance(fixed, dict), f"{label}: columns and fixed must be objects")
            # An amount computed from other columns has no column of its own to declare.
            declared_amounts = entry.get("amounts") if isinstance(entry.get("amounts"), dict) else {}
            expected = set(required[name]) - (set(AMOUNT[name]) if declared_amounts.get("value_from") else set())
            need(
                not set(columns) & set(fixed)
                and sorted(set(columns) | set(fixed)) == sorted(expected | (set(columns) & set(OPTIONAL[name])))
                and expected <= set(columns) | set(fixed)
                and set(fixed) <= set(FIXABLE[name]),
                f"{label}: every field needs exactly one column or allowed fixed value",
            )
            chosen = entry.get("include_when")
            need(
                chosen is None
                or (isinstance(chosen, dict) and set(chosen) == {"column", "values"} and isinstance(chosen.get("values"), list)
                    and chosen["values"] and all(isinstance(value, str) for value in chosen["values"])),
                f"{label}: include_when is invalid",
            )
            date_formats = entry.get("date_format", "YYYY-MM-DD")
            date_formats = [date_formats] if isinstance(date_formats, str) else date_formats
            need(
                isinstance(date_formats, list) and date_formats and all(name in DATE_FORMAT_NAMES for name in date_formats),
                f"{label}: unknown date_format",
            )
            shift = entry.get("time_zone_shift_hours", 0)
            need(type(shift) is int and -14 <= shift <= 14, f"{label}: invalid time_zone_shift_hours")
            value_from = amounts_of(entry).get("value_from")
            parsed_value = None
            if value_from is not None:
                need(
                    isinstance(value_from, dict) and len(value_from) == 1
                    and set(value_from) <= {"multiply", "add"}
                    and isinstance(next(iter(value_from.values())), list) and len(next(iter(value_from.values()))) == 2,
                    f"{label}: invalid value_from",
                )
                parsed_value = {"operation": next(iter(value_from)), "columns": list(next(iter(value_from.values())))}
            currency_holder = amounts_of(entry).get("currency", {})
            currency_column = currency_holder.get("column", "") if isinstance(currency_holder, dict) else ""
            currency_rates = currency_holder.get("rates", {}) if isinstance(currency_holder, dict) else {}
            need(
                isinstance(currency_rates, dict)
                and all(isinstance(value, str) and PLAIN_DECIMAL.fullmatch(value) for value in currency_rates.values()),
                f"{label}: invalid currency rates",
            )
            delimiter = entry.get("delimiter", "comma")
            encoding = entry.get("encoding", "utf-8")
            need(delimiter in DELIMITERS and encoding in ENCODINGS, f"{label}: unknown delimiter or encoding")
            amounts = entry.get("amounts", {})
            need(isinstance(amounts, dict), f"{label}: amounts must be an object")
            currency = amounts.get("currency", {})
            need(isinstance(currency, dict), f"{label}: currency must be an object")
            code = currency.get("code", "AED")
            rate = currency.get("aed_per_unit", "1")
            need(
                isinstance(rate, str) and PLAIN_DECIMAL.fullmatch(rate) is not None and Decimal(rate) > 0
                and (code != "AED" or rate == "1"),
                f"{label}: invalid currency rate",
            )
            refunds = amounts.get("refunds", "reject")
            need(refunds in REFUND_MODES and (name == "revenue" or refunds == "reject"), f"{label}: invalid refunds")
            uppercase = entry.get("uppercase", [])
            need(
                isinstance(uppercase, list) and all(isinstance(item, str) for item in uppercase)
                and set(uppercase) <= set(IDENTIFIERS[name]),
                f"{label}: invalid uppercase",
            )
            spec = {
                "path": config_path.parent / label, "label": label, "columns": columns, "fixed": fixed,
                "date_formats": tuple(date_formats), "time_shift": shift, "value_from": parsed_value,
                "currency_column": currency_column, "currency_rates": {k: v for k, v in currency_rates.items()}, "thousands_separator": amounts.get("thousands_separator", ""),
                "currency_label": amounts.get("currency_label", ""), "currency": code, "aed_per_unit": rate,
                "decimal": amounts.get("decimal", "point"), "header_row": entry.get("header_row", 1),
                "rate_source": currency.get("rate_source", ""), "refunds": refunds, "uppercase": set(uppercase),
                "required": required[name],
                "delimiter": delimiter, "encoding": encoding,
                "filter_column": chosen["column"] if chosen else "",
                "filter_values": tuple(sorted({value.casefold() for value in chosen["values"]})) if chosen else (),
            }
            files[name].append(spec)
            described.append(
                {
                    "source": name, "file": label, "columns": dict(columns), "fixed": dict(fixed),
                    "date_formats": list(date_formats), "time_zone_shift_hours": shift, "value_from": parsed_value,
                    "currency_column": currency_column,
                    "currency_rates": dict(sorted(currency_rates.items())),
                    "delimiter": delimiter, "encoding": encoding,
                    "thousands_separator": spec["thousands_separator"],
                    "currency_label": spec["currency_label"], "currency": code, "aed_per_unit": rate,
                    "decimal": spec["decimal"], "header_row": spec["header_row"],
                    "rate_source": spec["rate_source"], "refunds": refunds, "uppercase": sorted(set(uppercase)),
                    "include_when": {"column": chosen["column"], "values": sorted({v.casefold() for v in chosen["values"]})} if chosen else None,
                }
            )
    fingerprint = hashlib.sha256(raw).hexdigest()
    folded = {key.casefold(): value for key, value in aliases.items()}
    return {
        "files": files,
        "windows": windows,
        "aliases": folded,
        "join": join,
        "rule": rule,
        "threshold": threshold,
        "fingerprint": fingerprint,
        "declared": {
            "name": config.get("engagement"),
            "data_origin": config.get("data_origin"),
            "config_fingerprint": fingerprint,
            "attribution_windows_days": list(windows),
            "channel_aliases": dict(sorted(folded.items())),
            "revenue_join": join,
            "multiple_campaigns": rule,
            "coverage_threshold_percent": threshold,
            "files": described,
        },
    }


def _record_start_lines(text: str, delimiter: str = ",") -> list[int]:
    """Physical start line of every non-blank CSV record, found without the csv module.

    A small quote-aware scanner: newlines inside a quoted field continue the
    record, "\\r\\n", "\\r" and "\\n" each end one physical line, and an empty line
    is not a record. The engine derives the same numbers from the csv module's
    reader position; two methods agreeing is what makes a reference trustworthy.
    """
    starts: list[int] = []
    line = 1
    record_line = 1
    empty = True
    state = "field_start"
    index = 0
    while index < len(text):
        char = text[index]
        crlf = text.startswith("\r\n", index)
        if char in "\r\n" and state != "quoted":
            if not empty:
                starts.append(record_line)
            index += 2 if crlf else 1
            line += 1
            record_line, empty, state = line, True, "field_start"
            continue
        empty = False
        if state == "field_start":
            state = "quoted" if char == '"' else "field_start" if char == delimiter else "unquoted"
        elif state == "unquoted":
            if char == delimiter:
                state = "field_start"
        elif state == "quoted":
            if char == '"':
                state = "closing_quote"
            elif char in "\r\n":
                line += 1
                if crlf:
                    index += 1
        elif state == "closing_quote":
            state = "quoted" if char == '"' else "field_start" if char == delimiter else "unquoted"
        index += 1
    if not empty:
        starts.append(record_line)
    return starts


def _read_rows(spec: dict) -> list[tuple[int, dict]]:
    path = Path(spec["path"])
    name = spec["label"] or path.name
    try:
        text = path.read_bytes().decode(ENCODINGS[spec["encoding"]])
    except OSError as exc:
        raise ReconstructionError(f"cannot read {name} ({type(exc).__name__})") from exc
    except (UnicodeDecodeError, UnicodeError) as exc:
        raise ReconstructionError(
            f"{name} is not UTF-8 text" if spec["encoding"] == "utf-8"
            else f"{name} is not {spec['encoding']} text as declared"
        ) from exc
    try:
        records = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=DELIMITERS[spec["delimiter"]]) if r]
    except csv.Error as exc:
        raise ReconstructionError(f"{name} is not a readable CSV ({exc})") from exc
    record_starts = _record_start_lines(text, DELIMITERS[spec["delimiter"]])
    if len(record_starts) != len(records):
        raise ReconstructionError(f"cannot align the records of {name} to physical lines")
    # An export may print a title above its header row; the lines above it are not records.
    kept = [pair for pair in zip(record_starts, records) if pair[0] >= spec["header_row"]]
    if not kept:
        raise ReconstructionError(f"{name} has no header row at line {spec['header_row']}")
    fieldnames = list(kept[0][1])
    rows = []
    for _start, record in kept[1:]:
        row: dict = dict(zip(fieldnames, record))
        if len(record) > len(fieldnames):
            row[None] = record[len(fieldnames):]
        for absent in fieldnames[len(record):]:
            row[absent] = None
        rows.append(row)
    headers = [spec["columns"][field] for field in spec["required"] if field in spec["columns"]]
    headers += [header for header in (
        spec["filter_column"], spec["currency_column"], *(spec["value_from"]["columns"] if spec["value_from"] else ())
    ) if header]
    missing = [header for header in headers if header not in fieldnames]
    if missing:
        raise ReconstructionError(f"{name} is missing required columns: {', '.join(sorted(missing))}")
    repeated = sorted({header for header in headers if fieldnames.count(header) > 1})
    if repeated:
        raise ReconstructionError(f"{name} has more than one column named {', '.join(repeated)}")
    return [(start, row) for (start, _record), row in zip(kept[1:], rows)]


def _read_csv_output(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError as exc:
        raise ReconstructionError(f"cannot read {path.name} ({type(exc).__name__})") from exc


def _resolve(items: list[dict], key: str, signature, status: dict) -> tuple[list[dict], dict[str, list[str]]]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item[key], []).append(item)
    kept: list[dict] = []
    conflicts: dict[str, list[str]] = {}
    for identifier, members in groups.items():
        members.sort(key=lambda item: item["row"])
        if len({signature(item) for item in members}) > 1:
            conflicts[identifier] = [item["ref"] for item in members]
            for item in members:
                status[item["ref"]] = "conflict"
            continue
        kept.append(members[0])
        status[members[0]["ref"]] = "accepted"
        for item in members[1:]:
            status[item["ref"]] = "duplicate"
    kept.sort(key=lambda item: item["row"])
    return kept, conflicts


def derive(
    sources: dict, windows: list[int], aliases: dict | None = None, join: str = "lead_id", rule: str = "unattributed"
) -> dict:
    """Every row status and every published figure, from the raw exports alone.

    sources maps each source to one CSV path (the three-file contract) or to the
    file specs read from an engagement config.
    """
    specs = {
        name: sources[name] if isinstance(sources[name], list) else [_plain_spec(name, sources[name])]
        for name in SOURCES
    }
    aliases = aliases or {}
    raw = {
        name: [(index, spec, number, row) for index, spec in enumerate(specs[name]) for number, row in _read_rows(spec)]
        for name in SOURCES
    }
    status: dict[str, str] = {}
    amount: dict[str, str] = {}
    source_of: dict[str, str] = {}
    order: dict[str, tuple] = {}

    def track(name: str, index: int, spec: dict, number: int) -> str:
        ref = _ref(name, spec["label"], number)
        source_of[ref], order[ref], amount[ref] = name, (SOURCES.index(name), index, number), ""
        return ref

    def read_date(row: dict, spec: dict, field: str):
        text = _field(row, spec, field)
        return _declared_date(text, spec["date_formats"], spec["time_shift"]) if spec["label"] else _day(text)

    def read_amount(row: dict, spec: dict, field: str):
        return _declared_amount(row, spec, field) if spec["label"] else _amount(_field(row, spec, field))

    valid_ads = []
    for index, spec, number, row in raw["ads"]:
        ref = track("ads", index, spec, number)
        if not _included(row, spec):
            status[ref] = "filtered"
            spend = read_amount(row, spec, "spend_aed")
            if spend is not None:
                amount[ref] = _money(spend)
            continue
        campaign, channel = _field(row, spec, "campaign_id"), _field(row, spec, "channel")
        channel = aliases.get(channel.casefold(), channel)
        row_id = _field(row, spec, "row_id") if "row_id" in spec["columns"] else ""
        day, spend = read_date(row, spec, "date"), read_amount(row, spec, "spend_aed")
        if spend is not None:
            amount[ref] = _money(spend)
        if (
            not campaign or not channel or any(mark in campaign + channel for mark in "[]")
            or day is None or spend is None or ("row_id" in spec["columns"] and not row_id)
        ):
            status[ref] = "rejected"
            continue
        valid_ads.append({"ref": ref, "row": order[ref], "date": day, "campaign": campaign, "channel": channel,
                          "spend": spend, "row_id": row_id})
    channels_by_campaign: dict[str, set] = {}
    for item in valid_ads:
        channels_by_campaign.setdefault(item["campaign"], set()).add(item["channel"])
    remaining = []
    for item in valid_ads:
        if len(channels_by_campaign[item["campaign"]]) > 1:
            status[item["ref"]] = "conflict"
            continue
        remaining.append(item)
    if remaining and remaining[0]["row_id"]:
        ads, _ = _resolve(
            remaining, "row_id", lambda item: (item["date"], item["campaign"], item["channel"], item["spend"]), status
        )
    else:
        ads, seen = [], set()
        for item in remaining:
            signature = (item["date"], item["campaign"], item["channel"], item["spend"])
            if signature in seen:
                status[item["ref"]] = "duplicate"
                continue
            seen.add(signature)
            status[item["ref"]] = "accepted"
            ads.append(item)

    customer_join = join == "customer_id"
    valid_crm = []
    for index, spec, number, row in raw["crm"]:
        ref = track("crm", index, spec, number)
        if not _included(row, spec):
            status[ref] = "filtered"
            continue
        lead, campaign = _field(row, spec, "lead_id"), _field(row, spec, "campaign_id")
        customer = _field(row, spec, "customer_id") if customer_join else ""
        created, stage = read_date(row, spec, "created_at"), _field(row, spec, "status").lower()
        if not lead or not campaign or created is None or not stage or (customer_join and not customer):
            status[ref] = "rejected"
            continue
        valid_crm.append(
            {"ref": ref, "row": order[ref], "lead": lead, "campaign": campaign, "created": created, "stage": stage, "customer": customer}
        )
    crm, conflicting_leads = _resolve(
        valid_crm, "lead", lambda item: (item["campaign"], item["created"], item["stage"], item["customer"]), status
    )

    link = "customer_id" if customer_join else "lead_id"
    line_items = any("line_id" in spec["columns"] for spec in specs["revenue"])
    valid_revenue = []
    for index, spec, number, row in raw["revenue"]:
        ref = track("revenue", index, spec, number)
        if not _included(row, spec):
            status[ref] = "filtered"
            value = read_amount(row, spec, "value_aed")
            if value is not None:
                amount[ref] = _money(value)
            continue
        transaction, lead = _field(row, spec, "transaction_id"), _field(row, spec, link)
        line = _field(row, spec, "line_id") if line_items else ""
        value, day = read_amount(row, spec, "value_aed"), read_date(row, spec, "date")
        if value is not None:
            amount[ref] = _money(value)
        if not transaction or not lead or value is None or day is None or (line_items and not line):
            status[ref] = "rejected"
            continue
        valid_revenue.append({"ref": ref, "row": order[ref], "transaction": transaction, "lead": lead, "value": value,
                              "date": day, "identity": f"{transaction}\x1f{line}" if line_items else transaction})
    accepted_lines, _ = _resolve(
        valid_revenue, "identity", lambda item: (item["lead"], item["value"], item["date"]), status
    )
    if line_items:
        by_invoice: dict[str, list] = {}
        for item in accepted_lines:
            by_invoice.setdefault(item["transaction"], []).append(item)
        revenue = []
        for transaction, lines in by_invoice.items():
            if len({line["lead"] for line in lines}) > 1 or len({line["date"] for line in lines}) > 1:
                for line in lines:
                    status[line["ref"]] = "conflict"
                continue
            first = min(lines, key=lambda line: line["row"])
            revenue.append({
                "ref": first["ref"], "row": first["row"], "transaction": transaction, "lead": first["lead"],
                "date": first["date"], "value": sum((line["value"] for line in lines), Decimal("0")),
                "refs": [line["ref"] for line in lines], "rows": lines,
            })
        revenue.sort(key=lambda invoice: invoice["row"])
    else:
        revenue = [{**item, "refs": [item["ref"]], "rows": [item]} for item in accepted_lines]

    # Revenue reaches CRM records through its lead, or through every accepted lead of its customer.
    def key_of(item: dict) -> str:
        return item["customer"] if customer_join else item["lead"]

    leads: dict[str, list] = {}
    for item in crm:
        leads.setdefault(key_of(item), []).append(item)
    crm_by_ref = {item["ref"]: item for item in valid_crm}
    conflicting: dict[str, set] = {}
    for members in conflicting_leads.values():
        for ref in members:
            conflicting.setdefault(key_of(crm_by_ref[ref]), set()).add(ref)
    campaign_channel = {item["campaign"]: item["channel"] for item in ads}
    attributed, unattributed, conflicted = [], [], []
    for item in revenue:
        candidates = sorted(leads.get(item["lead"], []), key=lambda lead: (lead["created"], lead["row"]))
        first = candidates[0] if candidates else None
        if rule == "last_touch":
            by_then = [lead for lead in candidates if lead["created"] <= item["date"]]
            first = by_then[-1] if by_then else first
        single_campaign = rule != "unattributed" or len({lead["campaign"] for lead in candidates}) == 1
        channel = campaign_channel.get(first["campaign"]) if first is not None and single_campaign else None
        if first is None or channel is None or item["date"] < first["created"]:
            for line in item["rows"]:
                status[line["ref"]] = "accepted-unattributed"
            unattributed.append(item)
            if first is None and item["lead"] in conflicting:
                conflicted.append(item)
            continue
        attributed.append(
            {
                **item,
                "channel": channel,
                "crm_refs": {lead["ref"] for lead in candidates},
                "delay": (item["date"] - first["created"]).days,
            }
        )

    def total(items, key):
        return sum((item[key] for item in items), Decimal("0"))

    def refs(items):
        return {ref for item in items for ref in item.get("refs", [item["ref"]])}

    def joins(items):
        return {ref for item in items for ref in item["crm_refs"]}

    claims: dict[str, dict] = {}

    def claim(evidence_id, value, unit, **roles):
        claims[evidence_id] = {
            "value": value,
            "unit": unit,
            "roles": {role: set(members) for role, members in roles.items() if members},
        }

    spend_total, revenue_total = total(ads, "spend"), total(revenue, "value")
    attributed_total = total(attributed, "value")
    coverage = Decimal("0") if revenue_total == 0 else attributed_total / revenue_total
    coverage_text = str((coverage * 100).quantize(Decimal("0.1")))
    claim("EV-SPEND-001", _money(spend_total), "AED", summand=refs(ads))
    claim("EV-REV-001", _money(revenue_total), "AED", summand=refs(revenue))
    claim("EV-ATTR-001", _money(attributed_total), "AED", summand=refs(attributed), join=joins(attributed))
    claim(
        "EV-COVER-001", coverage_text, "percent",
        numerator=refs(attributed), denominator=refs(revenue), join=joins(attributed),
    )
    claim("EV-UNMATCH-001", _money(total(unattributed, "value")), "AED", summand=refs(unattributed))
    claim(
        "EV-CONFLICT-001", _money(total(conflicted, "value")), "AED",
        summand=refs(conflicted),
        join={ref for item in conflicted for ref in conflicting[item["lead"]]},
    )
    declares_refunds = any(spec["refunds"] != "reject" for spec in specs["revenue"])
    refund_rows = [line for invoice in revenue for line in invoice["rows"] if line["value"] < 0]
    if declares_refunds:
        claim("EV-REFUND-001", _money(total(refund_rows, "value")), "AED", summand=refs(refund_rows))

    channels = []
    for index, name in enumerate(sorted({item["channel"] for item in ads}), start=1):
        ids = {
            "spend": f"EV-CHSPEND-{index:03d}",
            "attributed_revenue": f"EV-CHATTR-{index:03d}",
            "roas": f"EV-CHROAS-{index:03d}",
        }
        channel_ads = [item for item in ads if item["channel"] == name]
        channel_attributed = [item for item in attributed if item["channel"] == name]
        spend, income = total(channel_ads, "spend"), total(channel_attributed, "value")
        roas = str((Decimal("0") if spend == 0 else income / spend).quantize(CENT))
        claim(ids["spend"], _money(spend), "AED", summand=refs(channel_ads))
        claim(ids["attributed_revenue"], _money(income), "AED", summand=refs(channel_attributed), join=joins(channel_attributed))
        claim(
            ids["roas"], roas, "ratio",
            numerator=refs(channel_attributed), denominator=refs(channel_ads), join=joins(channel_attributed),
        )
        channels.append(
            {
                "channel": name,
                "spend_aed": _money(spend),
                "attributed_revenue_aed": _money(income),
                "assumption_dependent_roas": roas,
                "evidence_ids": ids,
            }
        )

    sensitivity = []
    for window in windows:
        ids = {"attributed": f"EV-WINATTR-{window:03d}", "excluded": f"EV-WINEXCL-{window:03d}"}
        inside = [item for item in attributed if item["delay"] <= window]
        outside = [item for item in attributed if item["delay"] > window]
        claim(ids["attributed"], _money(total(inside, "value")), "AED", summand=refs(inside), join=joins(inside))
        claim(ids["excluded"], _money(total(outside, "value")), "AED", summand=refs(outside), join=joins(outside))
        sensitivity.append(
            {
                "attribution_window_days": window,
                "attributed_revenue_aed": _money(total(inside, "value")),
                "excluded_delayed_revenue_aed": _money(total(outside, "value")),
                "evidence_ids": ids,
            }
        )

    counts = {name: {"input": len(raw[name]), **{value: 0 for value in STATUSES}} for name in SOURCES}
    for ref, value in status.items():
        counts[source_of[ref]][value] += 1
    summary = {
        "accepted_spend_aed": _money(spend_total),
        "accepted_revenue_aed": _money(revenue_total),
        "attributed_revenue_aed": _money(attributed_total),
        "unattributed_or_invalid_revenue_aed": _money(total(unattributed, "value")),
        "conflicting_lead_revenue_aed": _money(total(conflicted, "value")),
        "attribution_coverage_percent": coverage_text,
        "accepted_rows": {"ads": len(ads), "crm": len(crm), "revenue": len(revenue)},
        "row_status_counts": counts,
        "rejected_or_uncertain_rows": sum(1 for value in status.values() if value != "accepted"),
    }
    if declares_refunds:
        summary["refunded_revenue_aed"] = _money(total(refund_rows, "value"))
    return {
        "status": status,
        "amount": amount,
        "order": order,
        "source_of": source_of,
        "claims": claims,
        "channels": channels,
        "sensitivity": sensitivity,
        "summary": summary,
    }


def _role_sets(entries) -> dict[str, set]:
    roles: dict[str, set] = {}
    for entry in entries:
        roles.setdefault(entry["role"], set()).add(entry["source_ref"])
    return roles


def verify_outputs(source_paths: dict | None, output_dir) -> tuple[list[str], list[str]]:
    """Return the checks run and a list of failures; an empty list means every check passed."""
    output = Path(output_dir)
    failures: list[str] = []

    def fail(check: str, message: str) -> None:
        failures.append(f"{check}: {message}")

    try:
        config_path = (source_paths or {}).get("config")
        if not config_path and (not source_paths or any(not source_paths.get(name) for name in SOURCES)):
            raise ReconstructionError("source CSV paths were not supplied")
        try:
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReconstructionError(f"cannot read report.json ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise ReconstructionError("report.json is not valid JSON") from exc

        config = _load_config(config_path) if config_path else None
        if config:
            inputs = config["files"]
            files = [(f"{name}/{spec['label']}", spec["path"]) for name in SOURCES for spec in inputs[name]]
            published_engagement = report.get("engagement") or {}
            if published_engagement.get("config_fingerprint") != config["fingerprint"]:
                fail("engagement_config", "the engagement config changed since the report was computed")
            elif published_engagement != config["declared"]:
                fail("engagement_config", "the declared interpretations in report.json do not match the engagement config")
        else:
            inputs = {name: source_paths[name] for name in SOURCES}
            files = [(name, source_paths[name]) for name in SOURCES]
            if report.get("engagement") is not None:
                fail("engagement_config", "report.json declares an engagement config, but none was supplied")
        published_fingerprints = report.get("source_fingerprints", {})
        for key, file_path in files:
            try:
                actual = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
            except OSError as exc:
                raise ReconstructionError(f"cannot read the {key} export ({type(exc).__name__})") from exc
            if published_fingerprints.get(key) != actual:
                fail("sources_match_fingerprints", f"the {key} export changed since the report was computed")
        unlisted = set(published_fingerprints) - {key for key, _ in files}
        if unlisted:
            fail("sources_match_fingerprints", f"report.json lists exports that were not supplied: {_limited(unlisted)}")

        ruleset = dict(report.get("ruleset", {}))
        declared = ruleset.pop("fingerprint", None)
        computed = hashlib.sha256(json.dumps(ruleset, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if declared != computed:
            fail("ruleset_fingerprint_and_run_id", "the published rule set does not match its fingerprint")
        identity = "\n".join(
            [f"engine:{report.get('engine_version')}", f"ruleset:{computed}", f"as_of:{report.get('as_of') or ''}"]
            + ([f"config:{config['fingerprint']}"] if config else [])
            + [f"{name}:{digest}" for name, digest in sorted(report.get("source_fingerprints", {}).items())]
        )
        if report.get("run_id") != hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]:
            fail("ruleset_fingerprint_and_run_id", "run_id does not identify these inputs, engine and rule set")
        windows = config["windows"] if config else ruleset.get("attribution_windows_days")
        if not isinstance(windows, list) or not windows or not all(isinstance(w, int) and w > 0 for w in windows):
            raise ReconstructionError("the rule set does not declare valid attribution windows")
        if config and ruleset.get("attribution_windows_days") != windows:
            fail("engagement_config", "the rule set's attribution windows differ from the engagement config")
        if config and ruleset.get("coverage_threshold_percent") != config["threshold"]:
            fail("engagement_config", "the rule set's coverage threshold differs from the engagement config")

        expected = (
            derive(inputs, windows, config["aliases"], config["join"], config["rule"]) if config else derive(inputs, windows)
        )

        dispositions = _read_csv_output(output / "row_dispositions.csv")
        seen_refs = [_ref(row["source"], row.get("source_file", ""), row["source_row"]) for row in dispositions]
        # Counted once, not scanned per row: a real export has hundreds of thousands of rows.
        duplicated = {ref for ref, times in Counter(seen_refs).items() if times > 1}
        if duplicated:
            fail("row_dispositions", f"rows listed more than once: {_limited(duplicated)}")
        missing = set(expected["status"]) - set(seen_refs)
        unexpected = set(seen_refs) - set(expected["status"])
        if missing:
            fail("row_dispositions", f"input rows without a status: {_limited(missing)}")
        if unexpected:
            fail("row_dispositions", f"statuses for rows not in the exports: {_limited(unexpected)}")
        wrong_status = [
            f"{ref} is {row['status']}, expected {expected['status'][ref]}"
            for ref, row in zip(seen_refs, dispositions)
            if ref in expected["status"] and row["status"] != expected["status"][ref]
        ]
        if wrong_status:
            fail("row_dispositions", f"status disagrees: {_limited(wrong_status)}")
        wrong_amount = [
            f"{ref} shows '{row['amount_aed']}', expected '{expected['amount'][ref]}'"
            for ref, row in zip(seen_refs, dispositions)
            if ref in expected["amount"] and row["amount_aed"] != expected["amount"][ref]
        ]
        if wrong_amount:
            fail("row_dispositions", f"amount disagrees: {_limited(wrong_amount)}")

        rejected_rows = _read_csv_output(output / "rejected_records.csv")
        published_rejected = {
            (_ref(row["source"], row.get("source_file", ""), row["source_row"]), row["status"]) for row in rejected_rows
        }
        expected_rejected = {(ref, value) for ref, value in expected["status"].items() if value != "accepted"}
        if published_rejected != expected_rejected:
            difference = published_rejected ^ expected_rejected
            fail("rejected_records", f"rejected_records.csv disagrees on: {_limited(ref for ref, _ in difference)}")

        # Exception groups use the published reason labels, but their row counts, amounts
        # and examples come from the reconstructed statuses and amounts.
        groups: dict[tuple, list] = {}
        for ref, row in sorted(zip(seen_refs, dispositions), key=lambda pair: expected["order"].get(pair[0], (9,))):
            want_status = expected["status"].get(ref)
            if want_status in (None, "accepted"):
                continue
            groups.setdefault((expected["source_of"][ref], want_status, row.get("reason", "")), []).append(ref)
        expected_exceptions = []
        for (name, value, reason), members in groups.items():
            amounts = [Decimal(expected["amount"][ref]) for ref in members if expected["amount"][ref]]
            expected_exceptions.append(
                {
                    "source": name,
                    "status": value,
                    "reason": reason,
                    "rows": len(members),
                    "amount_aed": _money(sum(amounts, Decimal("0"))),
                    "rows_without_amount": len(members) - len(amounts),
                    "example_refs": members[:3],
                }
            )
        expected_exceptions.sort(
            key=lambda item: (
                -abs(Decimal(item["amount_aed"])), -item["rows"], SOURCES.index(item["source"]), item["status"], item["reason"]
            )
        )
        exception_fields = ("source", "status", "reason", "rows", "amount_aed", "rows_without_amount", "example_refs")
        published_exceptions = [{field: item.get(field) for field in exception_fields} for item in report.get("exceptions", [])]
        if published_exceptions != expected_exceptions:
            fail("exceptions", "exception groups in report.json do not match the reconstructed row statuses and amounts")
        exceptions_path = output / "exceptions.csv"
        if not exceptions_path.exists():
            fail("exceptions", "exceptions.csv is missing")
        else:
            as_csv = [
                {**item, "rows": str(item["rows"]), "rows_without_amount": str(item["rows_without_amount"]),
                 "example_refs": ";".join(item["example_refs"])}
                for item in expected_exceptions
            ]
            written = [{field: row.get(field) for field in exception_fields} for row in _read_csv_output(exceptions_path)]
            if written != as_csv:
                fail("exceptions", "exceptions.csv does not match the reconstructed exception groups")

        expected_entries = sum(len(members) for claim in expected["claims"].values() for members in claim["roles"].values())
        detail = report.get("detail_in_files", {})
        rows_in_report = len(expected["status"]) <= REPORT_ROW_LIMIT
        lineage_in_report = expected_entries <= REPORT_LINEAGE_LIMIT
        if rows_in_report and "dispositions" not in report:
            fail("claim_lineage", "report.json omits row dispositions that are small enough to publish")
        if not rows_in_report and detail.get("dispositions") != len(expected["status"]):
            fail("claim_lineage", "report.json does not say how many row dispositions are in row_dispositions.csv")
        if not lineage_in_report and detail.get("lineage_entries") != expected_entries:
            fail("claim_lineage", "report.json does not say how many lineage entries are in lineage.csv")

        published = {claim["evidence_id"]: claim for claim in report.get("claims", [])}
        for evidence_id in sorted(set(expected["claims"]) - set(published)):
            fail("claim_values", f"{evidence_id} is missing from the report")
        for evidence_id in sorted(set(published) - set(expected["claims"])):
            fail("claim_values", f"{evidence_id} is published but cannot be reconstructed")
        for evidence_id, want in expected["claims"].items():
            have = published.get(evidence_id)
            if have is None:
                continue
            if (have.get("value"), have.get("unit")) != (want["value"], want["unit"]):
                fail(
                    "claim_values",
                    f"{evidence_id} is {have.get('value')} {have.get('unit')}, reconstructed {want['value']} {want['unit']}",
                )
            if lineage_in_report:
                if _role_sets(have.get("lineage", [])) != want["roles"]:
                    fail("claim_lineage", f"{evidence_id} lineage in report.json does not match its reconstructed rows")
            elif have.get("lineage_entries") != sum(len(members) for members in want["roles"].values()):
                fail("claim_lineage", f"{evidence_id} does not state how many lineage rows it has in lineage.csv")

        lineage_rows = _read_csv_output(output / "lineage.csv")
        by_claim: dict[str, list] = {}
        for row in lineage_rows:
            by_claim.setdefault(row["evidence_id"], []).append(row)
        for evidence_id in sorted(set(by_claim) | set(expected["claims"])):
            want = expected["claims"].get(evidence_id, {}).get("roles", {})
            if _role_sets(by_claim.get(evidence_id, [])) != want:
                fail("claim_lineage", f"{evidence_id} lineage in lineage.csv does not match its reconstructed rows")

        supports_expected: dict[str, set] = {}
        for row in lineage_rows:
            supports_expected.setdefault(row["source_ref"], set()).add(f"{row['evidence_id']}:{row['role']}")
        wrong_supports = [
            ref
            for ref, row in zip(seen_refs, dispositions)
            if set(filter(None, row.get("supports", "").split(";"))) != supports_expected.get(ref, set())
        ]
        if wrong_supports:
            fail("row_dispositions", f"supports column disagrees with lineage.csv for: {_limited(wrong_supports)}")

        if report.get("summary") != expected["summary"]:
            keys = sorted(
                key
                for key in set(report.get("summary", {})) | set(expected["summary"])
                if report.get("summary", {}).get(key) != expected["summary"].get(key)
            )
            fail("summary", f"summary disagrees on: {_limited(keys)}")

        channel_fields = ("channel", "spend_aed", "attributed_revenue_aed", "assumption_dependent_roas", "evidence_ids")
        published_channels = [{field: row.get(field) for field in channel_fields} for row in report.get("channels", [])]
        if published_channels != expected["channels"]:
            fail("channels_and_sensitivity", "channel figures in report.json do not match reconstruction")
        window_fields = ("attribution_window_days", "attributed_revenue_aed", "excluded_delayed_revenue_aed", "evidence_ids")
        published_windows = [{field: row.get(field) for field in window_fields} for row in report.get("sensitivity", [])]
        if published_windows != expected["sensitivity"]:
            fail("channels_and_sensitivity", "sensitivity figures in report.json do not match reconstruction")

        brief_path = output / "executive_brief.md"
        brief = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
        if not brief:
            fail("executive_brief", "executive_brief.md is missing or empty")
        for evidence_id, want in expected["claims"].items():
            if brief and f"[{evidence_id}]" not in brief:
                fail("executive_brief", f"the brief omits {evidence_id}")
            elif brief and _shown(want) not in brief:
                fail("executive_brief", f"the brief does not show {_shown(want)} for {evidence_id}")
        for finding in report.get("recommendation", {}).get("findings", []):
            for evidence_id in CITATION.findall(finding):
                want = expected["claims"].get(evidence_id)
                if want is None:
                    fail("executive_brief", f"a finding cites {evidence_id}, which cannot be reconstructed")
                elif _shown(want) not in finding:
                    fail("executive_brief", f"a finding citing {evidence_id} does not state {_shown(want)}")
        for item in expected_exceptions[:10]:
            if brief and f"| {item['rows']} | {_exception_cell(item)} |" not in brief:
                fail("executive_brief", f"the brief does not list the {item['source']} {item['status']} exception group")

        counts = expected["summary"]["row_status_counts"]
        for name in SOURCES:
            usable = counts[name]["accepted"] + counts[name]["accepted-unattributed"]
            if usable == 0:
                breakdown = ", ".join(f"{counts[name][value]} {value}" for value in STATUSES if counts[name][value])
                fail(
                    "sufficient_evidence",
                    f"the {name} export has no usable rows ({breakdown or 'no data rows'})",
                )
    except ReconstructionError as exc:
        failures.append(f"inputs: {exc}")

    # One wrong row status changes every figure built on it. List the row-level
    # disagreements in full and summarise what follows from them, so the cause is
    # not buried under dozens of consequences.
    if any(failure.startswith("row_dispositions: status disagrees") for failure in failures):
        collapsed: dict[str, int] = {}
        kept = []
        for failure in failures:
            check = failure.split(":", 1)[0]
            if check in DOWNSTREAM_CHECKS:
                collapsed[check] = collapsed.get(check, 0) + 1
            else:
                kept.append(failure)
        failures = kept + [
            f"{check}: {count} discrepancies follow from the row status disagreements above"
            for check, count in collapsed.items()
        ]
    return list(CHECKS), failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independently reconstruct and verify a revenue evidence report")
    parser.add_argument("--ads")
    parser.add_argument("--crm")
    parser.add_argument("--revenue")
    parser.add_argument("--config", help="The engagement config the report was computed from")
    parser.add_argument("--output", required=True, help="Directory containing the published outputs")
    args = parser.parse_args(argv)
    plain = [args.ads, args.crm, args.revenue]
    if args.config and any(plain):
        parser.error("use either --config or --ads, --crm and --revenue, not both")
    if not args.config and not all(plain):
        parser.error("--ads, --crm and --revenue are required unless --config is given")
    sources = {"config": args.config} if args.config else {"ads": args.ads, "crm": args.crm, "revenue": args.revenue}
    checks, failures = verify_outputs(sources, args.output)
    print(json.dumps({"status": "FAIL" if failures else "PASS", "checks": checks, "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

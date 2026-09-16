from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from pathlib import Path
from typing import Any, Iterable

from .reconstruct import verify_outputs
from zoneinfo import ZoneInfo


MONEY = Decimal("0.01")

# Part of every run_id: a change to the computation must not reuse an old identity.
# Kept equal to pyproject.toml's version by test.
ENGINE_VERSION = "0.4.0"

# Every threshold and window the report depends on, published in the report. The
# whole content (not just the version label) is hashed into run_id.
RULESET: dict[str, Any] = {
    "id": "pas95-revenue-evidence-rules",
    "version": "1",
    "attribution_windows_days": [30, 60, 90],
    "coverage_threshold_percent": "80",
    "coverage_threshold_basis": (
        "planning choice used only to flag weak attribution coverage; not a validated decision threshold"
    ),
}

REASON_CONFLICTING_LEAD = "lead_id has conflicting CRM records; revenue cannot be attributed"
REASON_NO_LEAD = "lead_id has no accepted CRM record; revenue cannot be attributed"
REASON_CONFLICTING_CAMPAIGN = "CRM campaign_id maps to conflicting advertising channels"
REASON_NO_CAMPAIGN = "CRM campaign_id has no accepted advertising record"
REASON_CHRONOLOGY = "revenue date precedes lead creation date"
REASON_NO_CUSTOMER = "customer_id has no accepted CRM record; revenue cannot be attributed"
REASON_CONFLICTING_CUSTOMER = "customer_id has only conflicting CRM records; revenue cannot be attributed"
REASON_CUSTOMER_CAMPAIGNS = "customer_id has accepted CRM leads from more than one campaign"

# Every input row receives exactly one status. Only the two "accepted" statuses
# may contribute to a total; the others are visible but excluded.
ACCEPTED = "accepted"
ACCEPTED_UNATTRIBUTED = "accepted-unattributed"
REJECTED = "rejected"
DUPLICATE = "duplicate"
CONFLICT = "conflict"
# A row the engagement config excludes by a declared filter, such as an unpaid invoice.
FILTERED = "filtered"
STATUSES = (ACCEPTED, ACCEPTED_UNATTRIBUTED, REJECTED, DUPLICATE, CONFLICT, FILTERED)
INCLUDED_STATUSES = frozenset({ACCEPTED, ACCEPTED_UNATTRIBUTED})
SEVERITY_BY_STATUS = {
    REJECTED: "rejected",
    FILTERED: "excluded",
    DUPLICATE: "rejected",
    CONFLICT: "uncertain",
    ACCEPTED_UNATTRIBUTED: "uncertain",
}
SOURCE_ORDER = {"ads": 0, "crm": 1, "revenue": 2}

# How a source row supports a claim. Summands add up to the claim's value;
# numerator and denominator rows form a ratio; join rows are linkage evidence
# (such as the CRM lead connecting revenue to a campaign) and carry no amount.
# report.json repeats what the CSV files hold. On a large export that repetition is
# what makes it unopenable, so above these sizes the detail stays in the CSVs and the
# report states how many rows it left there. Both implementations apply the same rule,
# so an engine cannot quietly drop lineage from a small run.
REPORT_ROW_LIMIT = 50_000
REPORT_LINEAGE_LIMIT = 200_000
LINEAGE_ROLES = ("summand", "numerator", "denominator", "join")
CONTRIBUTING_ROLES = frozenset({"summand", "numerator", "denominator"})


class InputContractError(ValueError):
    """Raised when an input file cannot satisfy the required schema."""


@dataclass(frozen=True)
class RowDisposition:
    source: str
    source_row: int
    record_id: str
    status: str
    reason: str
    amount_aed: str
    # The export file as named in an engagement config; empty for the three-file contract.
    source_file: str = ""


@dataclass(frozen=True)
class RejectedRecord:
    source: str
    source_row: int
    record_id: str
    severity: str
    reason: str
    status: str
    source_file: str = ""


@dataclass(frozen=True)
class Claim:
    evidence_id: str
    metric: str
    value: str
    unit: str
    grade: str
    explanation: str
    source_refs: list[str]
    assumptions: list[str]
    lineage: list[dict[str, str]]
    # Qualifiers that belong to the figure, such as its channel or window length.
    # A narrative may repeat these next to the claim it cites.
    dimensions: dict[str, str]


@dataclass
class EvidenceReport:
    run_id: str
    # An optional reporting date supplied by the caller. The wall clock is never
    # read, so identical inputs produce identical bytes.
    as_of: str | None
    engine_version: str
    ruleset: dict[str, Any]
    source_fingerprints: dict[str, str]
    summary: dict[str, Any]
    channels: list[dict[str, Any]]
    sensitivity: list[dict[str, Any]]
    claims: list[Claim]
    rejected_records: list[RejectedRecord]
    dispositions: list[RowDisposition]
    recommendation: dict[str, Any]
    human_approval: dict[str, Any]
    narrative: dict[str, Any]
    # Rows excluded from every figure, or counted without a channel, grouped by
    # reason with the amount they hold, largest first.
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    # The declared interpretations of an engagement config; None for the plain
    # three-file contract.
    engagement: dict[str, Any] | None = None
    # Where the exports (or the engagement config) were read from, so publication
    # can re-read them for independent verification. Never serialised: paths
    # differ between machines.
    source_paths: dict[str, str] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("source_paths", None)
        return payload


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _grouped(amount: str) -> str:
    """A money string as a reader expects it: 24500.00 becomes 24,500.00."""
    return f"{Decimal(amount):,.2f}"


PLAIN_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _parse_money(value: str | None, field: str) -> Decimal:
    """A plain non-negative decimal such as 1000 or 1000.50, rounded half-up to fils.

    Exponents (1e3), signs, thousands separators, underscores, bare points and
    non-ASCII digits are refused: each is a spelling two readers could take
    differently, and silently choosing one would put a guess into the books.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not PLAIN_DECIMAL.fullmatch(text):
        raise ValueError(f"{field} must be a plain non-negative decimal such as 1000 or 1000.50")
    try:
        return Decimal(text).quantize(MONEY, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"{field} is too large to represent exactly") from exc


def _parse_date(value: str | None, field: str) -> date:
    text = value.strip() if isinstance(value, str) else ""
    try:
        if not ISO_DATE.fullmatch(text):
            raise ValueError(text)
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _text(row: dict[str, Any], field: str) -> str:
    """A field's trimmed text; a short row yields an empty value, never a crash."""
    value = row.get(field)
    return value.strip() if isinstance(value, str) else ""


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


SOURCES = ("ads", "crm", "revenue")
CANONICAL_FIELDS: dict[str, tuple[str, ...]] = {
    "ads": ("date", "campaign_id", "channel", "spend_aed"),
    "crm": ("lead_id", "campaign_id", "created_at", "status"),
    "revenue": ("transaction_id", "lead_id", "value_aed", "date"),
}
AMOUNT_FIELDS = {"ads": ("spend_aed",), "crm": (), "revenue": ("value_aed",)}
IDENTIFIER_FIELDS = {
    "ads": ("campaign_id",),
    "crm": ("lead_id", "campaign_id", "customer_id"),
    "revenue": ("transaction_id", "lead_id", "customer_id"),
}
# When revenue records name a customer instead of a lead, CRM rows carry that
# customer and revenue joins through it.
CUSTOMER_JOIN_FIELDS: dict[str, tuple[str, ...]] = {
    "ads": CANONICAL_FIELDS["ads"],
    "crm": (*CANONICAL_FIELDS["crm"], "customer_id"),
    "revenue": ("transaction_id", "customer_id", "value_aed", "date"),
}
# Fields a file may declare but need not: a row identifier for advertising exports
# that have one, and a line identifier for line-item revenue exports.
OPTIONAL_FIELDS = {"ads": ("row_id",), "crm": (), "revenue": ("line_id",)}
REVENUE_JOINS = ("lead_id", "customer_id")
# How a customer whose accepted leads came from more than one campaign is credited:
# not at all, to the earliest lead, or to the latest lead created by the transaction date.
MULTIPLE_CAMPAIGN_RULES = ("unattributed", "first_touch", "last_touch")
# reject: a negative amount is an error. negative_values: "-500.00" or "(500.00)" is a
# refund or credit note. whole_file: every row of the file is a refund written as a
# positive amount (a separate credit-note export).
REFUND_MODES = ("reject", "negative_values", "whole_file")
CURRENCY_CODE = re.compile(r"[A-Z]{3}")
# Excel writes ; in many locales and some systems export tabs; legacy systems are
# rarely UTF-8. Each is declared, never sniffed.
DELIMITERS = {"comma": ",", "semicolon": ";", "tab": "\t"}
ENCODINGS = {"utf-8": "utf-8-sig", "utf-16": "utf-16", "windows-1252": "cp1252", "latin-1": "latin-1"}
MAX_INTEGER_DIGITS = 15
# A total an export writes in parts, such as a line quantity and its unit price.
VALUE_OPERATIONS = ("multiply", "add")
MAX_TIME_SHIFT_HOURS = 14
# Only descriptive labels may be declared once for a whole file (a Meta export has no
# channel column). Identifiers, dates and amounts must come from the rows themselves.
FIXABLE_FIELDS = {"ads": ("channel",), "crm": ("status",), "revenue": ()}
DATA_ORIGINS = ("synthetic", "public", "client")
CONFIG_KEYS = frozenset(
    {
        "config_version", "engagement", "data_origin", "attribution_windows_days", "channel_aliases", "revenue_join",
        "multiple_campaigns", "coverage_threshold_percent", "reporting_time_zone", "sources",
    }
)
FILE_KEYS = frozenset(
    {
        "file", "columns", "fixed", "date_format", "amounts", "uppercase", "include_when", "delimiter", "encoding",
        "time_zone_shift_hours", "header_row", "time_zone", "lookup",
    }
)
MAX_FILES_PER_SOURCE = 20
MAX_WINDOWS = 5

# How a written date is read. A config declares the shape a file uses, built from these
# parts, and every shape is converted to the one calendar date the report works in. The
# declaration is what makes 03/07/2026 a single date rather than both 3 July and 7 March:
# the tool reads many shapes, but it never decides between two readings of one value.
MONTH_WORDS: dict[int, tuple[str, ...]] = {
    1: ("january", "janvier", "januar", "enero", "janeiro", "gennaio", "januari", "\u064a\u0646\u0627\u064a\u0631"),
    2: ("february", "f\u00e9vrier", "fevrier", "februar", "febrero", "fevereiro", "febbraio", "februari",
        "\u0641\u0628\u0631\u0627\u064a\u0631"),
    3: ("march", "mars", "m\u00e4rz", "marz", "maerz", "marzo", "mar\u00e7o", "marco", "maart", "mrt",
        "\u0645\u0627\u0631\u0633"),
    4: ("april", "avril", "abril", "aprile", "\u0623\u0628\u0631\u064a\u0644", "\u0627\u0628\u0631\u064a\u0644"),
    5: ("may", "mai", "mayo", "maio", "maggio", "mei", "\u0645\u0627\u064a\u0648"),
    6: ("june", "juin", "juni", "junio", "junho", "giugno", "\u064a\u0648\u0646\u064a\u0648"),
    7: ("july", "juillet", "juli", "julio", "julho", "luglio", "\u064a\u0648\u0644\u064a\u0648"),
    8: ("august", "ao\u00fbt", "aout", "agosto", "augustus", "\u0623\u063a\u0633\u0637\u0633",
        "\u0627\u063a\u0633\u0637\u0633"),
    9: ("september", "septembre", "septiembre", "setiembre", "setembro", "settembre",
        "\u0633\u0628\u062a\u0645\u0628\u0631"),
    10: ("october", "octobre", "oktober", "octubre", "outubro", "ottobre", "\u0623\u0643\u062a\u0648\u0628\u0631",
         "\u0627\u0643\u062a\u0648\u0628\u0631"),
    11: ("november", "novembre", "noviembre", "novembro", "\u0646\u0648\u0641\u0645\u0628\u0631"),
    12: ("december", "d\u00e9cembre", "decembre", "dezember", "diciembre", "dezembro", "dicembre",
         "\u062f\u064a\u0633\u0645\u0628\u0631"),
}
# Full names and their 3- and 4-letter openings. A spelling two months share, such as
# the French "jui" of juin and juillet, is dropped: it is refused, never guessed.
MONTH_NUMBERS: dict[str, int] = {}
_shared: set[str] = set()
for _number, _spellings in MONTH_WORDS.items():
    for _spelling in _spellings:
        for _key in {_spelling, _spelling[:3], _spelling[:4]}:
            if len(_key) >= 3 and MONTH_NUMBERS.setdefault(_key, _number) != _number:
                _shared.add(_key)
for _key in _shared:
    MONTH_NUMBERS.pop(_key, None)
MONTH_NUMBERS["sept"] = 9
# Two-digit years: 69 to 99 are the 1900s, 00 to 68 are the 2000s, as POSIX has it.
CENTURY_BREAK = 69
_DATE_TOKENS = {
    "YYYY": r"(?P<y>[0-9]{4})",
    "YY": r"(?P<yy>[0-9]{2})",
    "MMMM": r"(?P<name>[^\W\d_]{3,12})",
    "MMM": r"(?P<name>[^\W\d_]{3,12})",
    "MM": r"(?P<m>[0-9]{1,2})",
    "M": r"(?P<m>[0-9]{1,2})",
    "DD": r"(?P<d>[0-9]{1,2})",
    "D": r"(?P<d>[0-9]{1,2})",
}
_TIME_TOKENS = {
    "HH": r"(?P<H>[0-9]{1,2})",
    "H": r"(?P<H>[0-9]{1,2})",
    "MM": r"(?P<M>[0-9]{2})",
    "SS": r"(?P<S>[0-9]{2})",
    "AM": r"(?P<half>[AaPp]\.?[Mm]\.?)",
}
FORMAT_SEPARATORS = "-/., :"
# A value may state its own offset from UTC; then the instant, not the text, is converted.
_ZONE = r"(?:\s?(?P<zone>Z|z|[+-][0-9]{2}:?[0-9]{2}))?"
_COMPILED_FORMATS: dict[str, re.Pattern[str] | None] = {}


def _split_format(name: str) -> tuple[str, str, str]:
    """The day part, what joins it to the time, and the time part."""
    for index, character in enumerate(name):
        if character in ("T", " ") and name[index + 1: index + 2] == "H":
            return name[:index], character, name[index + 1:]
    return name, "", ""


def _format_pieces(part: str, tokens: dict[str, str]) -> list[tuple[str, str]] | None:
    pieces: list[tuple[str, str]] = []
    index = 0
    while index < len(part):
        for token in sorted(tokens, key=len, reverse=True):
            if part.startswith(token, index):
                pieces.append((token, tokens[token]))
                index += len(token)
                break
        else:
            if part[index] not in FORMAT_SEPARATORS:
                return None
            pieces.append(("", re.escape(part[index])))
            index += 1
    return pieces


def compile_date_format(name: str) -> re.Pattern[str] | None:
    """One pattern for a written format such as 'DD-MMM-YYYY HH:MM', or None if it is not one."""
    if name in _COMPILED_FORMATS:
        return _COMPILED_FORMATS[name]
    compiled: re.Pattern[str] | None = None
    day_part, joiner, time_part = _split_format(name)
    day_pieces = _format_pieces(day_part, _DATE_TOKENS) if day_part else None
    if day_pieces is not None:
        kinds = [token for token, _ in day_pieces if token]
        counts = [sum(1 for kind in kinds if kind.startswith(letter)) for letter in "YMD"]
        if len(kinds) == 3 and counts == [1, 1, 1]:
            pattern = "".join(piece for _, piece in day_pieces)
            time_pieces = _format_pieces(time_part, _TIME_TOKENS) if time_part else []
            names = [token for token, _ in (time_pieces or []) if token]
            if time_part and (time_pieces is None or names[:2] not in (["HH", "MM"], ["H", "MM"])):
                pattern = ""
            elif time_part:
                pattern += re.escape(joiner) + "".join(piece for _, piece in time_pieces)
                if "SS" in names:
                    pattern += r"(?:\.[0-9]+)?"
            if pattern:
                compiled = re.compile(pattern + _ZONE)
    _COMPILED_FORMATS[name] = compiled
    return compiled


def numeric_order(name: str) -> str:
    """Whether a numeric format writes the day or the month first; '' when it names the month."""
    day_part, _joiner, _time = _split_format(name)
    pieces = _format_pieces(day_part, _DATE_TOKENS)
    kinds = [token for token, _ in pieces or [] if token]
    # A named month reads one way, and a year-first shape leaves no position where a day
    # and a month could trade places, so neither can be read two ways.
    if any(kind.startswith("MMM") for kind in kinds) or (kinds and kinds[0].startswith("Y")):
        return ""
    for kind in kinds:
        if kind.startswith("D"):
            return "day-first"
        if kind.startswith("M"):
            return "month-first"
    return ""


# Shapes common enough to try when reading an export to draft a config. A config may
# declare any shape these parts can build, not only the ones listed here.
DATE_FORMATS: dict[str, re.Pattern[str]] = {}
for _day in ("YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "DD-MM-YYYY", "MM-DD-YYYY", "YYYY/MM/DD",
             "DD.MM.YYYY", "D MMM YYYY", "MMM D, YYYY", "DD-MMM-YYYY", "DD MMMM YYYY",
             "DD/MM/YY", "MM/DD/YY", "DD-MMM-YY"):
    for _time in ("", " HH:MM", " HH:MM:SS", "THH:MM:SS", " HH:MM AM", " HH:MM:SS AM"):
        _pattern = compile_date_format(f"{_day}{_time}")
        if _pattern is not None:
            DATE_FORMATS[f"{_day}{_time}"] = _pattern
GROUPED_DECIMAL = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?")
# Excel in many locales writes 1.234,56 where others write 1,234.56.
GROUPED_COMMA_DECIMAL = re.compile(r"[0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]+)?|[0-9]+(?:,[0-9]+)?")
PLAIN_COMMA_DECIMAL = re.compile(r"[0-9]+(?:,[0-9]+)?")
# The grouping mark that belongs with each decimal mark.
DECIMAL_MARKS = {"point": ",", "comma": "."}
MAX_HEADER_ROW = 1000


def named_zone(name: str) -> ZoneInfo | None:
    """The zone with this IANA name, or None when there is no such zone here."""
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


def _century(short_year: int) -> int:
    return 1900 + short_year if short_year >= CENTURY_BREAK else 2000 + short_year


def _half_of_day(hour: int, half: str) -> int:
    """12-hour clock to 24-hour: 12am is 0, 12pm is 12."""
    if not 1 <= hour <= 12:
        raise ValueError("hour is not on a 12-hour clock")
    if half[0] in "Pp":
        return hour if hour == 12 else hour + 12
    return 0 if hour == 12 else hour


def _ref(source: str, source_file: str, source_row: int) -> str:
    """crm:14 under the three-file contract; crm/hubspot_contacts.csv:14 in an engagement."""
    return f"{source}/{source_file}:{source_row}" if source_file else f"{source}:{source_row}"


@dataclass(frozen=True)
class FileSpec:
    """How to read one export: which column holds each field and how its values are written.

    A spec without a label is the plain three-file contract: canonical column names,
    YYYY-MM-DD dates and plain decimals, with the contract's original messages.
    """

    source: str
    path: Path
    label: str
    columns: dict[str, str]
    fixed: dict[str, str]
    date_formats: tuple[str, ...] = ("YYYY-MM-DD",)
    thousands_separator: str = ""
    # "point" reads 1,234.56; "comma" reads 1.234,56.
    decimal: str = "point"
    currency_label: str = ""
    uppercase: frozenset[str] = frozenset()
    # Fields this file must supply; empty means the three-file contract's fields.
    fields: tuple[str, ...] = ()
    delimiter: str = "comma"
    encoding: str = "utf-8"
    # The physical line the header sits on; ad platforms print a title above it.
    header_row: int = 1
    # The zone this file's timestamps are written in, and the zone dates are stated in.
    time_zone: str = ""
    reporting_time_zone: str = ""
    # A second export that carries fields this one does not, matched on a shared key.
    lookup_path: Path | None = None
    lookup_match: tuple[str, str] = ("", "")
    lookup_columns: dict[str, str] = field(default_factory=dict)
    filter_column: str = ""
    filter_values: tuple[str, ...] = ()
    # Amounts are written in this currency and converted at the declared rate.
    currency: str = "AED"
    aed_per_unit: str = "1"
    rate_source: str = ""
    refunds: str = "reject"
    # A value the export writes in parts: ("multiply", ("Quantity", "Unit price")).
    value_from: tuple[str, tuple[str, str]] | None = None
    # A currency written per row, with a declared rate for each code.
    currency_column: str = ""
    currency_rates: dict[str, str] = field(default_factory=dict)
    time_shift_hours: int = 0

    @property
    def date_format(self) -> str:
        return " or ".join(self.date_formats)

    @property
    def extra_headers(self) -> tuple[str, ...]:
        """Columns the file must also carry: a filter, a currency, or the parts of a value."""
        extra = [self.filter_column, self.currency_column, *(self.value_from[1] if self.value_from else ())]
        return tuple(header for header in extra if header)

    @property
    def name(self) -> str:
        return self.label or self.path.name

    @property
    def required(self) -> tuple[str, ...]:
        return self.fields or CANONICAL_FIELDS[self.source]

    def included(self, row: dict[str, Any]) -> bool:
        """False when a declared filter excludes the row, such as an unpaid invoice."""
        if not self.filter_column:
            return True
        return _text(row, self.filter_column).casefold() in self.filter_values

    def read_text(self, row: dict[str, Any], field: str) -> str:
        value = self.fixed[field] if field in self.fixed else _text(row, self.columns[field])
        return value.upper() if field in self.uppercase else value

    def read_date(self, row: dict[str, Any], field: str) -> date:
        if not self.label:
            return _parse_date(row.get(self.columns[field]), field)
        text = self.read_text(row, field)
        for name in self.date_formats:
            pattern = compile_date_format(name)
            match = pattern.fullmatch(text) if pattern else None
            if match is None:
                continue
            found = match.groupdict()
            try:
                year = int(found["y"]) if found.get("y") else _century(int(found["yy"]))
                month = MONTH_NUMBERS[found["name"].lower()] if found.get("name") else int(found["m"])
                hour = int(found.get("H") or 0)
                if found.get("half"):
                    hour = _half_of_day(hour, found["half"])
                moment = datetime(year, month, int(found["d"]), hour, int(found.get("M") or 0))
            except (ValueError, KeyError):
                continue
            return self._calendar_date(moment, found.get("zone"))
        raise ValueError(f"{field} does not match the declared format {self.date_format}")

    def _calendar_date(self, moment: datetime, zone: str | None) -> date:
        """The day this value belongs to, once its offset, its zone and any shift are applied.

        A value stating its own offset is an instant; a file declaring the zone its
        timestamps are written in makes its values instants too. Either way the instant
        is stated in the engagement's reporting zone before the day is taken, so a late
        evening in Sao Paulo is not silently the next morning in the report.
        """
        if zone:
            if zone in ("Z", "z"):
                moment = moment.replace(tzinfo=timezone.utc)
            else:
                digits = zone.replace(":", "")
                minutes = int(digits[1:3]) * 60 + int(digits[3:5])
                moment = moment.replace(tzinfo=timezone(timedelta(minutes=-minutes if zone[0] == "-" else minutes)))
        elif self.time_zone:
            moment = moment.replace(tzinfo=ZoneInfo(self.time_zone))
        if moment.tzinfo is not None:
            moment = moment.astimezone(ZoneInfo(self.reporting_time_zone or "UTC")).replace(tzinfo=None)
        return (moment + timedelta(hours=self.time_shift_hours)).date() if self.time_shift_hours else moment.date()

    def _decimal(self, text: str, field: str) -> Decimal:
        """One written amount, honouring the declared label, separator and sign."""
        if self.currency_label:
            if text.startswith(self.currency_label):
                text = text[len(self.currency_label):].lstrip()
            elif text.endswith(self.currency_label):
                text = text[: -len(self.currency_label)].rstrip()
        negative = False
        if len(text) >= 3 and text[0] == "(" and text[-1] == ")":
            text, negative = text[1:-1].strip(), True
        elif text.startswith("-"):
            text, negative = text[1:].lstrip(), True
        if self.decimal == "comma":
            pattern = GROUPED_COMMA_DECIMAL if self.thousands_separator else PLAIN_COMMA_DECIMAL
            if not pattern.fullmatch(text):
                raise ValueError(f"{field} is not written in the declared format")
            text = text.replace(self.thousands_separator, "") if self.thousands_separator else text
            text = text.replace(",", ".")
        elif self.thousands_separator:
            if not GROUPED_DECIMAL.fullmatch(text):
                raise ValueError(f"{field} is not written in the declared format")
            text = text.replace(self.thousands_separator, "")
        if not PLAIN_DECIMAL.fullmatch(text):
            raise ValueError(f"{field} is not written in the declared format")
        if len(text.split(".")[0]) > MAX_INTEGER_DIGITS:
            raise ValueError(f"{field} is too large to represent exactly")
        value = Decimal(text)
        return -value if negative else value

    def _rate(self, row: dict[str, Any], field: str) -> Decimal:
        if not self.currency_column:
            return Decimal(self.aed_per_unit)
        code = _text(row, self.currency_column).upper()
        rate = self.currency_rates.get(code)
        if rate is None:
            raise ValueError(f"{field} is in {code or 'an unnamed'} currency, which the config declares no rate for")
        return Decimal(rate)

    def read_amount(self, row: dict[str, Any], field: str) -> Decimal:
        if not self.label:
            return _parse_money(row.get(self.columns[field]), field)
        problem = f"{field} is not an amount in the declared format ({self.amount_format})"
        try:
            if self.value_from:
                operation, columns = self.value_from
                first, second = (self._decimal(_text(row, column), field) for column in columns)
                raw = first * second if operation == "multiply" else first + second
            else:
                raw = self._decimal(self.read_text(row, field), field)
        except ValueError as exc:
            if "too large" in str(exc):
                raise
            raise ValueError(problem) from exc
        if raw < 0 and self.refunds != "negative_values":
            raise ValueError(problem)
        if self.refunds == "whole_file":
            raw = -raw
        # Convert the amount as written, then round once to fils.
        value = (raw * self._rate(row, field)).quantize(MONEY, rounding=ROUND_HALF_UP)
        return Decimal("0.00") if value == 0 else value

    @property
    def lookup_fields(self) -> tuple[str, ...]:
        """Fields another export supplies, which this one therefore need not carry."""
        return tuple(self.lookup_columns)

    @property
    def amount_format(self) -> str:
        parts = [
            f"thousands separator '{self.thousands_separator}'" if self.thousands_separator else "no thousands separator"
        ]
        if self.decimal != "point":
            parts.append("decimal comma")
        if self.currency_label:
            parts.append(f"currency label {self.currency_label}")
        if self.value_from:
            parts.append(f"value {self.value_from[0]} of {' and '.join(self.value_from[1])}")
        if self.currency_column:
            rates = ", ".join(f"{code} at {rate}" for code, rate in sorted(self.currency_rates.items()))
            parts.append(f"currency from '{self.currency_column}' ({rates} AED per unit)")
        elif self.currency != "AED":
            parts.append(f"{self.currency} converted at {self.aed_per_unit} AED per unit")
        parts.append(
            {
                "reject": "no negative amounts",
                "negative_values": "negative amounts are refunds",
                "whole_file": "every row is a refund written as a positive amount",
            }[self.refunds]
        )
        return ", ".join(parts)


@dataclass(frozen=True)
class Engagement:
    """Every export to read and every interpretation declared for them."""

    files: dict[str, tuple[FileSpec, ...]]
    windows: tuple[int, ...]
    channel_aliases: dict[str, str] = field(default_factory=dict)
    name: str | None = None
    data_origin: str | None = None
    config_path: Path | None = None
    config_fingerprint: str | None = None
    # How revenue reaches a CRM record: through the lead, or through the customer.
    revenue_join: str = "lead_id"
    multiple_campaigns: str = "unattributed"
    coverage_threshold: str | None = None
    # The zone every date is stated in, once a file says which zone its timestamps are in.
    reporting_time_zone: str = "UTC"

    @property
    def declared(self) -> bool:
        return self.config_path is not None

    @property
    def threshold(self) -> str:
        return self.coverage_threshold or RULESET["coverage_threshold_percent"]

    @property
    def declares_refunds(self) -> bool:
        return any(spec.refunds != "reject" for spec in self.files["revenue"])

    def describe(self) -> dict[str, Any] | None:
        if not self.declared:
            return None
        return {
            "name": self.name,
            "data_origin": self.data_origin,
            "config_fingerprint": self.config_fingerprint,
            "attribution_windows_days": list(self.windows),
            "channel_aliases": dict(sorted(self.channel_aliases.items())),
            "revenue_join": self.revenue_join,
            "multiple_campaigns": self.multiple_campaigns,
            "coverage_threshold_percent": self.threshold,
            "reporting_time_zone": self.reporting_time_zone,
            "files": [
                {
                    "source": spec.source,
                    "file": spec.label,
                    "columns": {
                        name: header for name, header in spec.columns.items()
                        if not header.startswith(LOOKUP_PREFIX)
                    },
                    "fixed": dict(spec.fixed),
                    "date_formats": list(spec.date_formats),
                    "time_zone_shift_hours": spec.time_shift_hours,
                    "value_from": (
                        {"operation": spec.value_from[0], "columns": list(spec.value_from[1])} if spec.value_from else None
                    ),
                    "currency_column": spec.currency_column,
                    "currency_rates": dict(sorted(spec.currency_rates.items())),
                    "delimiter": spec.delimiter,
                    "encoding": spec.encoding,
                    "thousands_separator": spec.thousands_separator,
                    "currency_label": spec.currency_label,
                    "decimal": spec.decimal,
                    "time_zone": spec.time_zone,
                    "lookup": (
                        {
                            "file": spec.lookup_path.name,
                            "match": {spec.lookup_match[0]: spec.lookup_match[1]},
                            "columns": dict(spec.lookup_columns),
                        }
                        if spec.lookup_path
                        else None
                    ),
                    "header_row": spec.header_row,
                    "currency": spec.currency,
                    "aed_per_unit": spec.aed_per_unit,
                    "rate_source": spec.rate_source,
                    "refunds": spec.refunds,
                    "uppercase": sorted(spec.uppercase),
                    "include_when": (
                        {"column": spec.filter_column, "values": list(spec.filter_values)} if spec.filter_column else None
                    ),
                }
                for source in SOURCES
                for spec in self.files[source]
            ],
        }


def three_file_engagement(ads_path: str | Path, crm_path: str | Path, revenue_path: str | Path) -> Engagement:
    paths = {"ads": ads_path, "crm": crm_path, "revenue": revenue_path}
    return Engagement(
        files={
            source: (FileSpec(source, Path(paths[source]), "", {name: name for name in CANONICAL_FIELDS[source]}, {}),)
            for source in SOURCES
        },
        windows=tuple(RULESET["attribution_windows_days"]),
    )


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise ValueError(f"duplicate keys: {', '.join(repeated)}")
    return dict(pairs)


def load_engagement(config_path: str | Path) -> Engagement:
    """Read and strictly validate an engagement config. Nothing in it is guessed."""
    path = Path(config_path)
    try:
        raw = path.read_bytes()
        config = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    except OSError as exc:
        raise InputContractError(f"cannot read {path.name} ({type(exc).__name__})") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise InputContractError(f"{path.name} is not valid JSON ({exc})") from exc

    def fail(message: str) -> None:
        raise InputContractError(f"{path.name}: {message}")

    def is_text(value: Any) -> bool:
        return isinstance(value, str) and bool(value) and value.strip() == value and "\n" not in value

    def is_header(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip()) and "\n" not in value

    if not isinstance(config, dict):
        fail("the config must be a JSON object")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        fail(f"unknown keys: {', '.join(unknown)}")
    if type(config.get("config_version")) is not int or config["config_version"] != 1:
        fail("config_version must be 1")
    if not is_text(config.get("engagement")) or len(config["engagement"]) > 120:
        fail("engagement must be a name of 1 to 120 characters")
    if config.get("data_origin") not in DATA_ORIGINS:
        fail(f"data_origin must be one of {', '.join(DATA_ORIGINS)}")
    windows = config.get("attribution_windows_days", RULESET["attribution_windows_days"])
    if (
        not isinstance(windows, list)
        or not 1 <= len(windows) <= MAX_WINDOWS
        or any(type(window) is not int or not 1 <= window <= 999 for window in windows)
        or windows != sorted(set(windows))
    ):
        fail(f"attribution_windows_days must list 1 to {MAX_WINDOWS} increasing whole days between 1 and 999")
    threshold = config.get("coverage_threshold_percent")
    if threshold is not None and (
        not isinstance(threshold, str) or not PLAIN_DECIMAL.fullmatch(threshold) or not 0 <= Decimal(threshold) <= 100
    ):
        fail('coverage_threshold_percent must be a percentage in quotes, such as "70"')
    reporting_zone = config.get("reporting_time_zone", "UTC")
    if not is_text(reporting_zone) or named_zone(reporting_zone) is None:
        fail('reporting_time_zone must be a zone name such as "Asia/Dubai" or "UTC"')
    aliases = config.get("channel_aliases", {})
    if not isinstance(aliases, dict) or not all(
        is_text(key) and is_text(value) and "[" not in value and "]" not in value for key, value in aliases.items()
    ):
        fail("channel_aliases must map channel names to non-empty names without square brackets")
    folded = {key.casefold(): value for key, value in aliases.items()}
    if len(folded) != len(aliases):
        fail("channel_aliases has names that differ only in letter case")
    join = config.get("revenue_join", "lead_id")
    if join not in REVENUE_JOINS:
        fail("revenue_join must be lead_id or customer_id")
    required_fields = CANONICAL_FIELDS if join == "lead_id" else CUSTOMER_JOIN_FIELDS
    rule = config.get("multiple_campaigns", "unattributed")
    if rule not in MULTIPLE_CAMPAIGN_RULES:
        fail(f"multiple_campaigns must be one of {', '.join(MULTIPLE_CAMPAIGN_RULES)}")
    if rule != "unattributed" and join != "customer_id":
        fail("multiple_campaigns applies only with revenue_join customer_id; each lead has one campaign")
    sources = config.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(SOURCES):
        fail("sources must declare exactly ads, crm and revenue")

    files: dict[str, tuple[FileSpec, ...]] = {}
    for source in SOURCES:
        fields = required_fields[source]
        entries = sources[source]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES_PER_SOURCE:
            fail(f"sources.{source} must list 1 to {MAX_FILES_PER_SOURCE} files")
        specs: list[FileSpec] = []
        for index, entry in enumerate(entries):
            where = f"sources.{source}[{index}]"
            if not isinstance(entry, dict):
                fail(f"{where} must be an object")
            extra = sorted(set(entry) - FILE_KEYS)
            if extra:
                fail(f"{where} has unknown keys: {', '.join(extra)}")
            label = entry.get("file")
            if (
                not is_text(label)
                or any(mark in label for mark in ":\\|`")
                or label.startswith("/")
                or any(part in ("", ".", "..") for part in label.split("/"))
            ):
                fail(f"{where}.file must be a relative path inside the config's folder without ':', '\\', '|' or '`'")
            if any(spec.label == label for spec in specs):
                fail(f"{where}.file lists {label} twice")
            file_path = path.parent / label
            if not file_path.is_file():
                fail(f"{where}.file {label} does not exist next to the config")
            columns = entry.get("columns", {})
            fixed = entry.get("fixed", {})
            if not isinstance(columns, dict) or not all(is_header(value) for value in columns.values()):
                fail(f"{where}.columns must map fields to column names")
            if not isinstance(fixed, dict) or not all(
                is_text(value) and "[" not in value and "]" not in value for value in fixed.values()
            ):
                fail(f"{where}.fixed must map fields to non-empty values without square brackets")
            wrong = sorted(
                (set(columns) - set(fields) - set(OPTIONAL_FIELDS[source])) | (set(fixed) - set(FIXABLE_FIELDS[source]))
            )
            if wrong:
                fail(f"{where} cannot declare {', '.join(wrong)} for the {source} export with revenue_join {join}")
            declaring = entry.get("lookup")
            lookup_fields = (
                tuple(declaring["columns"])
                if isinstance(declaring, dict) and isinstance(declaring.get("columns"), dict)
                else ()
            )
            computed = (
                set(AMOUNT_FIELDS[source])
                if isinstance(entry.get("amounts"), dict) and entry["amounts"].get("value_from")
                else set()
            )
            unclear = sorted(set(columns) & set(fixed)) + [
                name for name in fields
                if name not in columns and name not in fixed and name not in computed and name not in lookup_fields
            ]
            if unclear:
                fail(f"{where} must give each field exactly one column or fixed value: {', '.join(unclear)}")
            if len(set(columns.values())) != len(columns):
                fail(f"{where}.columns maps two fields to the same column")
            delimiter = entry.get("delimiter", "comma")
            if delimiter not in DELIMITERS:
                fail(f"{where}.delimiter must be one of {', '.join(DELIMITERS)}")
            encoding = entry.get("encoding", "utf-8")
            if encoding not in ENCODINGS:
                fail(f"{where}.encoding must be one of {', '.join(ENCODINGS)}")
            declared_lookup = entry.get("lookup")
            lookup_path, lookup_match, lookup_columns = None, ("", ""), {}
            if declared_lookup is not None:
                match = declared_lookup.get("match") if isinstance(declared_lookup, dict) else None
                taken = declared_lookup.get("columns") if isinstance(declared_lookup, dict) else None
                if (
                    not isinstance(declared_lookup, dict)
                    or set(declared_lookup) != {"file", "match", "columns"}
                    or not is_text(declared_lookup.get("file"))
                    or any(mark in declared_lookup["file"] for mark in ":\\|`")
                    or not isinstance(match, dict) or len(match) != 1
                    or not all(is_header(key) and is_header(value) for key, value in match.items())
                    or not isinstance(taken, dict) or not taken
                    or not all(is_header(value) for value in taken.values())
                    or set(taken) - set(fields)
                ):
                    fail(f'{where}.lookup must be {{"file": "deals.csv", "match": {{"their_key": "my_key"}}, '
                         f'"columns": {{"{fields[0]}": "their_column"}}}} naming fields this export may declare')
                lookup_path = (path.parent / declared_lookup["file"]).resolve()
                if not lookup_path.is_file():
                    fail(f"{where}.lookup.file {declared_lookup['file']} does not exist next to the config")
                if set(taken) & (set(columns) | set(fixed)):
                    fail(f"{where}.lookup cannot supply a field this export already declares: "
                         f"{', '.join(sorted(set(taken) & (set(columns) | set(fixed))))}")
                lookup_match = next(iter(match.items()))
                lookup_columns = dict(taken)
            header_row = entry.get("header_row", 1)
            if not isinstance(header_row, int) or isinstance(header_row, bool) or not 1 <= header_row <= MAX_HEADER_ROW:
                fail(f"{where}.header_row must be a whole number from 1 to {MAX_HEADER_ROW}: the line the column "
                     "names sit on, when the export prints a title above them")
            declared_dates = entry.get("date_format", "YYYY-MM-DD")
            declared_dates = [declared_dates] if isinstance(declared_dates, str) else declared_dates
            if (
                not isinstance(declared_dates, list)
                or not 1 <= len(declared_dates) <= 4
                or len(set(declared_dates)) != len(declared_dates)
                or any(
                    not isinstance(name, str) or len(name) > 40 or compile_date_format(name) is None
                    for name in declared_dates
                )
            ):
                fail(f"{where}.date_format is written from YYYY, YY, MMMM, MMM, MM, M, DD and D with the "
                     "separators - / . and space, and an optional time such as ' HH:MM', ' HH:MM:SS', "
                     "'THH:MM:SS' or ' HH:MM AM'; a list declares several. Examples: "
                     f"{', '.join(list(DATE_FORMATS)[:3])}")
            if {"day-first", "month-first"} <= {numeric_order(name) for name in declared_dates}:
                fail(f"{where}.date_format cannot declare both a day-first and a month-first numeric shape: "
                     "03/07/2026 would be two dates")
            shift = entry.get("time_zone_shift_hours", 0)
            if type(shift) is not int or not -MAX_TIME_SHIFT_HOURS <= shift <= MAX_TIME_SHIFT_HOURS:
                fail(f"{where}.time_zone_shift_hours must be whole hours between -{MAX_TIME_SHIFT_HOURS} and {MAX_TIME_SHIFT_HOURS}")
            zone_name = entry.get("time_zone", "")
            if zone_name and (not is_text(zone_name) or named_zone(zone_name) is None):
                fail(f'{where}.time_zone must be a zone name such as "America/Sao_Paulo"')
            if zone_name and shift:
                fail(f"{where} declares both time_zone and time_zone_shift_hours; declare one of them")
            if zone_name and any("(?P<H>" not in compile_date_format(name).pattern for name in declared_dates):
                fail(f"{where}.time_zone needs every declared date_format to include a time")
            if shift and any("(?P<H>" not in compile_date_format(name).pattern for name in declared_dates):
                fail(f"{where}.time_zone_shift_hours needs every declared date_format to include a time")
            amounts = entry.get("amounts", {})
            if not isinstance(amounts, dict) or set(amounts) - {
                "thousands_separator", "decimal", "currency_label", "currency", "refunds", "value_from"
            }:
                fail(f"{where}.amounts may declare only thousands_separator, decimal, currency_label, currency, "
                     "refunds and value_from")
            if amounts and not AMOUNT_FIELDS[source]:
                fail(f"{where}.amounts is not allowed: the {source} export has no amounts")
            decimal_mark = amounts.get("decimal", "point")
            if decimal_mark not in DECIMAL_MARKS:
                fail(f'{where}.amounts.decimal must be "point" for 1,234.56 or "comma" for 1.234,56')
            separator = amounts.get("thousands_separator", "")
            if separator not in ("", DECIMAL_MARKS[decimal_mark]):
                fail(f"{where}.amounts.thousands_separator must be '{DECIMAL_MARKS[decimal_mark]}' "
                     f'with decimal "{decimal_mark}"')
            declared_value = amounts.get("value_from")
            parsed_value_from = None
            if declared_value is not None:
                named = list(declared_value.values())[0] if isinstance(declared_value, dict) and len(declared_value) == 1 else None
                if (
                    not isinstance(declared_value, dict)
                    or set(declared_value) - set(VALUE_OPERATIONS)
                    or not isinstance(named, list)
                    or len(named) != 2
                    or not all(is_header(column) for column in named)
                ):
                    fail(f'{where}.amounts.value_from must be {{"multiply": ["Quantity", "Unit price"]}} or {{"add": [...]}}')
                parsed_value_from = (next(iter(declared_value)), tuple(named))
            currency = amounts.get("currency", {})
            if not isinstance(currency, dict) or set(currency) - {"code", "aed_per_unit", "rate_source", "column", "rates"}:
                fail(f"{where}.amounts.currency may declare only code, aed_per_unit, rate_source, column and rates")
            code = currency.get("code", "AED")
            rate = currency.get("aed_per_unit", "1")
            rate_source = currency.get("rate_source", "")
            currency_column = currency.get("column", "")
            declared_rates = currency.get("rates", {})
            rates: dict[str, str] = {}
            # A currency written per row declares a rate for each code, so the single-code rules do not apply.
            per_row = bool(currency_column or declared_rates)
            if not per_row and (not isinstance(code, str) or not CURRENCY_CODE.fullmatch(code)):
                fail(f"{where}.amounts.currency.code must be a three-letter code such as USD")
            if not per_row and code == "AED" and (rate != "1" or rate_source):
                fail(f"{where}.amounts.currency: AED amounts take no conversion rate")
            if not per_row and code != "AED" and (
                not isinstance(rate, str)
                or not PLAIN_DECIMAL.fullmatch(rate)
                or Decimal(rate) <= 0
                or len(rate.split(".")[0]) > 6
                or not is_text(rate_source)
                or len(rate_source) > 200
            ):
                fail(
                    f"{where}.amounts.currency needs aed_per_unit as a positive decimal in quotes, such as \"3.6725\", "
                    "and a rate_source saying where the rate comes from"
                )
            if per_row:
                if "code" in currency or "aed_per_unit" in currency:
                    fail(f"{where}.amounts.currency declares either one code with its rate, or a column with rates")
                if (
                    not is_header(currency_column)
                    or not isinstance(declared_rates, dict)
                    or not declared_rates
                    or not all(
                        isinstance(name, str) and CURRENCY_CODE.fullmatch(name) and isinstance(value, str)
                        and PLAIN_DECIMAL.fullmatch(value) and Decimal(value) > 0 and len(value.split(".")[0]) <= 6
                        for name, value in declared_rates.items()
                    )
                    or not is_text(rate_source)
                    or len(rate_source) > 200
                ):
                    fail(
                        f'{where}.amounts.currency needs {{"column": "Currency", "rates": {{"USD": "3.6725"}}, '
                        '"rate_source": "where the rates come from"}'
                    )
                rates, code, rate = dict(declared_rates), "AED", "1"
            currency_label = amounts.get("currency_label", "")
            if currency_column and currency_label:
                fail(f"{where}.amounts.currency_label cannot be used with a currency column")
            if currency_label and (
                not is_text(currency_label)
                or len(currency_label) > 8
                or any(character.isdigit() for character in currency_label)
                or any(mark in currency_label for mark in "[]")
            ):
                fail(f'{where}.amounts.currency_label must be the text written beside the number, such as "AED", '
                     '"$" or "R$": up to 8 characters and no digits')
            refunds = amounts.get("refunds", "reject")
            if refunds not in REFUND_MODES or (refunds != "reject" and source != "revenue"):
                fail(f"{where}.amounts.refunds must be one of {', '.join(REFUND_MODES)}; only revenue exports hold refunds")
            chosen = entry.get("include_when")
            filter_column, filter_values = "", ()
            if chosen is not None:
                if (
                    not isinstance(chosen, dict)
                    or set(chosen) != {"column", "values"}
                    or not is_header(chosen["column"])
                    or not isinstance(chosen["values"], list)
                    or not 1 <= len(chosen["values"]) <= 50
                    or not all(is_text(value) for value in chosen["values"])
                ):
                    fail(f"{where}.include_when needs a column and 1 to 50 values to keep, such as "
                         '{"column": "Status", "values": ["PAID"]}')
                filter_column = chosen["column"]
                filter_values = tuple(sorted({value.casefold() for value in chosen["values"]}))
            identifiers = [name for name in IDENTIFIER_FIELDS[source] if name in fields or name in OPTIONAL_FIELDS[source]]
            uppercase = entry.get("uppercase", [])
            if (
                not isinstance(uppercase, list)
                or not all(isinstance(name, str) for name in uppercase)
                or len(set(uppercase)) != len(uppercase)
                or set(uppercase) - set(identifiers)
            ):
                fail(f"{where}.uppercase may list only {', '.join(identifiers)}")
            specs.append(
                FileSpec(
                    source=source, path=file_path, label=label,
                    columns={
                        **columns,
                        **{name: LOOKUP_PREFIX + header for name, header in lookup_columns.items()},
                    },
                    fixed=dict(fixed),
                    date_formats=tuple(declared_dates), thousands_separator=separator, currency_label=currency_label,
                    decimal=decimal_mark, header_row=header_row,
                    time_zone=zone_name, reporting_time_zone=reporting_zone,
                    lookup_path=lookup_path, lookup_match=lookup_match, lookup_columns=dict(lookup_columns),
                    uppercase=frozenset(uppercase), fields=tuple(fields), delimiter=delimiter, encoding=encoding,
                    filter_column=filter_column, filter_values=filter_values, currency=code, aed_per_unit=rate,
                    rate_source=rate_source, refunds=refunds, value_from=parsed_value_from,
                    currency_column=currency_column, currency_rates=rates, time_shift_hours=shift,
                )
            )
        for optional in OPTIONAL_FIELDS[source]:
            declared = [optional in spec.columns for spec in specs]
            if any(declared) and not all(declared):
                fail(f"sources.{source}: declare {optional} in every file of the source, or in none")
        files[source] = tuple(specs)
    return Engagement(
        files, tuple(windows), folded, config["engagement"], config["data_origin"], path,
        hashlib.sha256(raw).hexdigest(), join, rule, threshold, reporting_zone,
    )


def ensure_outside_repository(paths: Iterable[str | Path], repository: Path) -> None:
    """Client exports and outputs must never sit inside a code checkout, where one `git add` publishes them."""
    if not ((repository / ".git").exists() or (repository / "pyproject.toml").exists()):
        return
    root = repository.resolve()
    for candidate in paths:
        resolved = Path(candidate).resolve()
        if resolved == root or root in resolved.parents:
            raise InputContractError(
                f"client data must stay outside the code repository: {candidate} is inside {root}. "
                "Keep each engagement in its own folder (see docs/engagements.md)."
            )


def _found_columns(fieldnames: list[str]) -> str:
    if not fieldnames:
        return "none"
    shown = ", ".join(f"'{name}'" for name in fieldnames[:12])
    return shown + (f" and {len(fieldnames) - 12} more" if len(fieldnames) > 12 else "")


LOOKUP_PREFIX = "\x00lookup:"


def _fill_from_lookup(spec: FileSpec, rows: list[tuple[int, dict[str, Any]]]) -> None:
    """Copy the declared columns of a second export onto every row that shares its key.

    A row whose key is not in the second export keeps an empty value, so it is refused
    and named by the same rules as a blank cell, never quietly filled.
    """
    their_key, my_key = spec.lookup_match
    reader = replace(
        spec, path=spec.lookup_path, columns={}, fixed={}, fields=(), lookup_path=None, lookup_columns={},
        filter_column="", filter_values=(), currency_column="", value_from=None,
    )
    index: dict[str, dict[str, Any]] = {}
    for _line, row in _read_csv(reader):
        key = _text(row, their_key)
        if key and key not in index:
            index[key] = row
    for _line, row in rows:
        found = index.get(_text(row, my_key), {})
        for field_name, header in spec.lookup_columns.items():
            row[LOOKUP_PREFIX + header] = _text(found, header) if found else ""


def _read_csv(spec: FileSpec) -> list[tuple[int, dict[str, Any]]]:
    """Data rows paired with the physical line on which each record starts.

    Lines count the header as line 1 and include blank lines and the extra lines
    of quoted multi-line fields, so a reference such as crm:14 is the line a
    person opening the file would look at. Counting records instead drifts as
    soon as an export contains a blank line or a multi-line note.
    """
    fieldnames: list[str] | None = None
    rows: list[tuple[int, dict[str, Any]]] = []
    try:
        with spec.path.open("r", encoding=ENCODINGS[spec.encoding], newline="") as handle:
            reader = csv.reader(handle, delimiter=DELIMITERS[spec.delimiter])
            previous_line = 0
            for record in reader:
                start_line = previous_line + 1
                previous_line = reader.line_num
                # An export may print a report title and a date range above its header row.
                if start_line < spec.header_row:
                    continue
                if not record:
                    continue
                if fieldnames is None:
                    fieldnames = record
                    continue
                row: dict[Any, Any] = dict(zip(fieldnames, record))
                if len(record) > len(fieldnames):
                    row[None] = record[len(fieldnames):]
                rows.append((start_line, row))
    except (UnicodeDecodeError, UnicodeError) as exc:
        raise InputContractError(
            f"{spec.name} is not UTF-8 text" if spec.encoding == "utf-8"
            else f"{spec.name} is not {spec.encoding} text as declared"
        ) from exc
    except csv.Error as exc:
        raise InputContractError(f"{spec.name} is not a readable CSV ({exc})") from exc
    if spec.lookup_path is not None:
        _fill_from_lookup(spec, rows)
    found = list(fieldnames or [])
    needed = {
        name: spec.columns[name]
        for name in CANONICAL_FIELDS[spec.source]
        if name in spec.columns and not spec.columns[name].startswith(LOOKUP_PREFIX)
    }
    missing = [name for name, header in needed.items() if header not in found]
    if missing and not spec.label:
        raise InputContractError(
            f"{spec.name} is missing required columns: {', '.join(sorted(missing))}. "
            f"Found columns: {_found_columns(found)}. "
            "Exports with other column names or formats need an engagement config (--config)."
        )
    if missing:
        raise InputContractError(
            f"{spec.name} is missing "
            + ", ".join(f"'{needed[name]}' (mapped to {name})" for name in missing)
            + f". Found columns: {_found_columns(found)}."
        )
    repeated = sorted({header for header in needed.values() if found.count(header) > 1})
    if repeated:
        raise InputContractError(f"{spec.name} has more than one column named {', '.join(repeated)}")
    absent = [header for header in spec.extra_headers if header not in found]
    if absent:
        raise InputContractError(
            f"{spec.name} is missing the declared column {', '.join(repr(header) for header in absent)}. "
            f"Found columns: {_found_columns(found)}."
        )
    return rows


def _claim(
    evidence_id: str,
    metric: str,
    value: str,
    unit: str,
    grade: str,
    explanation: str,
    roles: dict[str, list[str]],
    assumptions: Iterable[str] = (),
    dimensions: dict[str, str] | None = None,
) -> Claim:
    unknown = set(roles) - set(LINEAGE_ROLES)
    if unknown:
        raise ValueError(f"unknown lineage roles: {sorted(unknown)}")
    lineage = [
        {"role": role, "source_ref": ref}
        for role in LINEAGE_ROLES
        for ref in dict.fromkeys(roles.get(role, []))
    ]
    source_refs = list(dict.fromkeys(entry["source_ref"] for entry in lineage))
    return Claim(
        evidence_id, metric, value, unit, grade, explanation, source_refs, list(assumptions), lineage,
        dict(dimensions or {}),
    )


def _dispose(
    dispositions: dict[str, RowDisposition],
    source: str,
    source_row: int,
    record_id: str,
    status: str,
    reason: str = "",
    amount: Decimal | None = None,
    source_file: str = "",
) -> None:
    dispositions[_ref(source, source_file, source_row)] = RowDisposition(
        source, source_row, record_id, status, reason, "" if amount is None else _money(amount), source_file
    )


def _refs(items: Iterable[dict[str, Any]]) -> list[str]:
    """Every source row behind these invoices; one row each unless the export is line-level."""
    return [ref for item in items for ref in item["refs"]]


def _readable_amount(spec: FileSpec, row: dict[str, Any], field: str) -> Decimal | None:
    """A rejected row's amount when it can still be read, so an owner sees what the row holds."""
    try:
        return spec.read_amount(row, field)
    except ValueError:
        return None


def _resolve_identity(
    rows: list[dict[str, Any]],
    key: str,
    signature,
    dispositions: dict[str, RowDisposition],
    source: str,
    duplicate_reason: str,
    conflict_reason,
    amount_field: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Resolve rows sharing an identifier without depending on file order.

    Identical rows are one record: the earliest is kept and the rest are
    duplicates. Rows that share an identifier but disagree are all conflicts —
    choosing one would let the export's row order decide the evidence.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    kept: list[dict[str, Any]] = []
    conflicts: dict[str, list[str]] = {}
    for identifier, members in groups.items():
        members.sort(key=lambda row: row["order"])
        amount = (lambda row: row[amount_field]) if amount_field else (lambda row: None)
        if len({signature(row) for row in members}) > 1:
            reason = conflict_reason(members)
            conflicts[identifier] = [row["source_ref"] for row in members]
            for row in members:
                _dispose(
                    dispositions, source, row["source_row"], identifier, CONFLICT, reason, amount(row),
                    row["source_file"],
                )
            continue
        first, *repeats = members
        kept.append(first)
        _dispose(dispositions, source, first["source_row"], identifier, ACCEPTED, "", amount(first), first["source_file"])
        for row in repeats:
            _dispose(
                dispositions, source, row["source_row"], identifier, DUPLICATE, duplicate_reason, amount(row),
                row["source_file"],
            )
    kept.sort(key=lambda row: row["order"])
    return kept, conflicts


class EvidenceEngine:
    ADS_FIELDS = {"date", "campaign_id", "channel", "spend_aed"}
    CRM_FIELDS = {"lead_id", "campaign_id", "created_at", "status"}
    REVENUE_FIELDS = {"transaction_id", "lead_id", "value_aed", "date"}

    def run(
        self,
        ads_path: str | Path,
        crm_path: str | Path,
        revenue_path: str | Path,
        as_of: str | None = None,
    ) -> EvidenceReport:
        return self.run_engagement(three_file_engagement(ads_path, crm_path, revenue_path), as_of)

    def run_engagement(self, engagement: Engagement, as_of: str | None = None) -> EvidenceReport:
        if as_of is not None:
            try:
                as_of = _parse_date(as_of, "as_of").isoformat()
            except ValueError as exc:
                raise InputContractError(str(exc)) from exc
        raw = {
            source: [
                (spec, index, line, row)
                for index, spec in enumerate(engagement.files[source])
                for line, row in _read_csv(spec)
            ]
            for source in SOURCES
        }
        customer_join = engagement.revenue_join == "customer_id"

        dispositions: dict[str, RowDisposition] = {}
        ads, conflicted_campaigns = self._validate_ads(raw["ads"], dispositions, engagement.channel_aliases)
        crm, conflicting_leads = self._validate_crm(raw["crm"], dispositions, customer_join)
        revenue = self._validate_revenue(raw["revenue"], dispositions, customer_join)

        spend_total = sum((row["spend"] for row in ads), Decimal("0"))
        revenue_total = sum((row["value"] for row in revenue), Decimal("0"))
        # Revenue reaches CRM records through its lead, or through every accepted lead of its customer.
        leads_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in crm:
            leads_by_key[row["customer_id"] if customer_join else row["lead_id"]].append(row)
        campaign_to_channel: dict[str, str] = {}
        spend_by_channel: dict[str, Decimal] = {}
        for row in ads:
            campaign_to_channel[row["campaign_id"]] = row["channel"]
            spend_by_channel[row["channel"]] = spend_by_channel.get(
                row["channel"], Decimal("0")
            ) + row["spend"]

        attributed: list[dict[str, Any]] = []
        unattributed: list[dict[str, Any]] = []
        conflicted_revenue: list[dict[str, Any]] = []
        for row in revenue:
            leads = sorted(leads_by_key.get(row["link_id"], []), key=lambda item: (item["created_at"], item["order"]))
            # All of a customer's accepted leads are join evidence; the declared rule picks the one credited.
            lead = leads[0] if leads else None
            if engagement.multiple_campaigns == "last_touch":
                earlier = [item for item in leads if item["created_at"] <= row["date"]]
                lead = earlier[-1] if earlier else lead
            single_campaign = engagement.multiple_campaigns != "unattributed" or len({item["campaign_id"] for item in leads}) == 1
            channel = campaign_to_channel.get(lead["campaign_id"]) if lead is not None and single_campaign else None
            if lead is None and row["link_id"] in conflicting_leads:
                reason = REASON_CONFLICTING_CUSTOMER if customer_join else REASON_CONFLICTING_LEAD
                conflicted_revenue.append(row)
            elif lead is None:
                reason = REASON_NO_CUSTOMER if customer_join else REASON_NO_LEAD
            elif not single_campaign:
                reason = REASON_CUSTOMER_CAMPAIGNS
            elif channel is None and lead["campaign_id"] in conflicted_campaigns:
                reason = REASON_CONFLICTING_CAMPAIGN
            elif channel is None:
                reason = REASON_NO_CAMPAIGN
            elif (row["date"] - lead["created_at"]).days < 0:
                reason = REASON_CHRONOLOGY
            else:
                reason = ""
            if reason:
                unattributed.append(row)
                for line in row["rows"]:
                    _dispose(
                        dispositions, "revenue", line["source_row"], row["transaction_id"],
                        ACCEPTED_UNATTRIBUTED, reason, line["value"], line["source_file"],
                    )
                continue
            attributed.append(
                {
                    **row,
                    "campaign_id": lead["campaign_id"],
                    "channel": channel,
                    "delay_days": (row["date"] - lead["created_at"]).days,
                    "crm_refs": [item["source_ref"] for item in leads],
                }
            )

        attributed_total = sum((row["value"] for row in attributed), Decimal("0"))
        unattributed_total = sum((row["value"] for row in unattributed), Decimal("0"))
        conflicted_total = sum((row["value"] for row in conflicted_revenue), Decimal("0"))
        coverage = Decimal("0") if revenue_total == 0 else attributed_total / revenue_total

        attribution_assumption = ["CRM campaign is treated as the governing source attribution."]
        if customer_join:
            attribution_assumption.append("Revenue is joined to CRM leads through customer_id.")
        if engagement.multiple_campaigns == "first_touch":
            attribution_assumption.append("A customer with leads from several campaigns is credited to the earliest lead.")
        elif engagement.multiple_campaigns == "last_touch":
            attribution_assumption.append(
                "A customer with leads from several campaigns is credited to the latest lead created by the transaction date."
            )
        channel_rows: list[dict[str, Any]] = []
        channel_claims: list[Claim] = []
        channels = sorted(spend_by_channel)
        if len(channels) > 999:
            raise InputContractError("more than 999 channels cannot be given evidence IDs")
        # IDs are numbered by sorted channel name, so they do not depend on row order.
        for index, channel in enumerate(channels, start=1):
            ids = {
                "spend": f"EV-CHSPEND-{index:03d}",
                "attributed_revenue": f"EV-CHATTR-{index:03d}",
                "roas": f"EV-CHROAS-{index:03d}",
            }
            channel_ads = [row["source_ref"] for row in ads if row["channel"] == channel]
            channel_attributed = [row for row in attributed if row["channel"] == channel]
            channel_revenue_refs = _refs(channel_attributed)
            channel_joins = [ref for row in channel_attributed for ref in row["crm_refs"]]
            channel_revenue = sum((row["value"] for row in channel_attributed), Decimal("0"))
            spend = spend_by_channel[channel]
            roas = Decimal("0") if spend == 0 else channel_revenue / spend
            roas_value = str(roas.quantize(Decimal("0.01")))
            channel_rows.append(
                {
                    "channel": channel,
                    "spend_aed": _money(spend),
                    "attributed_revenue_aed": _money(channel_revenue),
                    "assumption_dependent_roas": roas_value,
                    "evidence_grade": "assumption-dependent",
                    "evidence_ids": ids,
                }
            )
            dimensions = {"channel": channel}
            channel_claims.extend(
                [
                    _claim(
                        ids["spend"], "channel_accepted_ad_spend", _money(spend), "AED", "reconciled",
                        f"Sum of accepted advertising rows for the {channel} channel.",
                        {"summand": channel_ads}, dimensions=dimensions,
                    ),
                    _claim(
                        ids["attributed_revenue"], "channel_attributed_revenue", _money(channel_revenue),
                        "AED", "assumption-dependent",
                        f"Attributed revenue whose CRM campaign maps to the {channel} channel.",
                        {"summand": channel_revenue_refs, "join": channel_joins},
                        attribution_assumption, dimensions,
                    ),
                    _claim(
                        ids["roas"], "channel_assumption_dependent_roas", roas_value, "ratio",
                        "assumption-dependent",
                        f"Attributed revenue (numerator rows) divided by accepted spend (denominator rows) "
                        f"for the {channel} channel; not a causal return.",
                        {"numerator": channel_revenue_refs, "denominator": channel_ads, "join": channel_joins},
                        attribution_assumption, dimensions,
                    ),
                ]
            )

        sensitivity: list[dict[str, Any]] = []
        window_claims: list[Claim] = []
        for window in engagement.windows:
            ids = {"attributed": f"EV-WINATTR-{window:03d}", "excluded": f"EV-WINEXCL-{window:03d}"}
            inside = [row for row in attributed if row["delay_days"] <= window]
            outside = [row for row in attributed if row["delay_days"] > window]
            within = sum((row["value"] for row in inside), Decimal("0"))
            sensitivity.append(
                {
                    "attribution_window_days": window,
                    "attributed_revenue_aed": _money(within),
                    "excluded_delayed_revenue_aed": _money(attributed_total - within),
                    "evidence_ids": ids,
                }
            )
            dimensions = {"window_days": str(window)}
            window_claims.extend(
                [
                    _claim(
                        ids["attributed"], "window_attributed_revenue", _money(within), "AED",
                        "assumption-dependent",
                        f"Attributed revenue received within {window} days of lead creation.",
                        {"summand": _refs(inside), "join": [ref for row in inside for ref in row["crm_refs"]]},
                        attribution_assumption, dimensions,
                    ),
                    _claim(
                        ids["excluded"], "window_excluded_delayed_revenue", _money(attributed_total - within),
                        "AED", "assumption-dependent",
                        f"Attributed revenue received more than {window} days after lead creation.",
                        {"summand": _refs(outside), "join": [ref for row in outside for ref in row["crm_refs"]]},
                        attribution_assumption, dimensions,
                    ),
                ]
            )

        ad_refs = [row["source_ref"] for row in ads]
        revenue_refs = _refs(revenue)
        attributed_refs = _refs(attributed)
        attribution_joins = [ref for row in attributed for ref in row["crm_refs"]]
        claims = [
            _claim(
                "EV-SPEND-001",
                "accepted_ad_spend",
                _money(spend_total),
                "AED",
                "reconciled",
                "Sum of accepted advertising rows; invalid, duplicate and conflicting rows are excluded.",
                {"summand": ad_refs},
            ),
            _claim(
                "EV-REV-001",
                "accepted_revenue",
                _money(revenue_total),
                "AED",
                "reconciled",
                "Sum of accepted revenue rows, attributed or not; invalid, duplicate and conflicting rows are excluded.",
                {"summand": revenue_refs},
            ),
            _claim(
                "EV-ATTR-001",
                "attributed_revenue",
                _money(attributed_total),
                "AED",
                "assumption-dependent",
                "Revenue connected deterministically through revenue.lead_id to CRM campaign_id and advertising channel.",
                {"summand": attributed_refs, "join": attribution_joins},
                attribution_assumption,
            ),
            _claim(
                "EV-COVER-001",
                "revenue_attribution_coverage",
                str((coverage * 100).quantize(Decimal("0.1"))),
                "percent",
                "reconciled",
                "Attributed revenue (numerator rows) as a share of accepted revenue (denominator rows).",
                {"numerator": attributed_refs, "denominator": revenue_refs, "join": attribution_joins},
            ),
            _claim(
                "EV-UNMATCH-001",
                "unattributed_or_invalid_revenue",
                _money(unattributed_total),
                "AED",
                "reconciled",
                "Accepted revenue that could not be assigned to a channel: no accepted or unambiguous CRM lead, "
                "no accepted advertising campaign, or revenue dated before lead creation.",
                {"summand": _refs(unattributed)},
            ),
            _claim(
                "EV-CONFLICT-001",
                "revenue_with_conflicting_lead_attribution",
                _money(conflicted_total),
                "AED",
                "reconciled",
                "Accepted revenue whose CRM lead has contradictory records; excluded from attribution "
                "instead of letting file order choose a campaign.",
                {
                    "summand": _refs(conflicted_revenue),
                    "join": [
                        ref
                        for row in conflicted_revenue
                        for ref in conflicting_leads[row["link_id"]]
                    ],
                },
            ),
        ]
        refund_rows = [line for invoice in revenue for line in invoice["rows"] if line["value"] < 0]
        refunded_total = sum((row["value"] for row in refund_rows), Decimal("0"))
        if engagement.declares_refunds:
            claims.append(
                _claim(
                    "EV-REFUND-001",
                    "refunded_revenue",
                    _money(refunded_total),
                    "AED",
                    "reconciled",
                    "Refunds and credit notes in the revenue exports, as negative amounts already netted into "
                    "accepted revenue.",
                    {"summand": [row["source_ref"] for row in refund_rows]},
                )
            )
        claims.extend(channel_claims)
        claims.extend(window_claims)

        ordered = self._complete_dispositions(dispositions, raw)
        rejected = [
            RejectedRecord(
                row.source, row.source_row, row.record_id,
                SEVERITY_BY_STATUS[row.status], row.reason, row.status, row.source_file,
            )
            for row in ordered
            if row.status != ACCEPTED
        ]
        status_counts = {
            source: {
                "input": len(rows),
                **{
                    status: sum(1 for row in ordered if row.source == source and row.status == status)
                    for status in STATUSES
                },
            }
            for source, rows in raw.items()
        }

        recommendation = self._recommend(
            coverage,
            str((coverage * 100).quantize(Decimal("0.1"))),
            ordered,
            _money(unattributed_total),
            _money(conflicted_total),
            sensitivity[0],
            engagement.threshold,
        )
        fingerprints = {
            (f"{source}/{spec.label}" if spec.label else source): _fingerprint(spec.path)
            for source in SOURCES
            for spec in engagement.files[source]
        }
        fingerprints.update({
            f"{source}/{spec.lookup_path.name}": _fingerprint(spec.lookup_path)
            for source in SOURCES
            for spec in engagement.files[source]
            if spec.lookup_path is not None
        })
        ruleset = json.loads(json.dumps(RULESET))
        ruleset["attribution_windows_days"] = list(engagement.windows)
        ruleset["coverage_threshold_percent"] = engagement.threshold
        ruleset_fingerprint = hashlib.sha256(
            json.dumps(ruleset, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        identity = "\n".join(
            [f"engine:{ENGINE_VERSION}", f"ruleset:{ruleset_fingerprint}", f"as_of:{as_of or ''}"]
            + ([f"config:{engagement.config_fingerprint}"] if engagement.declared else [])
            + [f"{source}:{digest}" for source, digest in sorted(fingerprints.items())]
        )
        run_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return EvidenceReport(
            run_id=run_id,
            as_of=as_of,
            engine_version=ENGINE_VERSION,
            ruleset={**ruleset, "fingerprint": ruleset_fingerprint},
            source_fingerprints=fingerprints,
            summary={
                "accepted_spend_aed": _money(spend_total),
                "accepted_revenue_aed": _money(revenue_total),
                "attributed_revenue_aed": _money(attributed_total),
                "unattributed_or_invalid_revenue_aed": _money(unattributed_total),
                "conflicting_lead_revenue_aed": _money(conflicted_total),
                "attribution_coverage_percent": str(
                    (coverage * 100).quantize(Decimal("0.1"))
                ),
                "accepted_rows": {
                    "ads": len(ads),
                    "crm": len(crm),
                    "revenue": len(revenue),
                },
                "row_status_counts": status_counts,
                "rejected_or_uncertain_rows": len(rejected),
                **({"refunded_revenue_aed": _money(refunded_total)} if engagement.declares_refunds else {}),
            },
            channels=channel_rows,
            sensitivity=sensitivity,
            claims=claims,
            rejected_records=rejected,
            dispositions=ordered,
            recommendation=recommendation,
            human_approval={
                "required": True,
                "approver_role": "accountable budget owner",
                "status": "not approved",
                "allowed_values": ["approve", "reject", "request-more-evidence"],
            },
            narrative={
                "status": "disabled",
                "reason": "authoritative deterministic report generated; optional AI narrative not requested",
                "content": "",
            },
            exceptions=_exceptions(ordered),
            engagement=engagement.describe(),
            source_paths=(
                {"config": str(engagement.config_path)}
                if engagement.declared
                else {source: str(engagement.files[source][0].path) for source in SOURCES}
            ),
        )

    @staticmethod
    def _complete_dispositions(
        dispositions: dict[str, RowDisposition],
        inputs: dict[str, list[tuple[FileSpec, int, int, dict[str, Any]]]],
    ) -> list[RowDisposition]:
        """Every input row must have exactly one status; a gap is an engine defect."""
        expected = {_ref(source, spec.label, line) for source, rows in inputs.items() for spec, _, line, _ in rows}
        missing = expected - set(dispositions)
        unexpected = set(dispositions) - expected
        if missing or unexpected:
            raise RuntimeError(
                f"row disposition invariant violated; missing={sorted(missing)} unexpected={sorted(unexpected)}"
            )
        file_order = {(source, spec.label): index for source, rows in inputs.items() for spec, index, _, _ in rows}
        return sorted(
            dispositions.values(),
            key=lambda row: (SOURCE_ORDER[row.source], file_order.get((row.source, row.source_file), 0), row.source_row),
        )

    @staticmethod
    def _validate_ads(
        rows: Iterable[tuple[FileSpec, int, int, dict[str, Any]]],
        dispositions: dict[str, RowDisposition],
        aliases: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        aliases = aliases or {}
        valid: list[dict[str, Any]] = []
        for spec, file_index, source_row, row in rows:
            if not spec.included(row):
                _dispose(
                    dispositions, "ads", source_row, spec.read_text(row, "campaign_id") or f"row-{source_row}", FILTERED,
                    f"{spec.filter_column} is not one of the declared values", _readable_amount(spec, row, "spend_aed"),
                    spec.label,
                )
                continue
            campaign_id = spec.read_text(row, "campaign_id")
            channel = spec.read_text(row, "channel")
            channel = aliases.get(channel.casefold(), channel)
            record_id = campaign_id or f"row-{source_row}"
            try:
                parsed = {
                    "date": spec.read_date(row, "date"),
                    "campaign_id": campaign_id,
                    "channel": channel,
                    "spend": spec.read_amount(row, "spend_aed"),
                    "row_id": spec.read_text(row, "row_id") if "row_id" in spec.columns else "",
                    "source_row": source_row,
                    "source_file": spec.label,
                    "source_ref": _ref("ads", spec.label, source_row),
                    "order": (file_index, source_row),
                }
                if not campaign_id or not channel:
                    raise ValueError("campaign_id and channel are required")
                if "row_id" in spec.columns and not parsed["row_id"]:
                    raise ValueError("row_id is required")
                if any(mark in value for value in (campaign_id, channel) for mark in "[]"):
                    # Labels reach the narrative gate; a bracket could imitate a citation.
                    raise ValueError("campaign_id and channel must not contain square brackets")
            except ValueError as exc:
                _dispose(
                    dispositions, "ads", source_row, record_id, REJECTED, str(exc),
                    _readable_amount(spec, row, "spend_aed"), spec.label,
                )
                continue
            valid.append(parsed)

        channels_by_campaign: dict[str, set[str]] = defaultdict(set)
        for row in valid:
            channels_by_campaign[row["campaign_id"]].add(row["channel"])
        conflicted = {campaign for campaign, channels in channels_by_campaign.items() if len(channels) > 1}

        remaining: list[dict[str, Any]] = []
        for row in valid:
            if row["campaign_id"] in conflicted:
                _dispose(
                    dispositions, "ads", row["source_row"], row["campaign_id"], CONFLICT,
                    "campaign_id maps to conflicting channels", row["spend"], row["source_file"],
                )
                continue
            remaining.append(row)
        if remaining and remaining[0]["row_id"]:
            # The export identifies each row, so repeated identical rows are only
            # duplicates when they repeat the same identifier.
            accepted, _ = _resolve_identity(
                remaining,
                key="row_id",
                signature=lambda row: (row["date"], row["campaign_id"], row["channel"], row["spend"]),
                dispositions=dispositions,
                source="ads",
                duplicate_reason="duplicate advertising row_id",
                conflict_reason=lambda members: "row_id has conflicting values",
                amount_field="spend",
            )
            return accepted, conflicted

        accepted: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for row in remaining:
            signature = (row["date"], row["campaign_id"], row["channel"], row["spend"])
            if signature in seen:
                _dispose(
                    dispositions, "ads", row["source_row"], row["campaign_id"], DUPLICATE,
                    "exact duplicate advertising row", row["spend"], row["source_file"],
                )
                continue
            seen.add(signature)
            accepted.append(row)
            _dispose(
                dispositions, "ads", row["source_row"], row["campaign_id"], ACCEPTED, "", row["spend"], row["source_file"]
            )
        return accepted, conflicted

    @staticmethod
    def _validate_crm(
        rows: Iterable[tuple[FileSpec, int, int, dict[str, Any]]],
        dispositions: dict[str, RowDisposition],
        customer_join: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Accepted leads, and the refs of conflicting CRM rows keyed by the revenue join key."""
        valid: list[dict[str, Any]] = []
        for spec, file_index, source_row, row in rows:
            if not spec.included(row):
                _dispose(
                    dispositions, "crm", source_row, spec.read_text(row, "lead_id") or f"row-{source_row}", FILTERED,
                    f"{spec.filter_column} is not one of the declared values", None, spec.label,
                )
                continue
            lead_id = spec.read_text(row, "lead_id")
            campaign_id = spec.read_text(row, "campaign_id")
            customer_id = spec.read_text(row, "customer_id") if customer_join else ""
            record_id = lead_id or f"row-{source_row}"
            try:
                if not lead_id or not campaign_id:
                    raise ValueError("lead_id and campaign_id are required")
                if customer_join and not customer_id:
                    raise ValueError("customer_id is required")
                parsed = {
                    "lead_id": lead_id,
                    "campaign_id": campaign_id,
                    "customer_id": customer_id,
                    "created_at": spec.read_date(row, "created_at"),
                    "status": spec.read_text(row, "status").lower(),
                    "source_row": source_row,
                    "source_file": spec.label,
                    "source_ref": _ref("crm", spec.label, source_row),
                    "order": (file_index, source_row),
                }
                if not parsed["status"]:
                    raise ValueError("status is required")
            except ValueError as exc:
                _dispose(dispositions, "crm", source_row, record_id, REJECTED, str(exc), source_file=spec.label)
                continue
            valid.append(parsed)

        def conflict_reason(members: list[dict[str, Any]]) -> str:
            if len({row["campaign_id"] for row in members}) > 1:
                return "lead_id has conflicting campaign attribution"
            if len({row["customer_id"] for row in members}) > 1:
                return "lead_id has conflicting customer_id values"
            return "lead_id has conflicting created_at or status values"

        accepted, conflicts = _resolve_identity(
            valid,
            key="lead_id",
            signature=lambda row: (row["campaign_id"], row["created_at"], row["status"], row["customer_id"]),
            dispositions=dispositions,
            source="crm",
            duplicate_reason="duplicate lead_id",
            conflict_reason=conflict_reason,
        )
        if not customer_join:
            return accepted, conflicts
        by_ref = {row["source_ref"]: row for row in valid}
        by_customer: dict[str, list[str]] = defaultdict(list)
        for members in conflicts.values():
            for ref in members:
                by_customer[by_ref[ref]["customer_id"]].append(ref)
        return accepted, dict(by_customer)

    @staticmethod
    def _validate_revenue(
        rows: Iterable[tuple[FileSpec, int, int, dict[str, Any]]],
        dispositions: dict[str, RowDisposition],
        customer_join: bool = False,
    ) -> list[dict[str, Any]]:
        link = "customer_id" if customer_join else "lead_id"
        line_items = any("line_id" in spec.columns for spec, _, _, _ in rows)
        valid: list[dict[str, Any]] = []
        for spec, file_index, source_row, row in rows:
            if not spec.included(row):
                _dispose(
                    dispositions, "revenue", source_row, spec.read_text(row, "transaction_id") or f"row-{source_row}",
                    FILTERED, f"{spec.filter_column} is not one of the declared values",
                    _readable_amount(spec, row, "value_aed"), spec.label,
                )
                continue
            transaction_id = spec.read_text(row, "transaction_id")
            link_id = spec.read_text(row, link)
            record_id = transaction_id or f"row-{source_row}"
            try:
                if not transaction_id or not link_id:
                    raise ValueError(f"transaction_id and {link} are required")
                line_id = spec.read_text(row, "line_id") if line_items else ""
                if line_items and not line_id:
                    raise ValueError("line_id is required")
                parsed = {
                    "transaction_id": transaction_id,
                    "line_id": line_id,
                    "identity": f"{transaction_id}\x1f{line_id}" if line_items else transaction_id,
                    "link_id": link_id,
                    "value": spec.read_amount(row, "value_aed"),
                    "date": spec.read_date(row, "date"),
                    "source_row": source_row,
                    "source_file": spec.label,
                    "source_ref": _ref("revenue", spec.label, source_row),
                    "order": (file_index, source_row),
                }
            except ValueError as exc:
                _dispose(
                    dispositions, "revenue", source_row, record_id, REJECTED, str(exc),
                    _readable_amount(spec, row, "value_aed"), spec.label,
                )
                continue
            valid.append(parsed)

        accepted, _ = _resolve_identity(
            valid,
            key="identity",
            signature=lambda row: (row["link_id"], row["value"], row["date"]),
            dispositions=dispositions,
            source="revenue",
            duplicate_reason="duplicate transaction_id and line_id" if line_items else "duplicate transaction_id",
            conflict_reason=lambda members: (
                "transaction_id and line_id have conflicting values" if line_items else "transaction_id has conflicting values"
            ),
            amount_field="value",
        )
        if not line_items:
            return [
                {**row, "refs": [row["source_ref"]], "rows": [row]}
                for row in accepted
            ]

        # One invoice per transaction_id: its lines must agree on who it belongs to and when.
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in accepted:
            grouped[row["transaction_id"]].append(row)
        invoices: list[dict[str, Any]] = []
        for transaction_id, lines in grouped.items():
            if len({line["link_id"] for line in lines}) > 1 or len({line["date"] for line in lines}) > 1:
                for line in lines:
                    _dispose(
                        dispositions, "revenue", line["source_row"], transaction_id, CONFLICT,
                        f"transaction_id has conflicting {link} or date across its lines", line["value"], line["source_file"],
                    )
                continue
            first = min(lines, key=lambda line: line["order"])
            invoices.append(
                {
                    "transaction_id": transaction_id,
                    "link_id": first["link_id"],
                    "date": first["date"],
                    "value": sum((line["value"] for line in lines), Decimal("0")),
                    "order": first["order"],
                    "source_file": first["source_file"],
                    "source_row": first["source_row"],
                    "refs": [line["source_ref"] for line in lines],
                    "rows": lines,
                }
            )
        invoices.sort(key=lambda invoice: invoice["order"])
        return invoices

    @staticmethod
    def _recommend(
        coverage: Decimal,
        coverage_percent: str,
        dispositions: list[RowDisposition],
        unattributed_aed: str,
        conflicted_aed: str,
        shortest_window: dict[str, Any],
        coverage_threshold: str = RULESET["coverage_threshold_percent"],
    ) -> dict[str, Any]:
        """Data-quality findings and open questions; never a budget move.

        Attribution through CRM campaigns cannot show that spend caused revenue,
        and neither a coverage threshold nor an approval step changes that. The
        output is therefore what the exports substantiate and what must be
        checked next, whatever the coverage.
        """
        threshold = Decimal(coverage_threshold)
        below_threshold = coverage * 100 < threshold
        excluded = Counter(row.status for row in dispositions if row.status in {REJECTED, DUPLICATE, CONFLICT})
        filtered = [row for row in dispositions if row.status == FILTERED]
        unattributed_reasons = {row.reason for row in dispositions if row.status == ACCEPTED_UNATTRIBUTED}

        findings: list[str] = []
        questions: list[str] = []
        if below_threshold:
            findings.append(
                f"Attribution coverage is {coverage_percent}% [EV-COVER-001], below the rule set's "
                f"reporting threshold of {coverage_threshold}%."
            )
        if Decimal(unattributed_aed) > 0:
            findings.append(
                f"AED {_grouped(unattributed_aed)} of accepted revenue could not be assigned to a channel [EV-UNMATCH-001]."
            )
        if Decimal(conflicted_aed) > 0:
            findings.append(
                f"AED {_grouped(conflicted_aed)} of that revenue belongs to leads or customers with contradictory CRM "
                "records [EV-CONFLICT-001]."
            )
            questions.append("Which campaign is correct for each lead with contradictory CRM records?")
        if excluded:
            findings.append(
                f"{excluded[REJECTED]} rejected, {excluded[DUPLICATE]} duplicate and {excluded[CONFLICT]} "
                "conflicting source rows are excluded from every figure; each is listed in row_dispositions.csv."
            )
            questions.append("Can the source systems correct or confirm the rejected, duplicate and conflicting rows?")
        if filtered:
            held = sum((Decimal(row.amount_aed) for row in filtered if row.amount_aed), Decimal("0"))
            findings.append(
                f"{len(filtered)} rows holding AED {_grouped(_money(held))} were excluded by a declared filter in the "
                "engagement config; each is listed in row_dispositions.csv."
            )
        window = shortest_window["attribution_window_days"]
        delayed = shortest_window["excluded_delayed_revenue_aed"]
        if Decimal(delayed) > 0:
            findings.append(
                f"AED {_grouped(delayed)} of attributed revenue arrived more than {window} days after lead creation "
                f"[{shortest_window['evidence_ids']['excluded']}], so channel figures change with the attribution window."
            )
        if REASON_NO_LEAD in unattributed_reasons:
            questions.append("Can the CRM export include the leads referenced by unattributed transactions?")
        if REASON_NO_CUSTOMER in unattributed_reasons:
            questions.append("Can the CRM export include the customers referenced by unattributed transactions?")
        if REASON_CUSTOMER_CAMPAIGNS in unattributed_reasons:
            questions.append("Which campaign should be credited for customers whose leads came from more than one campaign?")
        if REASON_NO_CAMPAIGN in unattributed_reasons:
            questions.append("Can the advertising export include every campaign referenced by CRM leads?")
        if REASON_CHRONOLOGY in unattributed_reasons:
            questions.append("Why are some transactions dated before their lead was created?")
        questions.append(
            "What comparison or controlled test would the budget owner accept as evidence that changing "
            "channel spend changes revenue?"
        )

        reasons = []
        if below_threshold:
            reasons.append("Attribution coverage is below the rule set's reporting threshold.")
        if excluded:
            reasons.append("Some source rows were rejected, duplicated or in conflict.")
        reasons.append("Channel figures are attribution-based and do not show that spend caused revenue.")
        return {
            "action": "request-more-evidence" if below_threshold or excluded else "review-evidence",
            "decision": "Do not reallocate budget from this evidence set alone.",
            "reason": " ".join(reasons),
            "evidence_grade": "unsupported" if below_threshold else "assumption-dependent",
            "findings": findings,
            "questions": questions,
            "human_approval_required": True,
        }


FIX_OWNER_BY_REASON = {
    REASON_NO_CAMPAIGN: "Advertising account owner",
    REASON_CONFLICTING_CAMPAIGN: "Advertising account owner",
    REASON_NO_LEAD: "CRM owner",
    REASON_CONFLICTING_LEAD: "CRM owner",
    REASON_CHRONOLOGY: "CRM owner",
    REASON_NO_CUSTOMER: "CRM owner",
    REASON_CONFLICTING_CUSTOMER: "CRM owner",
    REASON_CUSTOMER_CAMPAIGNS: "CRM owner",
}
FIX_OWNER_BY_SOURCE = {"ads": "Advertising account owner", "crm": "CRM owner", "revenue": "Finance or billing owner"}
BRIEF_EXCEPTION_GROUPS = 10


def _exceptions(dispositions: list[RowDisposition]) -> list[dict[str, Any]]:
    """Rows that do not count in full, grouped by source, status and reason, largest amount first."""
    groups: dict[tuple[str, str, str], list[RowDisposition]] = {}
    for row in dispositions:
        if row.status != ACCEPTED:
            groups.setdefault((row.source, row.status, row.reason), []).append(row)
    items = []
    for (source, status, reason), rows in groups.items():
        amounts = [Decimal(row.amount_aed) for row in rows if row.amount_aed]
        items.append(
            {
                "source": source,
                "status": status,
                "reason": reason,
                "rows": len(rows),
                "amount_aed": _money(sum(amounts, Decimal("0"))),
                "rows_without_amount": len(rows) - len(amounts),
                "example_refs": [_ref(row.source, row.source_file, row.source_row) for row in rows[:3]],
                "who_can_fix": FIX_OWNER_BY_REASON.get(reason, FIX_OWNER_BY_SOURCE[source]),
            }
        )
    items.sort(
        key=lambda item: (
            -abs(Decimal(item["amount_aed"])), -item["rows"], SOURCE_ORDER[item["source"]], item["status"], item["reason"]
        )
    )
    return items


def diagnose(engagement: Engagement, report: EvidenceReport, sample: int = 500) -> list[str]:
    """Why rows were rejected, and which declaration would have matched them instead.

    Guidance only: it never changes a figure. A single wrong date format rejects
    almost every row, and without this the only clue is thousands of identical
    rejections.
    """
    rejected: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in report.dispositions:
        if row.status == REJECTED:
            rejected[(row.source, row.source_file)].append(row.source_row)
    hints: list[str] = []
    for (source, label), lines in sorted(rejected.items()):
        spec = next((item for item in engagement.files[source] if item.label == label), None)
        if spec is None:
            continue
        try:
            rows = dict(_read_csv(spec))
        except InputContractError:
            continue
        wanted = set(lines[:sample])
        rows = {line: row for line, row in rows.items() if line in wanted}
        if not rows:
            continue
        name = f"{source}/{label}" if label else source
        total = len(rows)

        date_field = "created_at" if source == "crm" else "date"
        values = [spec.read_text(row, date_field) for row in rows.values()]
        matches = {
            fmt: sum(1 for value in values if pattern.fullmatch(value)) for fmt, pattern in DATE_FORMATS.items()
        }
        declared_best = max((matches[fmt] for fmt in spec.date_formats), default=0)
        best = max(matches, key=lambda fmt: (matches[fmt], fmt in spec.date_formats))
        if matches[best] > declared_best:
            hints.append(
                f"{name}: {matches[best]} of {total} rejected rows would match date_format \"{best}\" "
                f"(declared \"{spec.date_format}\")."
            )

        for field in AMOUNT_FIELDS[source]:
            def parses(candidate: FileSpec) -> int:
                total_ok = 0
                for row in rows.values():
                    try:
                        candidate.read_amount(row, field)
                    except ValueError:
                        continue
                    total_ok += 1
                return total_ok

            current = parses(spec)
            best_spec, best_count = spec, current
            for decimal_mark in DECIMAL_MARKS:
                for separator in ("", DECIMAL_MARKS[decimal_mark]):
                    for currency_label in ("", spec.currency):
                        for refunds in (spec.refunds, "negative_values") if source == "revenue" else (spec.refunds,):
                            candidate = replace(
                                spec, thousands_separator=separator, currency_label=currency_label,
                                decimal=decimal_mark, refunds=refunds,
                            )
                            count = parses(candidate)
                            if count > best_count:
                                best_spec, best_count = candidate, count
            if best_count > current:
                declared = [
                    f"thousands_separator \"{best_spec.thousands_separator}\"",
                    f"currency_label \"{best_spec.currency_label}\"",
                ]
                if best_spec.decimal != spec.decimal:
                    declared.append(f"decimal \"{best_spec.decimal}\"")
                if best_spec.refunds != spec.refunds:
                    declared.append(f"refunds \"{best_spec.refunds}\"")
                hints.append(
                    f"{name}: {best_count} of {total} rejected rows would match {field} with "
                    + ", ".join(declared)
                    + "."
                )

        for field in IDENTIFIER_FIELDS[source]:
            if field not in spec.columns or field not in spec.required:
                continue
            empty = sum(1 for row in rows.values() if not spec.read_text(row, field))
            if empty == total:
                hints.append(
                    f"{name}: every rejected row has an empty {field} in column '{spec.columns[field]}'"
                    + (
                        "; if revenue names customers rather than leads, set revenue_join to customer_id."
                        if source == "revenue" and field == "lead_id"
                        else "."
                    )
                )
    return hints


OUTPUT_NAMES = (
    "report.json",
    "lineage.csv",
    "row_dispositions.csv",
    "rejected_records.csv",
    "exceptions.csv",
    "executive_brief.md",
    "report.html",
    "evaluation_results.json",
)


def clear_outputs(output_dir: str | Path) -> None:
    """Remove files a previous run published, so a failed run cannot leave a stale brief behind."""
    output = Path(output_dir)
    for name in OUTPUT_NAMES:
        (output / name).unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_outputs(report: EvidenceReport, output_dir: str | Path) -> dict[str, Any]:
    """Publish outputs only after independent reconstruction passes.

    Everything is written to a staging directory and checked by
    reconstruct.verify_outputs, which re-derives every row status and figure from
    the raw exports without using this module. On failure only
    evaluation_results.json is published, listing why; no report, lineage or
    brief is left behind. Returns the evaluation.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    clear_outputs(output)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=output))
    try:
        _write_files(report, staging)
        checks, failures = verify_outputs(report.source_paths, staging)
        evaluation: dict[str, Any] = {
            "run_id": report.run_id,
            "engine_version": report.engine_version,
            "status": "FAIL" if failures else "PASS",
            "checks": checks,
            "failures": failures,
            "published_outputs": {},
        }
        if failures:
            _write_json(output / "evaluation_results.json", evaluation)
            return evaluation
        evaluation["published_outputs"] = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(staging.iterdir())
        }
        _write_json(staging / "evaluation_results.json", evaluation)
        for path in sorted(staging.iterdir()):
            os.replace(path, output / path.name)
        return evaluation
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _report_payload(report: EvidenceReport) -> dict[str, Any]:
    """The report as published: detail that would make it unopenable stays in the CSVs."""
    payload = report.to_dict()
    entries = sum(len(claim.lineage) for claim in report.claims)
    omitted: dict[str, int] = {}
    if len(report.dispositions) > REPORT_ROW_LIMIT:
        payload.pop("dispositions")
        payload.pop("rejected_records")
        omitted["dispositions"] = len(report.dispositions)
        omitted["rejected_records"] = len(report.rejected_records)
    if entries > REPORT_LINEAGE_LIMIT:
        for claim in payload["claims"]:
            claim["lineage_entries"] = len(claim.pop("lineage"))
            claim.pop("source_refs")
        omitted["lineage_entries"] = entries
    if omitted:
        payload["detail_in_files"] = {
            **omitted,
            "row_dispositions": "row_dispositions.csv",
            "rejected_records_file": "rejected_records.csv",
            "lineage": "lineage.csv",
        }
    return payload


def _write_files(report: EvidenceReport, output: Path) -> None:
    (output / "report.json").write_text(
        json.dumps(_report_payload(report), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )

    with (output / "rejected_records.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["source", "source_row", "record_id", "status", "severity", "reason", "source_file"]
        )
        writer.writeheader()
        for row in report.rejected_records:
            writer.writerow(asdict(row))

    with (output / "lineage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["evidence_id", "metric", "grade", "role", "source_ref"],
        )
        writer.writeheader()
        for claim in report.claims:
            for entry in claim.lineage:
                writer.writerow(
                    {
                        "evidence_id": claim.evidence_id,
                        "metric": claim.metric,
                        "grade": claim.grade,
                        "role": entry["role"],
                        "source_ref": entry["source_ref"],
                    }
                )

    supports: dict[str, list[str]] = defaultdict(list)
    for claim in report.claims:
        for entry in claim.lineage:
            supports[entry["source_ref"]].append(f"{claim.evidence_id}:{entry['role']}")
    with (output / "row_dispositions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source", "source_row", "record_id", "status", "reason", "amount_aed", "supports", "source_file"],
        )
        writer.writeheader()
        for row in report.dispositions:
            writer.writerow(
                {
                    **asdict(row),
                    "supports": ";".join(supports.get(_ref(row.source, row.source_file, row.source_row), [])),
                }
            )

    with (output / "exceptions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["rows", "amount_aed", "rows_without_amount", "status", "source", "reason", "who_can_fix", "example_refs"],
        )
        writer.writeheader()
        for item in report.exceptions:
            writer.writerow({**item, "example_refs": ";".join(item["example_refs"])})

    (output / "executive_brief.md").write_text(_brief(report), encoding="utf-8")
    (output / "report.html").write_text(_html(report), encoding="utf-8")


def _exception_cell(item: dict[str, Any]) -> str:
    if item["rows_without_amount"] == item["rows"]:
        return "—"
    extra = item["rows_without_amount"]
    return _grouped(item["amount_aed"]) + (f" (+{extra} without a readable amount)" if extra else "")


def _heading(report: EvidenceReport) -> tuple[str, str, str]:
    """Title, evidence-status sentence and page tag, set by where the data came from."""
    engagement = report.engagement
    if engagement is None:
        return (
            "Executive Evidence Brief — Synthetic Demonstration",
            "synthetic and reproducible; not a customer result, real-data deployment, reference, or paid validation.",
            "SYNTHETIC DEMONSTRATION — NOT CUSTOMER VALIDATION",
        )
    if engagement["data_origin"] == "synthetic":
        return (
            f"Executive Evidence Brief — Synthetic Example: {engagement['name']}",
            "synthetic exports read under the declared interpretations below; not a customer result, real-data "
            "deployment, reference, or paid validation.",
            "SYNTHETIC EXAMPLE — NOT CUSTOMER VALIDATION",
        )
    if engagement["data_origin"] == "public":
        return (
            f"Executive Evidence Brief — Public Data Rehearsal: {engagement['name']}",
            "public data read under the declared interpretations below; not a customer result or paid validation.",
            "PUBLIC DATA REHEARSAL — NOT CUSTOMER VALIDATION",
        )
    return (
        f"Executive Evidence Brief — {engagement['name']}",
        "computed from the client's exports under the declared interpretations below; it does not measure causation "
        "or return on investment.",
        "CLIENT EXPORTS — DETERMINISTIC EVIDENCE, NOT A CAUSAL ANALYSIS",
    )


def _interpretations(report: EvidenceReport) -> str:
    engagement = report.engagement
    if engagement is None:
        return ""
    lines = [
        "## Declared interpretations",
        "",
        f"Config fingerprint `{engagement['config_fingerprint'][:16]}`. Revenue is joined to CRM records through "
        f"`{engagement['revenue_join']}`."
        + {
            "unattributed": "",
            "first_touch": " A customer with leads from several campaigns is credited to the earliest lead.",
            "last_touch": " A customer with leads from several campaigns is credited to the latest lead created by the "
            "transaction date.",
        }[engagement["multiple_campaigns"]],
    ]
    if engagement["channel_aliases"]:
        lines.append(
            "Channel names read as: "
            + "; ".join(f"'{key}' as {value}" for key, value in engagement["channel_aliases"].items())
            + "."
        )
    lines.append("")
    for item in engagement["files"]:
        parts = [", ".join(f"{name} from '{header}'" for name, header in item["columns"].items())]
        if item["fixed"]:
            parts.append(", ".join(f"{name} fixed as {value}" for name, value in item["fixed"].items()))
        if item["delimiter"] != "comma" or item["encoding"] != "utf-8":
            parts.append(f"{item['delimiter']}-separated {item['encoding']} text")
        parts.append("dates written " + " or ".join(item["date_formats"]))
        if item.get("time_zone"):
            parts.append(f"times written in {item['time_zone']}")
        if item.get("header_row", 1) > 1:
            parts.append(f"column names read from line {item['header_row']}")
        if item["time_zone_shift_hours"]:
            parts.append(f"times shifted {item['time_zone_shift_hours']:+d} hours before the date is taken")
        if item["source"] != "crm":
            amount = [
                f"thousands separator '{item['thousands_separator']}'" if item["thousands_separator"] else "no thousands separator"
            ]
            if item["currency_label"]:
                amount.append(f"currency label {item['currency_label']}")
            if item.get("decimal", "point") != "point":
                amount.append("decimal comma")
            if item["value_from"]:
                amount.append(
                    f"value {item['value_from']['operation']} of " + " and ".join(item["value_from"]["columns"])
                )
            if item["currency_column"]:
                amount.append(
                    f"currency from '{item['currency_column']}' ("
                    + ", ".join(f"{code} at {rate}" for code, rate in item["currency_rates"].items())
                    + " AED per unit)"
                )
            if item["currency"] != "AED":
                amount.append(f"{item['currency']} converted at {item['aed_per_unit']} AED per unit ({item['rate_source']})")
            if item["refunds"] == "negative_values":
                amount.append("negative amounts are refunds")
            elif item["refunds"] == "whole_file":
                amount.append("every row is a refund")
            parts.append("amounts with " + ", ".join(amount))
        if item["uppercase"]:
            parts.append("upper-cased " + ", ".join(item["uppercase"]))
        lines.append(f"- `{item['source']}/{item['file']}`: " + "; ".join(parts) + ".")
    return "\n".join(lines) + "\n\n"


def _brief(report: EvidenceReport) -> str:
    recommendation = report.recommendation
    title, status_line, _ = _heading(report)
    summary = report.summary
    as_of_line = f"**As of:** {report.as_of}  \n" if report.as_of else ""
    engine_line = (
        f"**Engine:** {report.engine_version} · **Rule set:** {report.ruleset['id']} "
        f"version {report.ruleset['version']}"
    )
    findings = "\n".join(f"- {item}" for item in recommendation["findings"]) or "- None."
    questions = "\n".join(f"- {item}" for item in recommendation["questions"])
    threshold_line = (
        f"Coverage threshold: {report.ruleset['coverage_threshold_percent']}% — "
        f"{report.ruleset['coverage_threshold_basis']}."
    )
    channels = "\n".join(
        f"- **{row['channel']}** — spend AED {_grouped(row['spend_aed'])} `[{row['evidence_ids']['spend']}]`; "
        f"attributed revenue AED {_grouped(row['attributed_revenue_aed'])} `[{row['evidence_ids']['attributed_revenue']}]`; "
        f"assumption-dependent ROAS {row['assumption_dependent_roas']}× `[{row['evidence_ids']['roas']}]`."
        for row in report.channels
    ) or "- No channel has accepted spend."
    sensitivity = "\n".join(
        f"- {row['attribution_window_days']} days: AED {_grouped(row['attributed_revenue_aed'])} attributed "
        f"`[{row['evidence_ids']['attributed']}]`; AED {_grouped(row['excluded_delayed_revenue_aed'])} excluded "
        f"`[{row['evidence_ids']['excluded']}]`."
        for row in report.sensitivity
    )
    refund = next((claim for claim in report.claims if claim.evidence_id == "EV-REFUND-001"), None)
    refund_line = (
        f"- Refunds and credit notes, netted into revenue: **AED {_grouped(refund.value)}** `[EV-REFUND-001]`\n"
        if refund
        else ""
    )
    shown = report.exceptions[:BRIEF_EXCEPTION_GROUPS]
    if shown:
        table = "\n".join(
            f"| {item['rows']} | {_exception_cell(item)} | {item['status']} | {item['reason'] or '—'} | "
            f"{item['who_can_fix']} | " + ", ".join(f"`{ref}`" for ref in item["example_refs"]) + " |"
            for item in shown
        )
        more = len(report.exceptions) - len(shown)
        exceptions = (
            "Rows excluded from every figure, or counted in revenue without a channel, grouped by reason. "
            "Largest amounts first.\n\n"
            "| Rows | AED in these rows | Status | Reason | Who can fix it | Examples |\n"
            "|---:|---:|---|---|---|---|\n"
            + table
            + (f"\n\n{more} more groups are listed in exceptions.csv." if more else "")
        )
    else:
        exceptions = "None: every row was accepted and attributed."
    return f"""# {title}

**Evidence status:** {status_line}
**Run ID:** `{report.run_id}`
**Decision status:** not approved; named budget-owner approval is required.
{as_of_line}{engine_line}

## Decision question

What can the supplied advertising, CRM, and revenue exports defend about the next channel-budget decision?

## Reconciled evidence

- Accepted spend: **AED {_grouped(summary['accepted_spend_aed'])}** `[EV-SPEND-001]`
- Accepted revenue: **AED {_grouped(summary['accepted_revenue_aed'])}** `[EV-REV-001]`
{refund_line}- Attributed revenue: **AED {_grouped(summary['attributed_revenue_aed'])}** `[EV-ATTR-001]`
- Attribution coverage: **{summary['attribution_coverage_percent']}%** `[EV-COVER-001]`
- Unattributed or invalid revenue: **AED {_grouped(summary['unattributed_or_invalid_revenue_aed'])}** `[EV-UNMATCH-001]`
- Revenue with conflicting lead attribution: **AED {_grouped(summary['conflicting_lead_revenue_aed'])}** `[EV-CONFLICT-001]`
- Rejected or uncertain rows: **{summary['rejected_or_uncertain_rows']}**

## Exceptions to resolve

{exceptions}

## Channel view

{channels}

## Sensitivity to conversion delay

{sensitivity}

## Recommendation

**{recommendation['decision']}**

Reason: {recommendation['reason']} This recommendation is graded **{recommendation['evidence_grade']}** and cannot be executed without human approval.

### What the exports show

{findings}

### Questions that need answers before any budget decision

{questions}

{threshold_line}

{_interpretations(report)}## What the evidence cannot defend

- causal incrementality or a claim that advertising caused the revenue;
- customer lifetime value beyond the supplied transaction period;
- an ROI guarantee or autonomous budget change;
- any conclusion from rejected, unmatched, or silently repaired rows.

## Required next action

The accountable budget owner must choose **approve**, **reject**, or **request more evidence**, record the rationale, and define the observation period. The deterministic report remains authoritative even if the optional AI narrative is disabled.
"""


def _html(report: EvidenceReport) -> str:
    # Source labels come from customer files; nothing from them is trusted as markup.
    title, _, tag = _heading(report)
    summary = report.summary
    rows = "".join(
        "<tr>"
        f"<td>{escape(row['channel'])}</td><td>AED {escape(_grouped(row['spend_aed']))}</td>"
        f"<td>AED {escape(_grouped(row['attributed_revenue_aed']))}</td>"
        f"<td>{escape(row['assumption_dependent_roas'])}×</td>"
        f"<td>{escape(row['evidence_grade'])}</td></tr>"
        for row in report.channels
    )
    exception_rows = "".join(
        "<tr>"
        f"<td>{item['rows']}</td><td>{escape(_exception_cell(item))}</td><td>{escape(item['status'])}</td>"
        f"<td>{escape(item['reason'])}</td><td>{escape(item['who_can_fix'])}</td>"
        f"<td>{escape(', '.join(item['example_refs']))}</td></tr>"
        for item in report.exceptions[:BRIEF_EXCEPTION_GROUPS]
    )
    recommendation = report.recommendation
    findings = "".join(f"<li>{escape(item)}</li>" for item in recommendation["findings"])
    questions = "".join(f"<li>{escape(item)}</li>" for item in recommendation["questions"])
    identity = (
        f"Run <code>{escape(report.run_id)}</code> · engine {escape(report.engine_version)} · "
        f"rule set {escape(report.ruleset['id'])} version {escape(report.ruleset['version'])}"
        + (f" · as of {escape(report.as_of)}" if report.as_of else "")
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
body{{font-family:Inter,Arial,sans-serif;margin:0;background:#f4f7fb;color:#17233b}}main{{max-width:960px;margin:40px auto;padding:0 24px}}.tag{{display:inline-block;background:#fff1cf;color:#6f4d00;padding:7px 10px;border-radius:16px;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:22px 0}}.card,section{{background:white;border:1px solid #dbe4f0;border-radius:12px;padding:20px;box-shadow:0 4px 16px #18233a0a}}.metric{{font-size:28px;font-weight:800;color:#173f7a}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #e4e9f0}}.warn{{border-left:5px solid #d18a00}}.approval{{border-left:5px solid #b42318}}small{{color:#596579}}h1,h2{{color:#183e73}}</style>
</head><body><main>
<span class="tag">{escape(tag)}</span>
<h1>{escape(title)}</h1><p>{identity}. Deterministic result; human approval required.</p>
<div class="grid">
<div class="card"><small>Accepted spend</small><div class="metric">AED {escape(_grouped(summary['accepted_spend_aed']))}</div><small>EV-SPEND-001</small></div>
<div class="card"><small>Accepted revenue</small><div class="metric">AED {escape(_grouped(summary['accepted_revenue_aed']))}</div><small>EV-REV-001</small></div>
<div class="card"><small>Attribution coverage</small><div class="metric">{summary['attribution_coverage_percent']}%</div><small>EV-COVER-001</small></div>
<div class="card"><small>Rows needing attention</small><div class="metric">{summary['rejected_or_uncertain_rows']}</div><small>Visible, never repaired silently</small></div>
</div>
<section><h2>Exceptions to resolve</h2><div class="scroll"><table><thead><tr><th>Rows</th><th>AED in these rows</th><th>Status</th><th>Reason</th><th>Who can fix it</th><th>Examples</th></tr></thead><tbody>{exception_rows or '<tr><td colspan="6">None</td></tr>'}</tbody></table></div></section>
<section><h2>Channel evidence</h2><div class="scroll"><table><thead><tr><th>Channel</th><th>Spend</th><th>Attributed revenue</th><th>ROAS</th><th>Grade</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section class="warn"><h2>Recommendation</h2><p><strong>{escape(recommendation['decision'])}</strong></p><p>{escape(recommendation['reason'])}</p><p>Evidence grade: {escape(recommendation['evidence_grade'])}.</p><h3>What the exports show</h3><ul>{findings or '<li>None.</li>'}</ul><h3>Questions that need answers before any budget decision</h3><ul>{questions}</ul><p><small>Coverage threshold {escape(report.ruleset['coverage_threshold_percent'])}%: {escape(report.ruleset['coverage_threshold_basis'])}.</small></p></section>
<section class="approval"><h2>Approval gate</h2><p>Status: <strong>not approved</strong>. The accountable budget owner must approve, reject, or request more evidence. No action is automated.</p></section>
</main></body></html>"""

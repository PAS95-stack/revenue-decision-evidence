"""Propose an engagement config by reading the exports, with the evidence for each choice.

Writing a config by hand means opening every export and transcribing headers. This
reads them instead and drafts one, saying for each choice how it was reached: which
column matched, how many sampled values parsed under the format proposed, and how many
identifiers overlap between files.

Nothing here changes how a run reads data. The draft is written beside the exports for
a person to check and rename; the run still uses only what the config declares, and a
value that does not match its declaration is still rejected. Where the data cannot
settle a choice — an exchange rate, a channel name, or a date that reads both ways —
the draft says so rather than guessing quietly.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine import (
    AMOUNT_FIELDS,
    CANONICAL_FIELDS,
    CUSTOMER_JOIN_FIELDS,
    DATE_FORMATS,
    DECIMAL_MARKS,
    DELIMITERS,
    ENCODINGS,
    FileSpec,
)

# Characters that belong to a written number, so anything else beside it is a label.
NUMBER_CHARACTERS = "0123456789.,-() "
MAX_PREAMBLE_ROWS = 6

SAMPLE_ROWS = 1000
SLASH_DATE = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/[0-9]{4}")
# Header words that name each field in the exports these systems produce.
FIELD_WORDS: dict[str, tuple[str, ...]] = {
    "date": ("reporting starts", "day", "date", "week", "month", "timestamp"),
    "campaign_id": ("campaign id", "utm campaign", "campaign name", "campaign", "utm_campaign", "source campaign",
                    "origin"),
    "channel": ("campaign type", "channel", "platform", "network", "source / medium", "medium", "source"),
    "spend_aed": ("amount spent", "cost", "spend", "amount", "budget spent"),
    "row_id": ("ad id", "ad set id", "adset id", "ad group id", "row id"),
    "lead_id": ("record id", "lead id", "mql id", "contact id", "person id", "lead", "mql", "contact", "reference"),
    "created_at": ("create date", "created at", "created date", "created", "first contact", "signup date"),
    "status": ("lifecycle stage", "deal stage", "stage", "status", "state"),
    "customer_id": ("customer id", "customerid", "account id", "client id", "seller id", "merchant id", "customer"),
    "transaction_id": ("invoice number", "invoiceno", "invoice no", "invoice", "order id", "order number",
                       "transaction id", "receipt number", "payment id"),
    "line_id": ("line item id", "lineitem sku", "line number", "line", "sku", "stock code", "stockcode", "item id"),
    "value_aed": ("line total", "net sales", "net amount", "grand total", "line amount", "extended price",
                  "order total", "sales", "revenue", "total", "amount", "value", "subtotal", "price"),
}
SOURCE_WORDS = {
    "ads": ("spend", "cost", "impressions", "clicks", "campaign", "ad set", "adset"),
    "crm": ("lifecycle", "lead", "contact", "stage", "deal", "mql"),
    "revenue": ("invoice", "order", "transaction", "receipt", "payment", "total"),
}
CURRENCY_WORDS = ("currency", "currency code", "ccy")
QUANTITY_WORDS = ("quantity", "qty", "units", "lineitem quantity")
UNIT_PRICE_WORDS = ("unit price", "price", "rate", "lineitem price", "unit cost")
NEEDS_YOU = "**needs you**"
# A file name is evidence of the platform when an export carries no channel column.
PLATFORM_NAMES = {
    "meta": "Meta", "facebook": "Meta", "instagram": "Meta", "google": "Google Ads", "gads": "Google Ads",
    "tiktok": "TikTok", "snap": "Snapchat", "linkedin": "LinkedIn", "twitter": "X", "youtube": "YouTube",
}


@dataclass
class Export:
    path: Path
    source: str
    delimiter: str
    encoding: str
    headers: list[str]
    rows: list[dict[str, str]]
    header_row: int = 1
    entry: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def values(self, canonical: str) -> list[str]:
        header = self.entry.get("columns", {}).get(canonical)
        return [(row.get(header) or "").strip() for row in self.rows] if header else []


EXACT = 300


def _plain(header: str) -> str:
    """One spelling of a header, so order_id, Order ID and OrderID all read alike."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", header.strip())
    return re.sub(r"\s+", " ", re.sub(r"[_\-.]+", " ", spaced)).strip().lower()


def _matched(header: str, words: tuple[str, ...]) -> tuple[int, str]:
    """How well a header names a field, and the word that matched.

    A header equal to the word beats one that merely contains it. Among equals the
    list order decides, so an id column wins a name column; among words merely
    contained, the longer wins, so 'first contact' takes 'first_contact_date' from
    'contact'. Whole words only: 'contact' does not match inside 'contacted'.
    """
    plain = _plain(header)
    best, matched = 0, ""
    for position, word in enumerate(words):
        if plain == word:
            score = EXACT - position * 4
        elif re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", plain):
            score = 100 + len(word) - position
        else:
            continue
        if score > best:
            best, matched = score, word
    return best, matched


def _score(header: str, words: tuple[str, ...]) -> int:
    return _matched(header, words)[0]


def _named(headers: list[str], words: tuple[str, ...]) -> str:
    found = [(_score(header, words), -index, header) for index, header in enumerate(headers)]
    found.sort(reverse=True)
    return found[0][2] if found and found[0][0] > 0 else ""


def _read_sample(
    path: Path, delimiter: str, encoding: str, header_row: int = 1
) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding=ENCODINGS[encoding], newline="") as handle:
        reader = csv.reader(handle, delimiter=DELIMITERS[delimiter])
        headers: list[str] = []
        rows: list[dict[str, str]] = []
        for index, record in enumerate(reader, start=1):
            if index < header_row or not record:
                continue
            if not headers:
                headers = record
            else:
                rows.append(dict(zip(headers, record)))
            if len(rows) >= SAMPLE_ROWS:
                break
    return headers, rows


def _reading(path: Path) -> Export | None:
    """The delimiter and encoding that read the most complete rows, each tried in turn."""
    best: Export | None = None
    best_score = -1
    for encoding in ENCODINGS:
        for delimiter in DELIMITERS:
            for header_row in range(1, MAX_PREAMBLE_ROWS + 1):
                try:
                    headers, rows = _read_sample(path, delimiter, encoding, header_row)
                except (UnicodeDecodeError, UnicodeError, csv.Error, OSError):
                    continue
                if len(headers) < 2 or not rows:
                    continue
                score = len(headers) * 10 + sum(1 for header in headers if header.strip() and header.isprintable())
                score += sum(1 for row in rows if len(row) == len(headers))
                # A title above the header only wins if it genuinely reads better.
                score -= header_row
                if score > best_score:
                    best = Export(path, "", delimiter, encoding, headers, rows, header_row)
                    best_score = score
    return best


def _classify(headers: list[str]) -> str:
    """Which export this is, judged by how well its headers cover each source's fields."""

    def coverage(source: str) -> int:
        fields = sum(
            max((_score(header, FIELD_WORDS[field]) for header in headers), default=0)
            for field in CANONICAL_FIELDS[source]
        )
        return fields + sum(1 for header in headers for word in SOURCE_WORDS[source] if word in header.strip().lower())

    return max(("ads", "crm", "revenue"), key=coverage)


def _fraction(rows: list[dict[str, str]], spec: FileSpec, field_name: str, reader: str) -> float:
    if not rows:
        return 0.0
    parsed = 0
    for row in rows:
        try:
            getattr(spec, reader)(row, field_name)
        except ValueError:
            continue
        parsed += 1
    return parsed / len(rows)


def _day_order(export: Export) -> str:
    """DD/MM or MM/DD, decided by any value in the file where one position exceeds 12."""
    day_first = month_first = 0
    for row in export.rows:
        for header in export.headers:
            match = SLASH_DATE.fullmatch((row.get(header) or "").strip()[:10])
            if match is None:
                continue
            first, second = int(match.group(1)), int(match.group(2))
            day_first += first > 12 >= second
            month_first += second > 12 >= first
    if day_first and not month_first:
        return "DD/MM/YYYY"
    if month_first and not day_first:
        return "MM/DD/YYYY"
    return ""


def _date_declaration(export: Export, header: str, field_name: str) -> tuple[list[str], float, str]:
    """Which declared formats read the most values, and what is still undecided."""
    scored = sorted(
        (
            _fraction(export.rows, FileSpec("ads", Path("x"), "x", {field_name: header}, {},
                                            date_formats=(name,), fields=(field_name,)), field_name, "read_date"),
            name,
        )
        for name in DATE_FORMATS
    )
    scored.reverse()
    best = scored[0][0]
    tied = [name for share, name in scored if share == best]
    families = {name.split(" ")[0].split("T")[0] for name in tied}
    note = ""
    chosen = [tied[0]]
    if {"DD/MM/YYYY", "MM/DD/YYYY"} <= families:
        decided = _day_order(export)
        suffix = tied[0][len(tied[0].split(" ")[0].split("T")[0]):]
        if decided:
            chosen = [decided + suffix]
            note = f"{decided} because another value in this file has a day above 12"
        else:
            chosen = ["DD/MM/YYYY" + suffix]
            note = (f"{NEEDS_YOU}: every sampled value fits both DD/MM/YYYY and MM/DD/YYYY; DD/MM/YYYY is proposed. "
                    "Confirm which the system writes")
    if best < 1.0 and not note:
        for share, name in scored[1:]:
            if share == 0:
                break
            if {"DD/MM/YYYY", "MM/DD/YYYY"} <= {item.split(" ")[0].split("T")[0] for item in chosen + [name]}:
                continue
            combined = FileSpec("ads", Path("x"), "x", {field_name: header}, {},
                                date_formats=tuple(chosen + [name]), fields=(field_name,))
            share = _fraction(export.rows, combined, field_name, "read_date")
            if share > best:
                chosen, best = chosen + [name], share
    return chosen, best, note


def _labels(export: Export, headers: tuple[str, ...]) -> tuple[str, ...]:
    """Text written beside the numbers, such as AED, $ or R$, taken from the values themselves."""
    seen: dict[str, int] = {}
    for row in export.rows:
        for header in headers:
            value = (row.get(header) or "").strip()
            start, end = 0, len(value)
            while start < len(value) and value[start] not in NUMBER_CHARACTERS:
                start += 1
            while end > start and value[end - 1] not in NUMBER_CHARACTERS:
                end -= 1
            for mark in (value[:start].strip(), value[end:].strip()):
                if mark:
                    seen[mark] = seen.get(mark, 0) + 1
    return tuple(sorted(seen, key=lambda mark: (-seen[mark], mark))[:2])


def _amount_declaration(
    export: Export, header: str, field_name: str, value_from: tuple[str, tuple[str, str]] | None = None
) -> tuple[dict[str, Any], float]:
    """The decimal mark, separator, label and refund handling that read the most amounts."""
    best_declaration: dict[str, Any] = {}
    best_share = -1.0
    columns = {} if value_from else {field_name: header}
    labels = ("",) + _labels(export, value_from[1] if value_from else (header,))
    for decimal in DECIMAL_MARKS:
        for separator in ("", DECIMAL_MARKS[decimal]):
            for label in labels:
                for refunds in ("reject", "negative_values") if export.source == "revenue" else ("reject",):
                    spec = FileSpec(export.source, Path("x"), "x", columns, {},
                                    thousands_separator=separator, decimal=decimal, currency_label=label,
                                    refunds=refunds, value_from=value_from, fields=(field_name,))
                    share = _fraction(export.rows, spec, field_name, "read_amount")
                    if share > best_share:
                        declaration: dict[str, Any] = {}
                        if separator:
                            declaration["thousands_separator"] = separator
                        if decimal != "point":
                            declaration["decimal"] = decimal
                        if label:
                            declaration["currency_label"] = label
                        if refunds != "reject":
                            declaration["refunds"] = refunds
                        best_declaration, best_share = declaration, share
    return best_declaration, best_share


def _overlap(left: list[str], right: list[str]) -> float:
    values = {value for value in left if value}
    return len(values & {value for value in right if value}) / len(values) if values else 0.0


def _map_columns(export: Export, fields: tuple[str, ...], excluded: tuple[str, ...] = ()) -> None:
    taken: set[str] = set(name for name in excluded if name)
    columns: dict[str, str] = {}
    evidence: dict[str, str] = {}
    scored = sorted(
        (
            (*_matched(header, FIELD_WORDS[name]), name, index, header)
            for name in fields
            for index, header in enumerate(export.headers)
        ),
        key=lambda item: (-item[0], item[3], item[4]),
    )
    for score, word, name, _index, header in scored:
        if score <= 0 or name in columns or header in taken:
            continue
        taken.add(header)
        columns[name] = header
        evidence[name] = "header matched" if score >= EXACT else f"header contains '{word}'"
    for name in fields:
        if name in columns:
            export.notes.append(f"| {name} | `{columns[name]}` | {evidence[name]} |")
        elif name == "channel" and export.source == "ads":
            stem = export.path.stem.lower()
            platform = next((title for word, title in PLATFORM_NAMES.items() if word in stem), "")
            if platform:
                export.entry.setdefault("fixed", {})["channel"] = platform
                export.notes.append(f"| channel | fixed as {platform} | no channel column; the file name says "
                                    f"`{export.path.name}`. Change it if that is not the platform |")
            else:
                export.notes.append(
                    f'| channel | — | {NEEDS_YOU}: no channel column. Add `"fixed": {{"channel": "Meta"}}` '
                    "with the platform this export came from |"
                )
        else:
            export.notes.append(f"| {name} | — | {NEEDS_YOU}: no column matched |")
    optional = "row_id" if export.source == "ads" else "line_id" if export.source == "revenue" else ""
    if optional:
        header = _named([header for header in export.headers if header not in taken], FIELD_WORDS[optional])
        if header and (optional == "row_id" or _repeats(export, columns)):
            columns[optional] = header
            reason = ("invoice numbers repeat across rows, so this is a line-item export"
                      if optional == "line_id" else "the export identifies each row")
            export.notes.append(f"| {optional} | `{header}` | {reason} |")
    export.entry["columns"] = columns


def _repeats(export: Export, columns: dict[str, str]) -> bool:
    header = columns.get("transaction_id")
    if not header:
        return False
    seen = [(row.get(header) or "").strip() for row in export.rows]
    return len(seen) > len({value for value in seen if value})


def propose(folder: Path, name: str, data_origin: str = "client") -> tuple[dict[str, Any], str]:
    """Read every export in the folder and draft a config, with a report explaining it."""
    paths = sorted(
        path for path in folder.glob("*.csv")
        if path.name != "engagement.json" and not path.name.endswith(".conversion.json")
    )
    if not paths:
        raise FileNotFoundError(f"no .csv exports in {folder}")
    exports: list[Export] = []
    for path in paths:
        export = _reading(path)
        if export is None:
            continue
        export.source = _classify(export.headers)
        exports.append(export)

    lines = [f"# Proposed engagement config for {name}", "",
             "Every choice below was read from the exports. Check each one, especially any",
             f"line marked {NEEDS_YOU}, then rename the draft to `engagement.json`.", ""]

    crm = next((export for export in exports if export.source == "crm"), None)
    revenue = next((export for export in exports if export.source == "revenue"), None)
    join = "lead_id"
    if crm and revenue:
        _map_columns(crm, CUSTOMER_JOIN_FIELDS["crm"])
        customers = crm.values("customer_id")
        if customers:
            by_customer = _overlap([(row.get(_named(revenue.headers, FIELD_WORDS["customer_id"])) or "").strip()
                                    for row in revenue.rows], customers)
            by_lead = _overlap([(row.get(_named(revenue.headers, FIELD_WORDS["lead_id"])) or "").strip()
                                for row in revenue.rows], crm.values("lead_id"))
            if by_customer > by_lead:
                join = "customer_id"
                lines += [f"Revenue names **customers**: {by_customer:.0%} of sampled revenue identifiers appear in the "
                          f"CRM's customer column, against {by_lead:.0%} for its lead column.", ""]
        crm.entry = {}
        crm.notes = []
    required = CANONICAL_FIELDS if join == "lead_id" else CUSTOMER_JOIN_FIELDS

    for export in exports:
        export.entry["file"] = export.path.name
        if export.header_row > 1:
            export.entry["header_row"] = export.header_row
            export.notes.append(f"| header row | line {export.header_row} | the lines above it are not column names |")
        if export.delimiter != "comma":
            export.entry["delimiter"] = export.delimiter
        if export.encoding != "utf-8":
            export.entry["encoding"] = export.encoding
        quantity, price = _named(export.headers, QUANTITY_WORDS), _named(export.headers, UNIT_PRICE_WORDS)
        # A quantity beside a unit price are the parts of a total, never the total itself.
        parts = (quantity, price) if quantity and price and AMOUNT_FIELDS[export.source] else ()
        _map_columns(export, required[export.source], parts)
        for name_of_field, header in list(export.entry["columns"].items()):
            if name_of_field in ("date", "created_at"):
                formats, share, note = _date_declaration(export, header, name_of_field)
                export.entry["date_format"] = formats[0] if len(formats) == 1 else formats
                explained = note or f"{' or '.join(formats)} reads {share:.0%} of sampled values"
                if not note and share <= 0.95:
                    explained += f" {NEEDS_YOU}: check the format"
                export.notes.append(f"| dates | `{header}` | {explained} |")
            if name_of_field in AMOUNT_FIELDS[export.source]:
                declaration, share = _amount_declaration(export, header, name_of_field)
                if declaration:
                    export.entry["amounts"] = declaration
                described = ", ".join(f"{key} {value}" for key, value in declaration.items()) or "plain decimals"
                mark = "" if share > 0.95 else f" {NEEDS_YOU}: check the amount format"
                export.notes.append(f"| amounts | `{header}` | {described} reads {share:.0%} of sampled values{mark} |")
        if AMOUNT_FIELDS[export.source] and not set(AMOUNT_FIELDS[export.source]) & set(export.entry["columns"]):
            quantity, price = _named(export.headers, QUANTITY_WORDS), _named(export.headers, UNIT_PRICE_WORDS)
            if quantity and price:
                amount_field = AMOUNT_FIELDS[export.source][0]
                computed, share = _amount_declaration(export, "", amount_field, ("multiply", (quantity, price)))
                if computed:
                    export.entry.setdefault("amounts", {}).update(computed)
                export.entry.setdefault("amounts", {})["value_from"] = {"multiply": [quantity, price]}
                export.notes = [note for note in export.notes if not note.startswith(f"| {amount_field} |")]
                export.notes.append(f"| {amount_field} | computed | `{quantity}` × `{price}`, as no column names a "
                                    "total. Check this is the line total before running |")
        currency = _named(export.headers, CURRENCY_WORDS)
        if currency and AMOUNT_FIELDS[export.source]:
            codes = sorted({(row.get(currency) or "").strip().upper() for row in export.rows} - {""})[:10]
            export.entry.setdefault("amounts", {})["currency"] = {
                "column": currency,
                "rates": {code: "0" for code in codes},
                "rate_source": "REPLACE with where these rates come from",
            }
            export.notes.append(f"| currency | `{currency}` | {NEEDS_YOU}: {', '.join(codes)} found. The config stays "
                                "invalid until you enter each rate and its source |")
        if export.source == "ads":
            channels = sorted({value for value in export.values("channel") if value})[:8]
            if channels:
                export.notes.append(f"| channel values | `{export.entry['columns']['channel']}` | {', '.join(channels)}."
                                    " Add `channel_aliases` if two exports name one channel differently |")
        if export.source == "revenue":
            status = _named(export.headers, FIELD_WORDS["status"])
            if status:
                values = sorted({(row.get(status) or "").strip() for row in export.rows} - {""})[:8]
                export.notes.append(f"| statuses | `{status}` | {', '.join(values)}. Add `include_when` to count only "
                                    "the ones that are real revenue |")

    # One system writes meta-101 and another META-101: say so rather than losing the join.
    ad_campaigns = [value for export in exports if export.source == "ads" for value in export.values("campaign_id")]
    pairs = [(crm, "campaign_id", ad_campaigns, "the advertising exports"),
             (revenue, join, crm.values(join) if crm else [], "the CRM export")]
    for left, left_field, other, described in pairs:
        if not left or not other:
            continue
        mine = left.values(left_field)
        plain = _overlap(mine, other)
        upper = _overlap([value.upper() for value in mine], [value.upper() for value in other])
        if upper > plain and (plain == 0 or upper - plain >= 0.2):
            left.entry.setdefault("uppercase", []).append(left_field)
            left.notes.append(f"| uppercase | `{left_field}` | matching {described} rises from {plain:.0%} to "
                              f"{upper:.0%} when upper-cased |")

    # One export writing both 'Search' and 'search' would otherwise read as two channels,
    # and a campaign that maps to two channels is dropped as a conflict.
    spellings: dict[str, dict[str, int]] = {}
    for export in exports:
        if export.source == "ads":
            for value in export.values("channel"):
                if value:
                    counted = spellings.setdefault(value.casefold(), {})
                    counted[value] = counted.get(value, 0) + 1
    aliases: dict[str, str] = {}
    alias_notes: list[str] = []
    for folded, counted in sorted(spellings.items()):
        if len(counted) > 1:
            # A channel is a proper name in the brief, so a capitalised spelling wins over a bare one.
            chosen = sorted(counted, key=lambda name: (not name[:1].isupper(), -counted[name], name))[0]
            aliases[folded] = chosen
            alias_notes.append(f"| {chosen} | {', '.join(sorted(counted))} | one channel written several ways; "
                               "read as a single name so it is not counted twice |")

    sources: dict[str, list[dict[str, Any]]] = {"ads": [], "crm": [], "revenue": []}
    for export in exports:
        sources[export.source].append(export.entry)
        lines += [f"## `{export.source}/{export.path.name}`", "",
                  f"Read as {export.delimiter}-separated {export.encoding} text, {len(export.headers)} columns, "
                  f"{len(export.rows)} rows sampled.", "",
                  "| Field | Column | How it was chosen |", "|---|---|---|", *export.notes, ""]
    for source, entries in sources.items():
        if not entries:
            lines += [f"{NEEDS_YOU}: no export looked like the {source} export. Add one to this folder and propose again.", ""]
    if alias_notes:
        lines += ["## Channel names", "", "| Read as | Spellings found | How it was chosen |", "|---|---|---|",
                  *alias_notes, ""]
    config = {
        "config_version": 1,
        "engagement": name,
        "data_origin": data_origin,
        "revenue_join": join,
        **({"channel_aliases": aliases} if aliases else {}),
        "sources": {
            source: entries or [{"file": f"ADD_THE_{source.upper()}_EXPORT.csv", "columns": {}}]
            for source, entries in sources.items()
        },
    }
    lines += ["Then run the engagement. A run refuses anything it cannot read, and prints which",
              "declaration would have matched the rows it rejected."]
    return config, "\n".join(lines) + "\n"


def write_proposal(folder: Path, name: str, data_origin: str = "client") -> tuple[Path, Path]:
    config, report = propose(folder, name, data_origin)
    draft = folder / "engagement.proposed.json"
    notes = folder / "engagement.proposal.md"
    draft.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    notes.write_text(report, encoding="utf-8")
    return draft, notes

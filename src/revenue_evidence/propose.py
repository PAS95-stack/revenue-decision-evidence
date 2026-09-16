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
from itertools import combinations
from dataclasses import dataclass, field, replace
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
    numeric_order,
)
from .joins import Join, Table, discover

# Characters that belong to a written number, so anything else beside it is a label.
NUMBER_CHARACTERS = "0123456789.,-() "
MAX_PREAMBLE_ROWS = 6

SAMPLE_ROWS = 1000
NUMERIC_DATE = re.compile(r"([0-9]{1,2})[-/.]([0-9]{1,2})[-/.][0-9]{2,4}")
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
# A choice only a person can make blocks a run; one whose cost is visible in the
# report — rows that will be refused and listed — is worth checking but not blocking.
CHECK = "**check**"
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


def advisory(report: str) -> list[str]:
    """Lines worth a look whose consequence is already visible in the report."""
    return [line for line in report.split("\n") if CHECK in line and line.startswith("|")]


def unresolved(report: str) -> list[str]:
    """The lines of a draft report a person must settle before a run.

    The report explains itself, so the words "needs you" also appear in its own
    instructions; only the table rows and the missing-export lines are decisions.
    """
    return [
        line for line in report.split("\n")
        if NEEDS_YOU in line and (line.startswith("|") or line.startswith(NEEDS_YOU))
    ]


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
    """Day-first or month-first, decided by any value in the file where one position exceeds 12."""
    day_first = month_first = 0
    for row in export.rows:
        for header in export.headers:
            match = NUMERIC_DATE.match((row.get(header) or "").strip())
            if match is None:
                continue
            first, second = int(match.group(1)), int(match.group(2))
            day_first += first > 12 >= second
            month_first += second > 12 >= first
    if day_first and not month_first:
        return "day-first"
    if month_first and not day_first:
        return "month-first"
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
    note = ""
    chosen = [tied[0]]
    if {"day-first", "month-first"} <= {numeric_order(name) for name in tied}:
        decided = _day_order(export)
        if decided:
            chosen = [next(name for name in tied if numeric_order(name) == decided)]
            note = f"{chosen[0]} because another value in this file has a day above 12"
        else:
            chosen = [next(name for name in tied if numeric_order(name) == "day-first")]
            note = (f"{NEEDS_YOU}: every sampled value reads as both a day-first and a month-first date; "
                    f"{chosen[0]} is proposed. Confirm which the system writes")
    if best < 1.0 and not note:
        for share, name in scored[1:]:
            if share == 0:
                break
            if {"day-first", "month-first"} <= {numeric_order(item) for item in chosen + [name]}:
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


WEAK_DATE_WORDS = frozenset({"ship", "shipping", "shipped", "delivery", "delivered", "carrier", "estimated",
                             "limit", "due", "expiry", "expires", "approved"})
# The fields without which an export cannot stand as that source at all.
CORE_FIELDS = {"ads": ("date", "campaign_id", "spend_aed"), "crm": ("lead_id", "campaign_id", "created_at"),
               "revenue": ("transaction_id", "value_aed")}
# The column identifying a record. A looked-up value must describe that record, so a sale's
# date is looked up on the sale, never on the seller who made it.
RECORD_KEYS = {"ads": "campaign_id", "crm": "lead_id", "revenue": "transaction_id"}


def _assign(
    headers: list[str], fields: tuple[str, ...], excluded: tuple[str, ...] = ()
) -> tuple[dict[str, str], dict[str, str]]:
    """Give every header to the field that names it best, not to whichever field is checked first."""
    taken: set[str] = set(name for name in excluded if name)
    columns: dict[str, str] = {}
    evidence: dict[str, str] = {}
    scored = sorted(
        (
            (*_matched(header, FIELD_WORDS[name]), name, index, header)
            for name in fields
            for index, header in enumerate(headers)
        ),
        key=lambda item: (-item[0], item[3], item[4]),
    )
    for score, word, name, _index, header in scored:
        if score <= 0 or name in columns or header in taken:
            continue
        taken.add(header)
        columns[name] = header
        evidence[name] = "header matched" if score >= EXACT else f"header contains '{word}'"
    return columns, evidence


def _map_columns(export: Export, fields: tuple[str, ...], excluded: tuple[str, ...] = ()) -> None:
    columns, evidence = _assign(export.headers, fields, excluded)
    taken = set(name for name in excluded if name) | set(columns.values())
    for name in fields:
        if name in columns:
            export.notes.append(f"| {name} | `{columns[name]}` | {evidence[name]} |")
        elif name == "channel" and export.source == "ads":
            stem = export.path.stem.lower()
            platform = next((title for word, title in PLATFORM_NAMES.items() if word in stem), "")
            if platform:
                export.entry.setdefault("fixed", {})["channel"] = platform
                export.notes.append(f"| channel | fixed as {platform} | {CHECK}: no channel column; the file name "
                                    f"says `{export.path.name}`. Change it if that is not the platform |")
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


def _weak_date(header: str) -> bool:
    """A fulfilment date — shipped, delivered, due — says when goods moved, not when a sale was made."""
    return bool(set(_plain(header).split()) & WEAK_DATE_WORDS)


def _parts(export: Export, source: str) -> tuple[str, ...]:
    quantity, price = _named(export.headers, QUANTITY_WORDS), _named(export.headers, UNIT_PRICE_WORDS)
    # A quantity beside a unit price are the parts of a total, never the total itself.
    return (quantity, price) if quantity and price and AMOUNT_FIELDS[source] else ()


def _covers_core(export: Export, source: str) -> bool:
    columns, _ = _assign(export.headers, CUSTOMER_JOIN_FIELDS[source], _parts(export, source))
    have = set(columns) | ({AMOUNT_FIELDS[source][0]} if _parts(export, source) else set())
    return set(CORE_FIELDS[source]) <= have


def _every_value(export: Export, header: str) -> set[str]:
    """Every value a column holds across the whole file.

    Sampled rows are not enough to say two exports share transactions: files sorted
    differently can share every one while their first thousand rows share none.
    """
    values: set[str] = set()
    with export.path.open("r", encoding=ENCODINGS[export.encoding], newline="") as handle:
        position = None
        for line, record in enumerate(csv.reader(handle, delimiter=DELIMITERS[export.delimiter]), start=1):
            if line < export.header_row or not record:
                continue
            if position is None:
                if header not in record:
                    return values
                position = record.index(header)
                continue
            if position < len(record) and record[position].strip():
                values.add(record[position].strip())
    return values


def _roles(exports: list[Export]) -> tuple[dict[str, str], dict[str, str]]:
    """Which exports stand as sources. The rest may still supply a missing field as a lookup.

    Where no export of a source covers its core fields, every export classified as that
    source stays one, so its gaps are reported rather than hidden. Two revenue exports
    describing the same transactions are never both counted.
    """
    roles: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for source in ("ads", "crm", "revenue"):
        group = [export for export in exports if export.source == source]
        primaries = [export for export in group if _covers_core(export, source)]
        for export in group:
            standing = not primaries or any(export is primary for primary in primaries)
            roles[export.path.name] = "source" if standing else "secondary"
    counted = [export for export in exports if export.source == "revenue" and roles[export.path.name] == "source"]
    for first, second in combinations(counted, 2):
        if roles[first.path.name] != "source" or roles[second.path.name] != "source":
            continue
        identifiers = []
        for export in (first, second):
            header = _assign(export.headers, ("transaction_id",))[0].get("transaction_id")
            identifiers.append(_every_value(export, header) if header else set())
        smaller = min(len(identifiers[0]), len(identifiers[1]))
        if smaller and len(identifiers[0] & identifiers[1]) >= 0.5 * smaller:
            keep, drop = sorted(
                (first, second),
                key=lambda export: (-len(_assign(export.headers, CUSTOMER_JOIN_FIELDS["revenue"] + ("lead_id",))[0]),
                                    export.path.name),
            )
            roles[drop.path.name] = "duplicate"
            reasons[drop.path.name] = (f"it describes the same transactions as `{keep.path.name}`, so counting both "
                                       "would count revenue twice")
    return roles, reasons


def _link(source: Export, other: Export, joins: list[Join], profiles: dict) -> list:
    """The ways `other` can give each row of `source` at most one row, strongest evidence first.

    Either the source's column is contained in a key of `other` that never repeats, or a
    column of `other`, itself never repeating, is contained in a key of the source: one
    record split across two files, as closed deals extend leads.
    """
    options = []
    for join in joins:
        if join.child == source.path.name and join.parent == other.path.name:
            options.append((tuple(zip(join.parent_columns, join.child_columns)), join))
        elif join.child == other.path.name and join.parent == source.path.name and len(join.child_columns) == 1:
            column = join.child_columns[0]
            if any(profile.column == column and profile.unique for profile in profiles.get(other.path.name, [])):
                options.append((tuple(zip(join.child_columns, join.parent_columns)), join))
    options.sort(key=lambda option: (option[1].confidence != "check", -option[1].containment))
    return options


def _attach_lookup(
    export: Export, exports: list[Export], roles: dict[str, str], joins: list[Join], profiles: dict,
    required: dict[str, tuple[str, ...]],
) -> None:
    """Fill the fields this export lacks from a second export joined on the export's own record."""
    columns = export.entry["columns"]
    wanted = [name for name in required[export.source] if name not in columns]
    if _parts(export, export.source):
        wanted = [name for name in wanted if name not in AMOUNT_FIELDS[export.source]]
    weak = export.source == "revenue" and "date" in columns and _weak_date(columns["date"])
    if weak:
        wanted.append("date")
    if not wanted:
        return
    record = columns.get(RECORD_KEYS[export.source], "")
    candidates = []
    for other in exports:
        if other is export or roles.get(other.path.name) not in ("secondary", "lookup"):
            continue
        for pairs, join in _link(export, other, joins, profiles):
            if record not in {mine for _theirs, mine in pairs} and not set(wanted) <= {"campaign_id", "channel"}:
                continue
            headers = [header for header in other.headers if not ("date" in wanted and _weak_date(header))]
            supplied, _ = _assign(headers, tuple(wanted), tuple(theirs for theirs, _mine in pairs))
            if supplied:
                rank = (join.confidence != "check", -len(supplied), -join.containment, other.path.name)
                candidates.append((rank, other, pairs, join, supplied))
            break
    if not candidates:
        return
    candidates.sort(key=lambda candidate: candidate[0])
    _rank, other, pairs, join, supplied = candidates[0]
    replaced = columns.pop("date") if weak and "date" in supplied else ""
    export.notes = [note for note in export.notes if not any(note.startswith(f"| {name} |") for name in supplied)]
    export.entry["lookup"] = {"file": other.path.name, "match": dict(pairs), "columns": dict(supplied)}
    keys = " and ".join(f"`{theirs}` = `{mine}`" for theirs, mine in pairs)
    for name, header in supplied.items():
        because = f"; `{replaced}` is a fulfilment date, not when the sale was made" if name == "date" and replaced else ""
        export.notes.append(f"| {name} | `{other.path.name}`: `{header}` | looked up on {keys}{because} |")
    mark = CHECK if join.confidence == "check" else NEEDS_YOU
    export.notes.append(f"| lookup | `{other.path.name}` | {mark}: {'; '.join(join.reasons)} |")
    for name in ("date", "created_at"):
        if name in supplied:
            formats, share, note = _date_declaration(other, supplied[name], name)
            export.entry["date_format"] = formats[0] if len(formats) == 1 else formats
            explained = note or f"{' or '.join(formats)} reads {share:.0%} of sampled values"
            export.notes.append(f"| dates | `{other.path.name}`: `{supplied[name]}` | {explained} |")
    for name in AMOUNT_FIELDS[export.source]:
        if name in supplied:
            declaration, share = _amount_declaration(replace(other, source=export.source), supplied[name], name)
            if declaration:
                export.entry["amounts"] = declaration
            described = ", ".join(f"{key} {value}" for key, value in declaration.items()) or "plain decimals"
            export.notes.append(f"| amounts | `{other.path.name}`: `{supplied[name]}` | {described} reads "
                                f"{share:.0%} of sampled values |")
    roles[other.path.name] = "lookup"


def _fix_status(export: Export) -> None:
    """A CRM export with no status column still says who is a lead: every row is one."""
    if "status" in export.entry["columns"] or "status" in export.entry.get("lookup", {}).get("columns", {}):
        return
    if not _covers_core(export, "crm"):
        return
    export.entry.setdefault("fixed", {})["status"] = "lead"
    export.notes = [note for note in export.notes if not note.startswith("| status |")]
    export.notes.append(f"| status | fixed as lead | {CHECK}: no status column, so every row is read as a lead |")


def _not_used(export: Export, exports: list[Export], roles: dict[str, str], reasons: dict[str, str],
              joins: list[Join]) -> str:
    name = export.path.name
    if name in reasons:
        return f"{CHECK}: {reasons[name]}"
    used = {other.path.name for other in exports if roles.get(other.path.name) in ("source", "lookup")}
    partners = sorted({join.parent if join.child == name else join.child for join in joins
                       if (join.child == name and join.parent in used) or (join.parent == name and join.child in used)})
    if partners:
        return (f"{CHECK}: it joins to {', '.join(f'`{partner}`' for partner in partners)} but supplies no field "
                "they are missing")
    return (f"{NEEDS_YOU}: it covers no export's required fields and no column of it joins another export. "
            "Confirm it belongs to this engagement, or remove it")


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
             f"line marked {NEEDS_YOU}, which is a choice only you can make. A line marked {CHECK} is worth",
             "reading but does not stop a run: values that do not match a declaration are refused and",
             "listed in the report rather than counted. Rename the draft to `engagement.json` when it is right.", ""]

    # One record is often exported across files. Find how they join before deciding which
    # export stands as a source and which only supplies fields to one.
    roles, reasons = _roles(exports)
    joins: list[Join] = []
    profiles: dict = {}
    if any(role != "source" for role in roles.values()):
        tables = [Table(export.path.name, export.path, DELIMITERS[export.delimiter], ENCODINGS[export.encoding],
                        export.header_row) for export in exports]
        joins = discover(tables, profiles)
    standing = [export for export in exports if roles[export.path.name] == "source"]

    crm = next((export for export in standing if export.source == "crm"), None)
    revenue = next((export for export in standing if export.source == "revenue"), None)
    join = "lead_id"
    if crm and revenue:
        _map_columns(crm, CUSTOMER_JOIN_FIELDS["crm"])
        customers = crm.values("customer_id")
        lead_key = crm.entry["columns"].get("lead_id", "")
        for other in ([] if customers else exports):
            if roles[other.path.name] != "secondary":
                continue
            for pairs, _evidence in _link(crm, other, joins, profiles):
                if lead_key in {mine for _theirs, mine in pairs}:
                    header = _assign(other.headers, ("customer_id",), tuple(t for t, _m in pairs))[0].get("customer_id")
                    if header:
                        customers = [(row.get(header) or "").strip() for row in other.rows]
                    break
            if customers:
                break
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

    for export in standing:
        export.entry["file"] = export.path.name
        if export.header_row > 1:
            export.entry["header_row"] = export.header_row
            export.notes.append(f"| header row | line {export.header_row} | the lines above it are not column names |")
        if export.delimiter != "comma":
            export.entry["delimiter"] = export.delimiter
        if export.encoding != "utf-8":
            export.entry["encoding"] = export.encoding
        parts = _parts(export, export.source)
        _map_columns(export, required[export.source], parts)
        if joins:
            _attach_lookup(export, exports, roles, joins, profiles, required)
        if export.source == "crm":
            _fix_status(export)
        for name_of_field, header in list(export.entry["columns"].items()):
            if name_of_field in ("date", "created_at"):
                formats, share, note = _date_declaration(export, header, name_of_field)
                export.entry["date_format"] = formats[0] if len(formats) == 1 else formats
                explained = note or f"{' or '.join(formats)} reads {share:.0%} of sampled values"
                if not note and share <= 0.95:
                    explained += f" {CHECK}: values that do not match are refused and listed"
                export.notes.append(f"| dates | `{header}` | {explained} |")
            if name_of_field in AMOUNT_FIELDS[export.source]:
                declaration, share = _amount_declaration(export, header, name_of_field)
                if declaration:
                    export.entry["amounts"] = declaration
                described = ", ".join(f"{key} {value}" for key, value in declaration.items()) or "plain decimals"
                mark = "" if share > 0.95 else f" {CHECK}: values that do not match are refused and listed"
                export.notes.append(f"| amounts | `{header}` | {described} reads {share:.0%} of sampled values{mark} |")
        supplied = export.entry.get("lookup", {}).get("columns", {})
        amount_fields = set(AMOUNT_FIELDS[export.source])
        if amount_fields and not amount_fields & set(export.entry["columns"]) and not amount_fields & set(supplied):
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
    ad_campaigns = [value for export in standing if export.source == "ads" for value in export.values("campaign_id")]
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
    for export in standing:
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
    for export in standing:
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
    idle = [export for export in exports if roles[export.path.name] in ("secondary", "duplicate")]
    if idle:
        lines += ["## Not used", "", "| Export | Why |", "|---|---|",
                  *(f"| `{export.path.name}` | {_not_used(export, exports, roles, reasons, joins)} |" for export in idle),
                  ""]
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

"""Find how exports that split one record across files join, and how sure that is.

A missed join is cheap: a person adds it. A wrong join is expensive: it silently changes
revenue. So a join is proposed only on evidence in the values themselves — one column's
values contained in a unique key of another file — and a name only ever supports that
evidence. The rules follow inclusion-dependency and foreign-key discovery (SPIDER and
BINDER in HPI's Metanome; Rostin et al. 2009; Zhang et al. 2010; Chen et al. 2014):

* profile every column, and set aside what cannot be a key: amounts, dates, booleans,
  free text, and integers not named like identifiers;
* take as parent keys the columns, or pairs of columns, that never repeat and are never
  blank;
* measure containment in one direction only, child values within the parent key;
* guard against overlap by chance: at least MIN_DISTINCT distinct values, the same shape
  of value, integers spread across the parent's range rather than sitting at its start,
  and no unique, near-contiguous integer column matched to another such
  column under a different name: two auto-increment sequences overlap by construction,
  which is what Zhang et al.'s randomness test exists to catch;
* a parent key that repeats would fan one row out into several, and is never a lookup.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from .engine import DATE_FORMATS

KIND_SAMPLE = 2_000
MIN_DISTINCT = 30
PROPOSE = 0.60
CERTAIN = 0.95
SPREAD_SHARE = 0.20
DENSE_SHARE = 0.90
CLOSE = 0.05
MAX_PAIR_COLUMNS = 6

IDENTIFIER_ENDINGS = ("id", "key", "code", "no", "num", "number", "ref", "sku", "uuid", "guid")
NOT_IDENTIFIERS = {"zip", "postal", "postcode", "phone", "fax", "prefix", "lat", "lng", "lon", "latitude", "longitude"}
MEASURE_ENDINGS = ("amount", "price", "cost", "total", "qty", "quantity", "count", "length", "lenght", "duration",
                   "rate", "score", "rating", "year", "age", "weight", "size", "stock", "balance", "percent",
                   "installments", "sequential", "discount", "tax")
BOOLEAN_WORDS = {"true", "false", "yes", "no", "y", "n", "t", "f", "0", "1"}
INTEGER = re.compile(r"-?[0-9]+")
TRAILING_ZERO = re.compile(r"-?[0-9]+\.0+")
DECIMAL = re.compile(r"-?[0-9]*[.,][0-9]+")
SCIENTIFIC = re.compile(r"-?[0-9](?:\.[0-9]+)?[eE][+-]?[0-9]+")
HEX = re.compile(r"[0-9a-fA-F]{16,}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
CODE = re.compile(r"[A-Za-z0-9]+(?:[-_./:#][A-Za-z0-9]+)*")
SYSTEM_PREFIX = re.compile(r"^gid://[A-Za-z]+/[A-Za-z]+/")
KEY_KINDS = ("integer", "hex", "code")


@dataclass(frozen=True)
class Table:
    name: str
    path: Path
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    header_row: int = 1


@dataclass
class Profile:
    table: str
    column: str
    index: int
    rows: int = 0
    blanks: int = 0
    kind: str = "empty"
    shape: str = ""
    drift: str = ""
    identifier: bool = False
    measure: bool = False
    distinct: set[str] = field(default_factory=set)
    repeats: bool = False
    high: int | None = None
    digit_share: float = 0.0

    @property
    def unique(self) -> bool:
        return self.rows > 0 and self.blanks == 0 and not self.repeats

    @property
    def keylike(self) -> bool:
        if self.kind not in KEY_KINDS or self.measure:
            return False
        if self.kind == "integer":
            return self.identifier
        if self.kind == "code":
            # Single words such as surnames or cities are text without spaces, not codes.
            return self.identifier or self.digit_share >= 0.5
        return True


@dataclass(frozen=True)
class Join:
    child: str
    child_columns: tuple[str, ...]
    parent: str
    parent_columns: tuple[str, ...]
    confidence: str
    containment: float
    child_distinct: int
    same_name: bool
    normalised: str
    rivals: tuple[str, ...]
    reasons: tuple[str, ...]


def _tokens(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name.strip())
    return [token for token in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if token]


def identifier_name(name: str) -> bool:
    tokens = _tokens(name)
    if not tokens or any(token in NOT_IDENTIFIERS for token in tokens):
        return False
    return "".join(tokens).endswith(IDENTIFIER_ENDINGS)


def measure_name(name: str) -> bool:
    tokens = _tokens(name)
    return bool(tokens) and not identifier_name(name) and tokens[-1].endswith(MEASURE_ENDINGS)


def comparable_name(name: str) -> str:
    """A column name as systems vary it: o_orderkey and l_orderkey both read 'orderkey'."""
    tokens = _tokens(name)
    if len(tokens) > 1 and re.match(r"^[A-Za-z]{1,3}_", name.strip()) and len("".join(tokens[1:])) >= 4:
        tokens = tokens[1:]
    return "".join(tokens)


def same_name(child: str, parent: str, parent_table: str) -> bool:
    mine, theirs = comparable_name(child), comparable_name(parent)
    if mine == theirs:
        return True
    stem = comparable_name(Path(parent_table).stem)
    stem = stem[:-3] + "y" if stem.endswith("ies") else stem[:-1] if stem.endswith("s") and len(stem) > 3 else stem
    return theirs == "id" and mine == stem + "id"


def named_after(column: str, table: str) -> bool:
    """Whether a column is named after a file, as store_id is after store.csv."""
    mine = comparable_name(column)
    for token in _tokens(Path(table).stem):
        single = token[:-3] + "y" if token.endswith("ies") else token[:-1] if token.endswith("s") and len(token) > 3 else token
        if len(single) >= 3 and mine.startswith(single):
            return True
    return False


def _looks_dated(value: str) -> bool:
    return 6 <= len(value) <= 40 and any(ch.isdigit() for ch in value) and any(ch in "-/. :" for ch in value)


def _kind(values: list[str]) -> tuple[str, str]:
    present = [value for value in values if value]
    if not present:
        return "empty", ""
    lowered = {value.lower() for value in present}
    # A column holding only 0 and 1 is read as integers: a file sorted by a key can open
    # with thousands of rows of "1", which says nothing about whether it is a flag.
    if len(lowered) <= 2 and lowered <= BOOLEAN_WORDS and not all(value.isdigit() for value in lowered):
        return "boolean", ""
    if all(INTEGER.fullmatch(value) for value in present):
        return "integer", ""
    if all(INTEGER.fullmatch(v) or TRAILING_ZERO.fullmatch(v) for v in present):
        return "integer", "written with a trailing .0"
    if all(INTEGER.fullmatch(v) or DECIMAL.fullmatch(v) for v in present):
        return "decimal", ""
    if any(SCIENTIFIC.fullmatch(value) for value in present):
        return "scientific", "written in scientific notation, which has lost digits"
    head = [value for value in present[:50] if _looks_dated(value)]
    if head and len(head) >= 0.95 * min(len(present), 50):
        if sum(1 for value in head if any(p.fullmatch(value) for p in DATE_FORMATS.values())) >= 0.95 * len(head):
            return "date", ""
    if all(HEX.fullmatch(value) for value in present):
        return "hex", ""
    if all(len(value) <= 64 and CODE.fullmatch(SYSTEM_PREFIX.sub("", value)) for value in present):
        drift = "written with a system prefix such as gid://" if any(SYSTEM_PREFIX.match(v) for v in present) else ""
        return "code", drift
    return "text", ""


def _shape(value: str) -> str:
    if INTEGER.fullmatch(value) or TRAILING_ZERO.fullmatch(value):
        return "#"
    if HEX.fullmatch(value):
        return f"hex{len(value)}"
    text = re.sub(r"[0-9]+", "#", SYSTEM_PREFIX.sub("", value).upper())
    return re.sub(r"[A-Z]+", "A", text)


def normal(value: str) -> str:
    """One spelling for values systems write differently: case, spaces, 123.0, 00123, gid:// prefixes."""
    text = SYSTEM_PREFIX.sub("", value.strip()).casefold()
    if TRAILING_ZERO.fullmatch(text):
        text = text.split(".")[0]
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


def _records(table: Table):
    with table.path.open("r", encoding=table.encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=table.delimiter)
        line = 0
        header_seen = False
        for record in reader:
            line += 1
            if line < table.header_row or not record:
                continue
            if not header_seen:
                header_seen = True
            yield record


def profile_table(table: Table) -> list[Profile]:
    records = _records(table)
    header = next(records, None)
    if not header:
        return []
    profiles = [Profile(table.name, name.strip(), index) for index, name in enumerate(header)]
    sample: list[list[str]] = []
    for record in records:
        sample.append(record)
        if len(sample) >= KIND_SAMPLE:
            break
    for profile in profiles:
        values = [(record[profile.index].strip() if profile.index < len(record) else "") for record in sample]
        profile.kind, profile.drift = _kind(values)
        profile.identifier, profile.measure = identifier_name(profile.column), measure_name(profile.column)
        present = [value for value in values if value]
        if present:
            profile.digit_share = sum(1 for value in present if any(ch.isdigit() for ch in value)) / len(present)
        shapes = Counter(_shape(value) for value in set(values) if value)
        if shapes:
            top, count = shapes.most_common(1)[0]
            profile.shape = top if count >= 0.9 * sum(shapes.values()) else "mixed"
    tracked = [profile for profile in profiles if profile.kind in KEY_KINDS]

    def take(record: list[str]) -> None:
        for profile in profiles:
            profile.rows += 1
            if not (record[profile.index].strip() if profile.index < len(record) else ""):
                profile.blanks += 1
        for profile in tracked:
            value = record[profile.index].strip() if profile.index < len(record) else ""
            if not value:
                continue
            if value in profile.distinct:
                profile.repeats = True
            else:
                profile.distinct.add(value)
                if profile.kind == "integer":
                    number = int(float(value))
                    profile.high = number if profile.high is None else max(profile.high, number)

    for record in sample:
        take(record)
    for record in records:
        take(record)
    return profiles


def _described_shape(profile: Profile) -> str:
    if profile.kind == "integer":
        return "whole numbers"
    if profile.shape.startswith("hex"):
        return f"{profile.shape[3:]}-character hexadecimal identifiers"
    if profile.shape in ("", "mixed"):
        return "codes of more than one shape"
    return f"codes shaped like {profile.shape.replace('#', '0')}"


def _dense_run(profile: Profile) -> bool:
    """Whether a column's integers fill most of their own span, as an auto-increment sequence does."""
    try:
        numbers = [int(float(value)) for value in profile.distinct]
    except ValueError:
        return False
    return bool(numbers) and len(numbers) >= DENSE_SHARE * (max(numbers) - min(numbers) + 1)


def _composite_keys(table: Table, profiles: list[Profile]) -> list[tuple[Profile, Profile, set[tuple[str, str]]]]:
    """Pairs of columns that never repeat together, for a file with no single-column key."""
    if any(profile.keylike and profile.unique for profile in profiles):
        return []
    columns = [profile for profile in profiles if profile.keylike and profile.blanks == 0][:MAX_PAIR_COLUMNS]
    tracking = {(a.index, b.index): set() for a, b in combinations(columns, 2)}
    if not tracking:
        return []
    records = _records(table)
    next(records, None)
    for record in records:
        for pair in list(tracking):
            value = tuple(record[i].strip() if i < len(record) else "" for i in pair)
            seen = tracking[pair]
            if not all(value) or value in seen:
                del tracking[pair]
            else:
                seen.add(value)
        if not tracking:
            break
    by_index = {profile.index: profile for profile in profiles}
    return [(by_index[a], by_index[b], seen) for (a, b), seen in tracking.items()]


def discover(
    tables: list[Table], cache: dict[str, list[Profile]] | None = None, low_cardinality: bool = False
) -> list[Join]:
    """Every join the data evidences between the tables, with its confidence and why.

    With `low_cardinality`, a column with fewer than MIN_DISTINCT distinct values is also
    considered, but only against a key of the same name and never above "needs you".
    """
    cache = {} if cache is None else cache
    for table in tables:
        if table.name not in cache:
            cache[table.name] = profile_table(table)
    parents = [p for table in tables for p in cache[table.name] if p.keylike and p.unique]
    normalised: dict[int, set[str]] = {}
    dense: dict[int, bool] = {}

    def is_dense(profile: Profile) -> bool:
        if id(profile) not in dense:
            dense[id(profile)] = _dense_run(profile)
        return dense[id(profile)]

    def normal_set(profile: Profile) -> set[str]:
        if id(profile) not in normalised:
            normalised[id(profile)] = {normal(value) for value in profile.distinct}
        return normalised[id(profile)]

    joins: list[Join] = []
    for table in tables:
        for child in cache[table.name]:
            few = len(child.distinct) < MIN_DISTINCT
            if not child.keylike or not child.distinct or (few and not low_cardinality):
                continue
            options = []
            for parent in parents:
                if parent.table == child.table or parent.kind != child.kind:
                    continue
                share = len(child.distinct & parent.distinct) / len(child.distinct)
                how = ""
                if share < PROPOSE:
                    loose = normal_set(child)
                    share = len(loose & normal_set(parent)) / len(loose) if loose else 0.0
                    if share < PROPOSE:
                        continue
                    how = child.drift or parent.drift or "values match only once case, spaces and leading zeros are ignored"
                if not how and child.shape != parent.shape:
                    continue
                if child.kind == "integer" and child.high is not None and parent.high and child.high < SPREAD_SHARE * parent.high:
                    continue
                named = same_name(child.column, parent.column, parent.table)
                if few and not named:
                    continue
                if not named and child.kind == "integer" and child.unique and is_dense(child) and is_dense(parent):
                    continue
                options.append((share, named, parent, how))
            if not options:
                continue
            pool = [option for option in options if option[1]] or options
            pool.sort(key=lambda option: (-option[0], not named_after(child.column, option[2].table),
                                          option[2].table, option[2].column))
            share, named, parent, how = pool[0]
            rivals = [f"{o[2].table}.{o[2].column}" for o in pool[1:] if share - o[0] < CLOSE]
            reasons = [f"{share:.1%} of {len(child.distinct)} distinct values are in {parent.table}.{parent.column}",
                       f"{parent.table}.{parent.column} never repeats, so each row matches at most one"]
            reasons.append("the column names agree" if named else "the column names differ")
            if not how:
                reasons.append(f"both hold {_described_shape(parent)}")
            if few:
                reasons.append(f"only {len(child.distinct)} distinct values, too few to rule out a match by chance")
            if how:
                reasons.append(f"matched only after normalising: {how}")
            if rivals:
                reasons.append(f"other keys fit about as well: {', '.join(rivals)}")
            certain = named and share >= CERTAIN and not how and not rivals and not few
            joins.append(Join(child.table, (child.column,), parent.table, (parent.column,),
                              "check" if certain else "needs you", share, len(child.distinct), named, how,
                              tuple(rivals), tuple(reasons)))

    for table in tables:
        for first, second, keys in _composite_keys(table, cache[table.name]):
            for other in tables:
                if other.name == table.name:
                    continue
                def fits(parent: Profile) -> list[Profile]:
                    return [p for p in cache[other.name] if p.keylike and p.kind == parent.kind and p.distinct
                            and len(p.distinct & parent.distinct) / len(p.distinct) >= PROPOSE]
                def carries_weight(child: Profile, parent: Profile) -> bool:
                    # A component with a handful of values adds almost nothing to the match, so the
                    # pair must earn it: enough values, or a name agreeing with the key it matches.
                    return len(child.distinct) >= MIN_DISTINCT or same_name(child.column, parent.column, table.name)

                pairs = [(x, y) for x in fits(first) for y in fits(second)
                         if x.index != y.index and carries_weight(x, first) and carries_weight(y, second)]
                if not pairs:
                    continue
                gathered = {(x.index, y.index): set() for x, y in pairs}
                records = _records(other)
                next(records, None)
                for record in records:
                    for pair, seen in gathered.items():
                        value = tuple(record[i].strip() if i < len(record) else "" for i in pair)
                        if all(value):
                            seen.add(value)
                by_index = {p.index: p for p in cache[other.name]}
                for (x, y), seen in gathered.items():
                    if len(seen) < MIN_DISTINCT:
                        continue
                    share = len(seen & keys) / len(seen)
                    if share < PROPOSE:
                        continue
                    child_x, child_y = by_index[x], by_index[y]
                    joins.append(Join(
                        other.name, (child_x.column, child_y.column), table.name, (first.column, second.column),
                        "needs you", share, len(seen),
                        same_name(child_x.column, first.column, table.name) and same_name(child_y.column, second.column, table.name),
                        "", (),
                        (f"{share:.1%} of {len(seen)} distinct pairs are in {table.name}.{first.column}+{second.column}",
                         "a two-column key, which a person should confirm"),
                    ))
    return joins


def lookup_verdict(source: Profile, other: Profile) -> str:
    """Whether `other` could supply values to `source` one row at a time."""
    if other.repeats:
        return "fan-out"
    mine, theirs = source.distinct, other.distinct
    if mine and theirs and max(len(mine & theirs) / len(mine), len(mine & theirs) / len(theirs)) >= PROPOSE:
        return "lookup"
    return "unrelated"

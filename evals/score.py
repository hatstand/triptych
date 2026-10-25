"""Scoring one extraction against its hand-checked answer, field by field, and adding scores up.

Reservations are paired with their expected counterparts by an optimal assignment (scipy), so order
doesn't matter and one bad reservation doesn't shift the rest. Each scored field then comes out as:

    correct       the right answer (or one of the case's `accept` alternatives)
    near          wrong, but usefully so: the right day at the wrong time, a day out at the right
                  time, another airport in the same city
    wrong         a different answer
    missed        expected a value, got null
    hallucinated  expected null, got a value
    na            null and null: not counted

Besides what the model said, it scores what the app makes of it: start_tz/end_tz after enrich.py's
normalise and fill_timezones have run over both sides, since that's what lands in the calendar.
"""

import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from scipy.optimize import linear_sum_assignment

from evals.cases import Expected, ExpectedReservation
from triptych import enrich, places
from triptych.models import ExtractedReservation, Extraction, Reservation


class Outcome(StrEnum):
    CORRECT = "correct"
    NEAR = "near"
    WRONG = "wrong"
    MISSED = "missed"
    HALLUCINATED = "hallucinated"
    NA = "na"


TEXT = ("city", "start_city")
CODES = ("confirmation_code", "flight_number", "country_code", "start_country_code")
TIMES = ("start_local", "end_local")
AIRPORTS = ("start_iata", "end_iata")
ENRICHED = ("start_tz", "end_tz")
FIELDS = ("kind", *TIMES, *AIRPORTS, *TEXT, *CODES, "timezone", *ENRICHED)
"""Every scored field, in report order."""
KEY = ("kind", *TIMES, *AIRPORTS, "city", "confirmation_code", "flight_number", *ENRICHED)
"""The fields a reservation needs right to land on the calendar correctly: all of them for a case to
count as fully correct. Country codes and the model's own timezone feed the enriched zones."""


class FieldScore(BaseModel):
    field: str
    outcome: Outcome
    expected: str | None = None
    predicted: str | None = None
    note: str | None = None


class PairScore(BaseModel):
    expected: int
    """Index into the expected reservations."""
    predicted: int
    title: str
    fields: list[FieldScore]

    @property
    def fully_correct(self) -> bool:
        return all(f.outcome in (Outcome.CORRECT, Outcome.NA) for f in self.fields if f.field in KEY)


class CaseScore(BaseModel):
    is_travel_document: bool
    predicted_travel: bool | None
    """None when the model gave no answer (an error)."""
    error: str | None = None
    expected_count: int
    predicted_count: int
    pairs: list[PairScore] = Field(default_factory=list)
    missed: list[int] = Field(default_factory=list)
    """Expected reservations with nothing extracted for them."""
    spurious: list[int] = Field(default_factory=list)
    """Extracted reservations matching nothing expected."""

    @property
    def classified(self) -> bool:
        return self.predicted_travel == self.is_travel_document

    @property
    def fully_correct(self) -> bool:
        return (
            self.error is None
            and self.classified
            and not self.missed
            and not self.spurious
            and all(p.fully_correct for p in self.pairs)
        )


# --- Comparing values ----------------------------------------------------------------------------


def fold(text: str) -> str:
    """Casefolded, accents stripped, whitespace collapsed: "São  Paulo" == "sao paulo"."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def code(text: str) -> str:
    return "".join(text.split()).upper()


@cache
def _offsets(tz: str) -> tuple[timedelta | None, ...]:
    """A zone's UTC offset through a year, to tell zones apart by what they do, not their name."""
    zone = ZoneInfo(tz)
    days = (datetime(2026, 1, 1) + timedelta(days=d, hours=12) for d in range(0, 365, 7))
    return tuple(day.replace(tzinfo=zone).utcoffset() for day in days)


def same_zone(a: str, b: str) -> bool:
    """Whether two zones keep the same time all year: Europe/Paris and Europe/Berlin do, so naming
    either is right; Europe/Lisbon and Europe/Madrid don't."""
    if a == b:
        return True
    if not (enrich.valid_tz(a) and enrich.valid_tz(b)):
        return False
    return _offsets(a) == _offsets(b)


def _compare_time(expected: str, predicted: str) -> tuple[Outcome, str | None]:
    e, p = enrich.parse_local(expected), enrich.parse_local(predicted)
    if p is None or e is None:
        return Outcome.WRONG, "unparseable"
    if e == p:
        return Outcome.CORRECT, None
    if e.date() == p.date():
        return Outcome.NEAR, "right day, wrong time"
    if abs(e - p) == timedelta(days=1):
        return Outcome.NEAR, "a day out"
    if e.time() == p.time() and e.replace(year=p.year) == p:
        return Outcome.NEAR, "wrong year"
    return Outcome.WRONG, None


def compare(name: str, expected: Any, predicted: Any, accept: list[str | None] | None = None) -> FieldScore:
    """Score one field. `accept` holds other right answers, null among them if null is fine."""
    e = None if expected is None else str(expected)
    p = None if predicted is None or predicted == "" else str(predicted)
    score = FieldScore(field=name, outcome=Outcome.NA, expected=e, predicted=p)
    answers = [e, *(accept or [])]
    if p is None:
        if any(a is None for a in answers):
            score.outcome = Outcome.NA if e is None else Outcome.CORRECT
        else:
            score.outcome = Outcome.MISSED
        return score
    right = [a for a in answers if a is not None]
    if not right:
        score.outcome = Outcome.HALLUCINATED
        return score
    if name in TEXT:
        ok = fold(p) in {fold(a) for a in right}
    elif name in CODES or name == "kind":
        ok = code(p) in {code(a) for a in right}
    elif name in ("timezone", *ENRICHED):
        ok = any(same_zone(a, p) for a in right)
    elif name in AIRPORTS:
        ok = code(p) in {code(a) for a in right}
        if not ok and any(places.same_city(a, p) for a in right):
            score.outcome, score.note = Outcome.NEAR, "same city"
            return score
    elif name in TIMES:
        best = min((_compare_time(a, p) for a in right), key=lambda r: list(Outcome).index(r[0]))
        score.outcome, score.note = best
        return score
    else:
        ok = p in right
    score.outcome = Outcome.CORRECT if ok else Outcome.WRONG
    return score


# --- Pairing ---------------------------------------------------------------------------------------


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b and code(a) == code(b))


def similarity(e: ExpectedReservation, p: ExtractedReservation) -> float:
    """How alike two reservations look, for pairing: the kind and start matter most."""
    start_e, start_p = enrich.parse_local(e.start_local), enrich.parse_local(p.start_local)
    s = 0.0
    s += 2 if e.kind == p.kind else 0
    if start_e and start_p:
        s += 2 if start_e.date() == start_p.date() else 0
        s += 1 if start_e == start_p else 0
    s += sum(
        _same(getattr(e, f), getattr(p, f))
        for f in ("confirmation_code", "flight_number", "start_iata", "end_iata")
    )
    s += 1 if e.city and p.city and fold(e.city) == fold(p.city) else 0
    return s


MIN_SIMILARITY = 2
"""Less alike than this (not even the same kind, or the same start day), and it's a miss plus a
spurious reservation rather than one with every field wrong."""


def pair(expected: list[ExpectedReservation], predicted: list[ExtractedReservation]) -> list[tuple[int, int]]:
    if not expected or not predicted:
        return []
    cost = [[-similarity(e, p) for p in predicted] for e in expected]
    rows, cols = linear_sum_assignment(cost)
    return [(int(i), int(j)) for i, j in zip(rows, cols, strict=True) if -cost[i][j] >= MIN_SIMILARITY]


# --- Enrichment ------------------------------------------------------------------------------------


def as_extracted(e: ExpectedReservation) -> ExtractedReservation:
    data = e.model_dump(exclude={"accept"})
    data["title"] = e.title or e.kind.value
    return ExtractedReservation.model_validate(data)


def enriched(rs: list[ExtractedReservation]) -> list[Reservation | None]:
    """What the app would store for each reservation, timezones filled in across the document
    (None for one it would drop, with no start time it can read)."""
    out = [enrich.normalise(r, reservation_id=str(i), ingest_id="eval") for i, r in enumerate(rs)]
    enrich.fill_timezones(sorted((r for r in out if r), key=lambda r: r.start_local))
    return out


# --- A case ----------------------------------------------------------------------------------------


def score_case(expected: Expected, extraction: Extraction | None, error: str | None = None) -> CaseScore:
    want = expected.reservations if expected.is_travel_document else []
    got = extraction.reservations if extraction else []
    score = CaseScore(
        is_travel_document=expected.is_travel_document,
        predicted_travel=extraction.is_travel_document if extraction else None,
        error=error,
        expected_count=len(want),
        predicted_count=len(got),
    )
    pairs = pair(want, got)
    want_enriched = enriched([as_extracted(e) for e in want])
    got_enriched = enriched(got)
    for i, j in pairs:
        e, p = want[i], got[j]
        fields = [
            compare(f, getattr(e, f), getattr(p, f), e.accept.get(f)) for f in FIELDS if f not in ENRICHED
        ]
        we, ge = want_enriched[i], got_enriched[j]
        for f in ENRICHED:
            fs = compare(f, we and getattr(we, f), ge and getattr(ge, f), e.accept.get(f))
            if ge is None:
                fs.note = "start time unreadable: the app would drop it"
            fields.append(fs)
        score.pairs.append(PairScore(expected=i, predicted=j, title=e.title or p.title, fields=fields))
    score.missed = sorted(set(range(len(want))) - {i for i, _ in pairs})
    score.spurious = sorted(set(range(len(got))) - {j for _, j in pairs})
    return score


# --- Adding up -------------------------------------------------------------------------------------


@dataclass
class Tally:
    """Scores over a set of cases (a run, or one tag of it)."""

    cases: int = 0
    classified: int = 0
    fully_correct: int = 0
    errors: int = 0
    expected: int = 0
    predicted: int = 0
    paired: int = 0
    fields: dict[str, Counter[Outcome]] = field(default_factory=dict)

    def add(self, s: CaseScore) -> None:
        self.cases += 1
        self.classified += s.classified
        self.fully_correct += s.fully_correct
        self.errors += s.error is not None
        self.expected += s.expected_count
        self.predicted += s.predicted_count
        self.paired += len(s.pairs)
        for p in s.pairs:
            for f in p.fields:
                self.fields.setdefault(f.field, Counter())[f.outcome] += 1

    @staticmethod
    def rate(n: int, d: int) -> float | None:
        return n / d if d else None

    @property
    def recall(self) -> float | None:
        return self.rate(self.paired, self.expected)

    @property
    def precision(self) -> float | None:
        return self.rate(self.paired, self.predicted)

    def accuracy(self, name: str) -> float | None:
        """Of the paired reservations where the field has an answer on either side, how often it's right."""
        c = self.fields.get(name, Counter())
        return self.rate(c[Outcome.CORRECT], c.total() - c[Outcome.NA])

    def summary(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "fully_correct": self.rate(self.fully_correct, self.cases),
            "classification": self.rate(self.classified, self.cases),
            "errors": self.errors,
            "reservation_recall": self.recall,
            "reservation_precision": self.precision,
            "fields": {
                f: {"accuracy": self.accuracy(f), **{o.value: n for o, n in self.fields[f].items()}}
                for f in FIELDS
                if f in self.fields
            },
        }


def tally(scores: list[tuple[list[str], CaseScore]]) -> dict[str, Tally]:
    """Totals overall ("all") and per tag, from (tags, score) per case."""
    out: dict[str, Tally] = {"all": Tally()}
    for tags, s in scores:
        out["all"].add(s)
        for t in tags:
            out.setdefault(t, Tally()).add(s)
    return out

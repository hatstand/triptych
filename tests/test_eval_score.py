"""The extraction eval's own machinery: case files, field scoring and pairing, and syncing from the
eval bucket. None of it needs a model; the one test that does is marked llm and skipped by default."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from evals import score
from evals.cases import Expected, ExpectedReservation, load_cases, read_expected, write_expected
from evals.score import Outcome, compare
from evals.sync import sync
from triptych.models import ExtractedReservation, Extraction, Kind

SMOKE = Path(__file__).resolve().parents[1] / "evals" / "smoke"


def as_extraction(expected: Expected) -> Extraction:
    """The answer a perfect model would give."""
    return Extraction(
        is_travel_document=expected.is_travel_document,
        reservations=[score.as_extracted(r) for r in expected.reservations],
    )


def smoke(case_id: str) -> Expected:
    return read_expected(SMOKE / "cases" / case_id / "expected.yaml")


def test_smoke_set_loads_with_its_inputs() -> None:
    cases = load_cases(SMOKE)
    assert [c.id for c in cases] == [
        "01-flight-lisbon", "02-hotel-lisbon", "03-dinner-lisbon", "04-rail-paris", "05-newsletter"
    ]  # fmt: skip
    assert all(c.input.exists() and c.content_type == "message/rfc822" for c in cases)
    assert [c.id for c in load_cases(SMOKE, tags=("rail",))] == ["04-rail-paris"]


def test_a_perfect_answer_is_fully_correct() -> None:
    for case in load_cases(SMOKE):
        s = score.score_case(case.expected, as_extraction(case.expected))
        assert s.fully_correct, case.id
        assert not s.missed and not s.spurious


def test_expected_round_trips_through_yaml(tmp_path: Path) -> None:
    expected = smoke("01-flight-lisbon")
    path = tmp_path / "cases" / "x" / "expected.yaml"
    write_expected(path, expected)
    assert path.read_text().startswith("# yaml-language-server: $schema=../../expected.schema.json\n")
    assert read_expected(path) == expected


def test_drafts_from_before_the_schema_change_still_load() -> None:
    old = {
        "kind": "flight",
        "start_local": "2026-11-12T15:40",
        "flight_number": "AQ 236",
        "timezone": "Europe/Lisbon",
    }
    r = ExpectedReservation.model_validate(old)
    assert (r.service_number, r.end_timezone) == ("AQ 236", "Europe/Lisbon")


@pytest.mark.parametrize(
    ("field", "expected", "predicted", "outcome"),
    [
        ("city", "São Paulo", "sao  paulo", Outcome.CORRECT),
        ("city", "Lisbon", "Porto", Outcome.WRONG),
        ("confirmation_code", "QZ7K2M", "qz7k 2m", Outcome.CORRECT),
        ("service_number", "AQ 236", "AQ236", Outcome.CORRECT),
        ("confirmation_code", "QZ7K2M", None, Outcome.MISSED),
        ("confirmation_code", None, "QZ7K2M", Outcome.HALLUCINATED),
        ("seat", None, "", Outcome.NA),
        ("start_iata", "LHR", "LGW", Outcome.NEAR),
        ("start_iata", "LHR", "LIS", Outcome.WRONG),
        ("end_timezone", "Europe/Paris", "Europe/Berlin", Outcome.CORRECT),
        ("end_timezone", "Europe/Lisbon", "Europe/Madrid", Outcome.WRONG),
        ("end_timezone", "Europe/Lisbon", "Lisbon", Outcome.WRONG),
        ("start_local", "2026-11-12T15:40", "2026-11-12 15:40", Outcome.CORRECT),
        ("start_local", "2026-11-12T15:40", "2026-11-12T03:40", Outcome.NEAR),
        ("start_local", "2026-11-13T11:25", "2026-11-12T11:25", Outcome.NEAR),
        ("start_local", "2026-11-12T15:40", "2025-11-12T15:40", Outcome.NEAR),
        ("start_local", "2026-11-12T15:40", "2026-12-12T15:40", Outcome.WRONG),
        ("start_local", "2026-11-12T15:40", "soon", Outcome.WRONG),
    ],
)
def test_compare(field: str, expected: str | None, predicted: str | None, outcome: Outcome) -> None:
    assert compare(field, expected, predicted).outcome is outcome


def test_accept_lists_other_right_answers_and_null() -> None:
    assert compare("city", "Lisbon", "Lisboa", ["Lisboa"]).outcome is Outcome.CORRECT
    assert compare("start_city", "Lisbon", None, [None]).outcome is Outcome.CORRECT
    assert compare("service_number", None, "9024", ["9024"]).outcome is Outcome.CORRECT
    assert compare("service_number", None, "9025", ["9024"]).outcome is Outcome.WRONG


def test_reservations_pair_regardless_of_order() -> None:
    expected = smoke("01-flight-lisbon")
    answer = as_extraction(expected)
    answer.reservations.reverse()
    answer.reservations[0].confirmation_code = None
    s = score.score_case(expected, answer)
    assert [(p.expected, p.predicted) for p in s.pairs] == [(0, 1), (1, 0)]
    outcomes = {f.field: f.outcome for f in s.pairs[1].fields}
    assert outcomes["confirmation_code"] is Outcome.MISSED
    assert not s.fully_correct


def test_unlike_reservations_are_a_miss_and_a_spurious_one() -> None:
    expected = smoke("03-dinner-lisbon")
    dinner = Extraction(
        is_travel_document=True,
        reservations=[
            ExtractedReservation(
                kind=Kind.ACTIVITY, title="Tour", start_local="2026-12-01T10:00", city="Porto"
            )
        ],
    )
    s = score.score_case(expected, dinner)
    assert (s.pairs, s.missed, s.spurious) == ([], [0], [0])


def test_no_answer_and_wrong_classification() -> None:
    s = score.score_case(smoke("02-hotel-lisbon"), None, "LlmError: timed out")
    assert s.predicted_travel is None and not s.classified and s.missed == [0]
    s = score.score_case(smoke("05-newsletter"), as_extraction(smoke("02-hotel-lisbon")))
    assert not s.classified and s.spurious == [0] and not s.fully_correct


def test_enriched_timezones_score_what_the_app_stores() -> None:
    # The model's own zone is wrong, but places.py knows Lisbon's: the calendar still comes out right.
    expected = smoke("02-hotel-lisbon")
    answer = as_extraction(expected)
    answer.reservations[0].start_timezone = answer.reservations[0].end_timezone = "Europe/Madrid"
    fields = {f.field: f for f in score.score_case(expected, answer).pairs[0].fields}
    assert fields["end_timezone"].outcome is Outcome.WRONG
    assert fields["start_tz"].outcome is Outcome.CORRECT
    # A train with no departure city or zone leaves from where it arrives (fill_timezones): wrong here.
    expected = smoke("04-rail-paris")
    answer = as_extraction(expected)
    train = answer.reservations[0]
    train.start_city = train.start_country_code = train.start_timezone = None
    fields = {f.field: f for f in score.score_case(expected, answer).pairs[0].fields}
    assert fields["start_tz"].outcome is Outcome.WRONG


def test_tally_by_tag() -> None:
    perfect = [
        (c.expected.tags, score.score_case(c.expected, as_extraction(c.expected))) for c in load_cases(SMOKE)
    ]
    perfect.append((["rail"], score.score_case(smoke("04-rail-paris"), None, "boom")))
    totals = score.tally(perfect)
    assert totals["all"].summary()["fully_correct"] == 5 / 6
    rail = totals["rail"].summary()
    assert (rail["cases"], rail["errors"], rail["reservation_recall"]) == (2, 1, 0.5)
    assert rail["fields"]["kind"]["accuracy"] == 1


@pytest.fixture
def bucket() -> Any:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="evals")
        yield s3


def put_case(
    s3: Any,
    ingest_id: str,
    output: dict[str, Any] | None,
    corrections: list[dict[str, Any]] | None = None,
    *,
    reclassified: bool = False,
) -> None:
    case = {
        "ingest_id": ingest_id, "source": "email", "content_type": "message/rfc822", "subject": "Your flight",
        "received_at": datetime(2026, 10, 25, 12, tzinfo=UTC).isoformat(), "images": 0,
        "run": {"model": "gemma4:26b", "prompt_version": "0123456789ab", "output": output},
        "corrections": corrections or [], "reclassified": reclassified,
    }  # fmt: skip
    s3.put_object(Bucket="evals", Key=f"inputs/{ingest_id}/original.eml", Body=b"From: a\r\n\r\nhi")
    s3.put_object(Bucket="evals", Key=f"inputs/{ingest_id}/case.json", Body=json.dumps(case).encode())


def test_sync_downloads_cases_and_drafts_new_ones(bucket: Any, tmp_path: Path) -> None:
    answer = as_extraction(smoke("03-dinner-lisbon")).model_dump(mode="json")
    put_case(bucket, "mail-1", answer)
    put_case(bucket, "mail-2", None)
    bucket.put_object(Bucket="evals", Key="inputs/mail-3/original.eml", Body=b"half copied")

    done = sync(tmp_path, bucket, "evals")
    assert (done.downloaded, done.drafted, done.incomplete) == (
        ["mail-1", "mail-2"],
        ["mail-1", "mail-2"],
        ["mail-3"],
    )
    draft = read_expected(tmp_path / "cases" / "mail-1" / "expected.yaml")
    assert draft.status == "draft" and draft.tags == ["email", "eml"]
    assert draft.reservations[0].confirmation_code == "TN-448190"
    assert "gemma4:26b" in (draft.notes or "")
    assert not read_expected(tmp_path / "cases" / "mail-2" / "expected.yaml").is_travel_document
    assert (tmp_path / ".gitignore").read_text() == "inputs/\nruns/\n"
    assert load_cases(tmp_path) == []
    assert len(load_cases(tmp_path, include_drafts=True)) == 2

    # Labels are never overwritten, and nothing is fetched twice.
    (tmp_path / "cases" / "mail-1" / "expected.yaml").write_text(
        (tmp_path / "cases" / "mail-1" / "expected.yaml")
        .read_text()
        .replace("status: draft", "status: verified")
    )
    again = sync(tmp_path, bucket, "evals")
    assert (again.downloaded, again.drafted) == ([], [])
    assert read_expected(tmp_path / "cases" / "mail-1" / "expected.yaml").status == "verified"


def two_flights() -> dict[str, Any]:
    flight = as_extraction(smoke("01-flight-lisbon")).reservations[0]
    back = flight.model_copy(update={"title": "Back", "start_local": "2026-11-20T18:00", "end_local": None})
    return Extraction(is_travel_document=True, reservations=[flight, back]).model_dump(mode="json")


def test_sync_drafts_from_the_owners_corrections(bucket: Any, tmp_path: Path) -> None:
    put_case(bucket, "mail-1", two_flights(), [
        {"reservation": 0, "edited": ["start_local", "end_location", "start_tz"],
         "fields": {"start_local": "2026-11-03T10:15:00", "end_location": "Porto (OPO)",
                    "start_tz": "Europe/Dublin"}},
        {"reservation": 1, "deleted": True},
        {"reservation": None, "edited": ["title"],
         "fields": {"kind": "lodging", "title": "Hotel Avenida", "start_local": "2026-11-03T15:00:00",
                    "end_local": None, "start_tz": "Europe/Lisbon", "city": "Lisbon", "travelers": []}},
    ])  # fmt: skip

    sync(tmp_path, bucket, "evals")
    draft = read_expected(tmp_path / "cases" / "mail-1" / "expected.yaml")
    flight, hotel = draft.reservations
    assert (flight.start_local, flight.end_location, flight.end_iata) == (
        "2026-11-03T10:15",
        "Porto (OPO)",
        "OPO",
    )
    assert flight.start_timezone == "Europe/Dublin"
    assert (hotel.kind, hotel.title, hotel.start_timezone) == (Kind.LODGING, "Hotel Avenida", "Europe/Lisbon")
    assert draft.status == "draft" and "corrected" in draft.tags
    assert "deleted Back" in (draft.notes or "")
    assert flight.owner_set == ["start_local", "end_location", "start_timezone", "end_iata"]
    assert set(hotel.owner_set) >= {"kind", "title", "start_local", "start_timezone", "city"}
    assert draft.partly_labelled and [c.id for c in load_cases(tmp_path)] == ["mail-1"]


def test_sync_drafts_one_its_owner_said_is_a_booking_as_travel(bucket: Any, tmp_path: Path) -> None:
    put_case(bucket, "mail-1", Extraction(is_travel_document=False).model_dump(mode="json"))
    sync(tmp_path, bucket, "evals")
    assert not read_expected(tmp_path / "cases" / "mail-1" / "expected.yaml").is_travel_document

    # Read again as a booking: its run is now the one that found the flights.
    put_case(bucket, "mail-1", two_flights(), reclassified=True)
    assert sync(tmp_path, bucket, "evals").redrafted == ["mail-1"]
    draft = read_expected(tmp_path / "cases" / "mail-1" / "expected.yaml")
    assert draft.is_travel_document and len(draft.reservations) == 2
    assert "corrected" in draft.tags and "said it's a booking" in (draft.notes or "")


def test_a_partly_labelled_draft_is_scored_on_what_its_owner_set() -> None:
    flight, back = (ExpectedReservation.model_validate(r) for r in two_flights()["reservations"])
    flight.owner_set = ["start_local", "start_timezone"]
    expected = Expected(
        received_at=datetime(2026, 10, 25, tzinfo=UTC), is_travel_document=True, reservations=[flight, back]
    )
    assert expected.partly_labelled
    model = score.as_extracted(flight)
    unchecked_wrong = model.model_copy(update={"city": "Porto", "confirmation_code": "ZZZ"})
    s = score.score_case(expected, Extraction(is_travel_document=True, reservations=[unchecked_wrong]))
    assert s.partial and s.fully_correct  # the return flight's missing, and city is the model's own
    [p] = s.pairs
    assert {f.field for f in p.fields if f.outcome != Outcome.NA} == {
        "start_local",
        "start_timezone",
        "start_tz",
    }

    late = model.model_copy(update={"start_local": "2026-11-03T11:00"})
    s = score.score_case(expected, Extraction(is_travel_document=True, reservations=[late]))
    assert not s.fully_correct
    totals = score.tally([(["corrected"], s)])["all"].summary()
    assert (totals["cases"], totals["partly_labelled"], totals["fields"]["start_local"]["accuracy"]) == (
        0,
        1,
        0,
    )
    assert "city" not in totals["fields"] or totals["fields"]["city"].get("wrong", 0) == 0


def test_sync_adds_what_the_owner_set_to_an_older_draft(bucket: Any, tmp_path: Path) -> None:
    put_case(bucket, "mail-1", two_flights(), [
        {"reservation": 0, "edited": ["seat"], "fields": {"seat": "12A"}},
    ])  # fmt: skip
    sync(tmp_path, bucket, "evals")
    path = tmp_path / "cases" / "mail-1" / "expected.yaml"
    older = read_expected(path)
    older.reservations[0].owner_set = []
    write_expected(path, older)  # as a sync from before owner_set wrote it

    assert sync(tmp_path, bucket, "evals").redrafted == ["mail-1"]
    assert read_expected(path).reservations[0].owner_set == ["seat"]
    assert sync(tmp_path, bucket, "evals").redrafted == []


def test_a_later_correction_redrafts_only_what_nobody_touched(bucket: Any, tmp_path: Path) -> None:
    put_case(bucket, "mail-1", two_flights())
    put_case(bucket, "mail-2", two_flights())
    sync(tmp_path, bucket, "evals")
    labelled = tmp_path / "cases" / "mail-2" / "expected.yaml"
    labelled.write_text(labelled.read_text().replace("status: draft", "status: verified"))

    deleted = [{"reservation": 1, "deleted": True}]
    put_case(bucket, "mail-1", two_flights(), deleted)
    put_case(bucket, "mail-2", two_flights(), deleted)
    again = sync(tmp_path, bucket, "evals")
    assert (again.redrafted, again.recorrected) == (["mail-1"], ["mail-2"])
    assert len(read_expected(tmp_path / "cases" / "mail-1" / "expected.yaml").reservations) == 1
    assert len(read_expected(labelled).reservations) == 2
    assert sync(tmp_path, bucket, "evals").redrafted == []


@pytest.mark.llm
def test_smoke_set_against_the_local_model(tmp_path: Path) -> None:
    """`pytest -m llm`: the public smoke set through the configured model on the local Ollama."""
    from evals.run import Variant, run
    from triptych.config import settings

    [out] = run(load_cases(SMOKE), [Variant(model=settings.llm_model, prompt="current")], tmp_path)
    summary = json.loads((out / "results.json").read_text())["summary"]["all"]
    assert summary["errors"] == 0
    assert summary["classification"] == 1
    assert summary["fully_correct"] >= 0.6


def test_labels_only_take_zones_extraction_can_write() -> None:
    zone = Expected.model_json_schema()["$defs"]["ExpectedReservation"]["properties"]["end_timezone"]
    assert "Asia/Shanghai" in zone["anyOf"][0]["enum"]
    ExpectedReservation(kind=Kind.LODGING, start_local="2026-10-15T15:00", end_timezone="Asia/Shanghai")
    old = ExpectedReservation(
        kind=Kind.LODGING,
        start_local="2026-10-15T15:00",
        start_timezone="Asia/Chongqing",
        end_timezone="Asia/Shanghai",
    )
    assert old.start_timezone == "Asia/Shanghai"  # an alias, from a label written before the list
    with pytest.raises(ValueError, match="isn't an IANA timezone"):
        ExpectedReservation(
            kind=Kind.FLIGHT, start_local="2026-10-15T15:00", start_timezone="Pacific/Wellington"
        )


def test_an_alias_the_trip_page_allows_drafts_as_its_canonical_zone(bucket: Any, tmp_path: Path) -> None:
    # The trip page takes any zone Python knows, aliases too; labels hold canonical ones.
    put_case(bucket, "mail-1", two_flights(), [
        {"reservation": 0, "edited": ["start_tz"], "fields": {"start_tz": "Europe/Kiev"}},
    ])  # fmt: skip
    sync(tmp_path, bucket, "evals")
    draft = read_expected(tmp_path / "cases" / "mail-1" / "expected.yaml")
    assert draft.reservations[0].start_timezone == "Europe/Kyiv"

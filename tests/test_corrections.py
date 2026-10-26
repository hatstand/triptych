"""Making the owner's trip-page corrections to an extraction run's answer."""

from datetime import UTC, datetime

from evals.sync import draft_from
from triptych import corrections
from triptych.corrections import Correction
from triptych.models import ExtractedReservation, Extraction, Kind


def answer() -> Extraction:
    flight = ExtractedReservation(
        kind=Kind.FLIGHT, title="BA 500 LHR→LIS", confirmation_code="ABC123",
        start_local="2026-11-03T09:00", start_iata="LHR", end_iata="LIS", end_location="Lisbon (LIS)",
        city="Lisbon", country_code="PT",
    )  # fmt: skip
    hotel = ExtractedReservation(
        kind=Kind.LODGING, title="Hotel Avenida", start_local="2026-11-03T15:00", city="Lisbon"
    )
    return Extraction(is_travel_document=True, reservations=[flight, hotel])


def test_edits_and_deletions_are_made_to_the_runs_answer() -> None:
    moved = {"end_location": "Porto (OPO)", "start_local": "2026-11-03T10:15:00", "start_tz": "Europe/Dublin"}
    fixed, changed = corrections.apply(
        answer(),
        [
            Correction(reservation=0, edited=["end_location", "start_local", "start_tz"], fields=moved),
            Correction(reservation=1, deleted=True),
        ],
    )
    [flight] = fixed.reservations
    assert (flight.start_local, flight.end_location, flight.end_iata) == (
        "2026-11-03T10:15",
        "Porto (OPO)",
        "OPO",
    )
    assert flight.start_timezone == "Europe/Dublin"
    assert flight.confirmation_code == "ABC123"  # untouched fields keep the model's answer
    assert changed == ["BA 500 LHR→LIS: end_location, start_local, start_tz", "deleted Hotel Avenida"]


def test_one_the_run_didnt_find_is_kept_and_a_kind_without_codes_loses_them() -> None:
    dinner = {"kind": "restaurant", "title": "Taberna", "start_local": "2026-11-03T20:00:00", "travelers": []}
    fixed, changed = corrections.apply(
        answer(),
        [
            Correction(reservation=0, edited=["kind"], fields={"kind": "ferry"}),
            Correction(reservation=None, edited=["title"], fields=dinner),
        ],
    )
    ferry, _, kept = fixed.reservations
    assert (ferry.kind, ferry.start_iata, ferry.end_iata) == (Kind.FERRY, None, None)
    assert (kept.kind, kept.title, kept.start_local) == (Kind.RESTAURANT, "Taberna", "2026-11-03T20:00")
    assert changed[-1] == "kept Taberna, which this run didn't find"


def test_corrections_to_a_run_with_no_answer() -> None:
    casa = {"kind": "lodging", "title": "Casa", "start_local": "2026-11-03T15:00:00"}
    fixed, _ = corrections.apply(None, [Correction(reservation=None, edited=["title"], fields=casa)])
    assert [r.title for r in fixed.reservations] == ["Casa"]


def test_a_zone_the_trip_page_allows_but_extraction_doesnt_drafts_as_unknown() -> None:
    # The trip page takes any zone Python knows, aliases too; labels only take canonical ones.
    case = {
        "received_at": datetime(2026, 10, 25, 12, tzinfo=UTC).isoformat(), "source": "email",
        "content_type": "message/rfc822",
        "run": {"model": "gemma4", "prompt_version": "x", "output": answer().model_dump(mode="json")},
        "corrections": [{"reservation": 1, "edited": ["start_tz"], "fields": {"start_tz": "Europe/Kiev"}}],
    }  # fmt: skip
    assert draft_from(case).reservations[1].start_timezone is None

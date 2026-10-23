"""Domain models shared by the Restate services, the web app and the SMTP gateway.

Everything that crosses a Restate boundary (handler input/output, ctx.run results, K/V state)
is a pydantic model so the SDK can (de)serialise it with type information.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Kind(StrEnum):
    FLIGHT = "flight"
    LODGING = "lodging"
    CAR = "car"
    RAIL = "rail"
    RESTAURANT = "restaurant"
    ACTIVITY = "activity"
    OTHER = "other"


# --- What the LLM produces -------------------------------------------------------------------
# Kept deliberately flat with string datetimes: small local models follow flat schemas far more
# reliably than nested ones, and we validate/normalise afterwards in enrich.py.


def _all_keys_required(schema: dict[str, Any]) -> None:
    # Ollama's grammar lets the model *skip* optional keys, and it does so eagerly (dropping
    # confirmation codes, addresses…). Requiring every key, nullable, makes it consider each one.
    schema["required"] = list(schema.get("properties", {}))


class ExtractedReservation(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_keys_required)

    kind: Kind
    title: str = Field(
        description="Short title: flight number + route for transport ('AQ 236 SFO→LIS'), the property "
        "name for lodging ('Casa do Miradouro'), the venue name otherwise"
    )
    provider: str | None = Field(
        None, description="Company: airline, hotel/property name, rental company, rail operator, restaurant"
    )
    confirmation_code: str | None = Field(
        None, description="Booking reference / confirmation number / PNR / booking ID, exactly as written"
    )
    start_local: str = Field(description="Local start time at the start location, YYYY-MM-DDTHH:MM")
    end_local: str | None = Field(None, description="Local end time at the end location, YYYY-MM-DDTHH:MM")
    start_location: str | None = Field(None, description="Departure airport/station, pickup point or venue")
    end_location: str | None = Field(None, description="Arrival airport/station or drop-off point")
    start_iata: str | None = Field(None, description="3-letter IATA code of departure airport (flights)")
    end_iata: str | None = Field(None, description="3-letter IATA code of arrival airport (flights)")
    address: str | None = Field(None, description="Street address of the hotel/venue/pickup, if given")
    city: str | None = Field(
        None, description="City in English where this takes place (arrival city for transport)"
    )
    timezone: str | None = Field(None, description="IANA timezone of the city, e.g. Europe/Lisbon")
    flight_number: str | None = None
    seat: str | None = None
    travelers: list[str] = Field(default_factory=list)
    notes: str | None = Field(None, description="Other useful facts: cabin, room type, party size…")


class Extraction(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_keys_required)

    is_travel_document: bool
    reservations: list[ExtractedReservation] = Field(default_factory=list)


# --- Normalised domain -----------------------------------------------------------------------


class Reservation(BaseModel):
    id: str
    kind: Kind
    title: str
    provider: str | None = None
    confirmation_code: str | None = None
    start_local: datetime
    start_tz: str | None = None
    end_local: datetime | None = None
    end_tz: str | None = None
    start_location: str | None = None
    end_location: str | None = None
    address: str | None = None
    city: str | None = None
    flight_number: str | None = None
    seat: str | None = None
    travelers: list[str] = Field(default_factory=list)
    notes: str | None = None
    source_ingest_id: str | None = None

    @property
    def start_date(self) -> date:
        return self.start_local.date()

    @property
    def end_date(self) -> date:
        return (self.end_local or self.start_local).date()


class TripSummary(BaseModel):
    id: str
    name: str
    destination: str | None
    start_date: date
    end_date: date
    reservation_count: int


class Trip(BaseModel):
    id: str
    owner: str
    name: str
    destination: str | None = None
    reservations: list[Reservation] = Field(default_factory=list)

    def summary(self) -> TripSummary:
        rs = self.reservations
        return TripSummary(
            id=self.id,
            name=self.name,
            destination=self.destination,
            start_date=min(r.start_date for r in rs) if rs else date.today(),
            end_date=max(r.end_date for r in rs) if rs else date.today(),
            reservation_count=len(rs),
        )


# --- Ingestion -------------------------------------------------------------------------------

IngestSource = Literal["upload", "paste", "email"]
IngestState = Literal["queued", "parsing", "extracting", "filing", "done", "no_travel", "failed"]


class IngestRequest(BaseModel):
    user: str
    source: IngestSource
    subject: str | None = None
    filename: str | None = None
    content_type: str = "text/plain"
    data_b64: str
    received_at: datetime


class ParsedDocument(BaseModel):
    subject: str | None
    text: str
    images_b64: list[str] = Field(default_factory=list)


class InboxItem(BaseModel):
    ingest_id: str
    source: IngestSource
    label: str
    received_at: datetime
    state: IngestState
    message: str | None = None
    trip_ids: list[str] = Field(default_factory=list)


class Notification(BaseModel):
    id: str
    at: datetime
    trip_id: str | None
    text: str


# --- Handler payloads ------------------------------------------------------------------------


class FileRequest(BaseModel):
    ingest_id: str
    reservations: list[Reservation]


class FileResult(BaseModel):
    trip_ids: list[str]


class AddToTrip(BaseModel):
    owner: str
    name: str
    destination: str | None
    reservations: list[Reservation]


class RemoveReservation(BaseModel):
    trip_id: str
    reservation_id: str


class RenameTrip(BaseModel):
    trip_id: str
    name: str


class Reminder(BaseModel):
    reservation_id: str
    expected_start: datetime
    text: str


class Overview(BaseModel):
    trips: list[TripSummary]
    inbox: list[InboxItem]
    notifications: list[Notification]


class LlmExtractRequest(BaseModel):
    document: ParsedDocument
    received_at: datetime


class LlmAskRequest(BaseModel):
    context: str
    question: str


class Answer(BaseModel):
    text: str


class Question(BaseModel):
    text: str

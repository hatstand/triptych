"""Domain models shared by the Restate services, the web app and the SMTP gateway.

Everything that crosses a Restate boundary (handler input/output, ctx.run results, K/V state)
is a pydantic model so the SDK can (de)serialise it with type information.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AliasChoices, BaseModel, ConfigDict, Field, WithJsonSchema

from triptych import places


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


def _known_country(code: str | None) -> str | None:
    code = (code or "").strip().upper()
    return code if code in places.country_codes() else None


def _country_code(description: str) -> Any:
    """An ISO 3166-1 alpha-2 code of a country we have places in, or null. The schema lists the
    codes, so Ollama's grammar only lets the model write one of them; anything else read back (a
    truncated reply) becomes null."""
    return Annotated[
        str | None,
        AfterValidator(_known_country),
        WithJsonSchema(
            {"anyOf": [{"type": "string", "enum": list(places.country_codes())}, {"type": "null"}],
             "description": description}
        ),
    ]  # fmt: skip


CityCountry = _country_code("ISO 3166-1 alpha-2 code of the country `city` is in, e.g. CN")
StartCountry = _country_code("ISO 3166-1 alpha-2 code of the country `start_city` is in, e.g. GB")


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
    country_code: CityCountry = None
    start_city: str | None = Field(None, description="City in English the transport departs from")
    start_country_code: StartCountry = None
    start_timezone: str | None = Field(
        None, description="IANA timezone where it starts (start_city for transport), e.g. Europe/London"
    )
    end_timezone: str | None = Field(
        None,
        # Extraction runs stored before there were two only have the city's, as timezone.
        validation_alias=AliasChoices("end_timezone", "timezone"),
        description="IANA timezone where it ends (city), e.g. Asia/Shanghai",
    )
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
    source_run_id: str | None = None
    """The extraction run (db extraction_run) this came from."""

    @property
    def start_date(self) -> date:
        return self.start_local.date()

    @property
    def end_date(self) -> date:
        return (self.end_local or self.start_local).date()


class PhotoCredit(BaseModel):
    """Who to credit for a destination photo, and under what licence (Commons licences require it)."""

    file: str
    """The Commons file name, "File:Lisbon montage.jpg"."""
    page_url: str
    """The file's page on Commons, which has the full licence and attribution."""
    article_url: str | None = None
    artist: str | None = None
    licence: str | None = None
    licence_url: str | None = None


DEFAULT_FOCUS = "50% 35%"
"""Where to crop a photo whose subject isn't known: a little above the middle, since a view's
foreground (water, a street, a bridge) is usually at the bottom and its landmarks' tops above."""


class Photo(PhotoCredit):
    version: str
    """Changes when the trip's photo does; goes in the image URL so browsers can cache it for good."""
    aspect: float | None = None
    """The image's width / height."""
    subject: list[float] | None = None
    """The main subject's box, [left, top, right, bottom] as fractions of the image."""

    def position(self, frame: float) -> str:
        """The CSS object-position that keeps the subject in view when the photo fills (object-fit:
        cover) a box of aspect `frame` (width / height): centred on it if it fits, else its top or
        middle kept, since the top of a tower matters more than its base."""
        if not self.subject or not self.aspect:
            return DEFAULT_FOCUS
        left, top, right, bottom = self.subject
        if self.aspect > frame:  # cropped at the sides; the visible width is `seen` of the image's
            seen = frame / self.aspect
            start = (left + right) / 2 - seen / 2
            return f"{_percent(start, seen)} 50%"
        seen = self.aspect / frame  # cropped top and bottom
        start = (top + bottom) / 2 - seen / 2 if bottom - top <= seen else top
        return f"50% {_percent(start, seen)}"


def _percent(start: float, seen: float) -> str:
    """object-position for a crop showing [start, start + seen] of an axis (fractions of the image)."""
    if seen >= 1:
        return "50%"
    return f"{round(min(max(start, 0), 1 - seen) / (1 - seen) * 100, 1)}%"


class TripSummary(BaseModel):
    id: str
    name: str
    destination: str | None
    start_date: date
    end_date: date
    reservation_count: int
    photo: Photo | None = None


class Trip(BaseModel):
    id: str
    owner: str
    name: str
    destination: str | None = None
    reservations: list[Reservation] = Field(default_factory=list)
    photo: Photo | None = None

    def summary(self) -> TripSummary:
        rs = self.reservations
        return TripSummary(
            id=self.id,
            name=self.name,
            destination=self.destination,
            start_date=min(r.start_date for r in rs) if rs else date.today(),
            end_date=max(r.end_date for r in rs) if rs else date.today(),
            reservation_count=len(rs),
            photo=self.photo,
        )


# --- Ingestion -------------------------------------------------------------------------------

IngestSource = Literal["upload", "paste", "email"]
IngestState = Literal["queued", "parsing", "extracting", "filing", "done", "no_travel", "failed"]


class IngestRequest(BaseModel):
    user: str  # app_user id
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


class RawInput(BaseModel):
    """An original input as it was received, kept so it can be reprocessed (inputs.py)."""

    id: str
    owner: str
    sha256: bytes
    object_key: str
    size: int
    source: IngestSource
    content_type: str
    filename: str | None = None
    received_at: datetime


ExtractionPurpose = Literal["ingest", "reprocess", "shadow", "eval"]


class ExtractionRun(BaseModel):
    """One extraction of an ingest: what ran, on what, and what came back (or why it didn't)."""

    id: str
    ingest_id: str
    owner: str
    input_id: str | None
    model: str
    model_digest: str | None
    prompt_version: str
    code_version: str
    params: dict[str, Any]
    purpose: ExtractionPurpose
    output: Extraction | None = None
    error: str | None = None


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


class ReconcileRequest(BaseModel):
    ingest_id: str
    run_id: str
    reservations: list[Reservation]


ReprocessMode = Literal["dry_run", "apply", "shadow"]


class ReprocessRequest(BaseModel):
    """Extract an ingest again (`model`) or re-derive it from a stored run (`from_run_id`), then
    work out what changes; `apply` also makes them. `shadow` is a dry run recorded as a shadow run."""

    batch_id: str
    ingest_id: str
    mode: ReprocessMode
    model: str | None = None
    from_run_id: str | None = None

    @property
    def key(self) -> str:
        """The Reprocess workflow's key, and its db reprocess row's id."""
        return f"{self.batch_id}:{self.ingest_id}"


class FileResult(BaseModel):
    trip_ids: list[str]


class TripRef(BaseModel):
    trip_id: str


class RemoveReservation(BaseModel):
    trip_id: str
    reservation_id: str


class RenameTrip(BaseModel):
    trip_id: str
    name: str


class PhotoRequest(BaseModel):
    place: str


class PhotoChoice(PhotoCredit):
    """The image picked for a place, before it is downloaded."""

    image_url: str


class PhotoSearch(BaseModel):
    choice: PhotoChoice | None = None


class SubjectBox(BaseModel):
    """What the model says the photo's main subject is and where (percentages of the image)."""

    model_config = ConfigDict(json_schema_extra=_all_keys_required)

    subject: str = Field(description="What the main subject is, e.g. 'Oriental Pearl Tower', 'Old town'")
    left: float = Field(description="Left edge of the subject, percent of image width from the left")
    top: float = Field(description="Top edge of the subject, percent of image height from the top")
    right: float = Field(description="Right edge of the subject, percent of image width from the left")
    bottom: float = Field(description="Bottom edge of the subject, percent of image height from the top")


class PhotoSubject(BaseModel):
    aspect: float
    box: list[float] | None = None
    """[left, top, right, bottom] as fractions, or None if the model's answer made no sense."""


class Reminder(BaseModel):
    owner: str
    trip_id: str
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
    model: str | None = None
    """An Ollama model other than LLM_MODEL (reprocessing, shadow runs)."""


class LlmSubjectRequest(BaseModel):
    place: str
    image_b64: str


class LlmAskRequest(BaseModel):
    context: str
    question: str


class Answer(BaseModel):
    text: str


class AskTrip(BaseModel):
    owner: str
    trip_id: str
    question: str

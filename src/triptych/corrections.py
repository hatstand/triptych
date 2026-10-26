"""What an ingest's owner corrected on the trip page, against what its extraction run answered.

`find` pairs the run's reservations with the ones the owner has now, the way a reprocess pairs them
(db.pair_up), and lists each edit and deletion. `apply` makes those corrections to the run's answer,
giving the answer the owner would have wanted: the eval set drafts labels from it (evals/sync.py),
and it is what a worked example of that ingest teaches.
"""

from typing import Any

from pydantic import BaseModel, Field

from triptych import db, enrich
from triptych.enrich import CODED
from triptych.journeys import iata_of
from triptych.models import ExtractedReservation, Extraction, Reservation, ReservationFields


class Correction(BaseModel):
    """What the owner changed about one reservation from the ingest's run (case.json corrections)."""

    reservation: int | None
    """Its index in the run's output reservations; None for one of theirs the run doesn't have (an
    earlier run's, say), which `fields` then gives in full."""
    deleted: bool = False
    edited: list[str] = Field(default_factory=list)
    """The fields they corrected (ReservationFields names)."""
    fields: dict[str, Any] = Field(default_factory=dict)
    """Those fields as they set them."""


def find(
    ingest_id: str,
    output: Extraction | None,
    current: list[Reservation],
    keys: dict[str, str],
    dismissals: list[db.Dismissal],
) -> list[Correction]:
    """The owner's edits and deletions against the run's `output`. `current` is what they have
    from the ingest now, `keys` those reservations' stored dedupe keys and `dismissals` the ones
    they deleted."""
    made = [
        (i, r)
        for i, x in enumerate(output.reservations if output else [])
        if (r := enrich.normalise(x, reservation_id=f"run-{i}", ingest_id=ingest_id)) is not None
    ]
    match = db.pair_up(current, [r for _, r in made], keys)
    out = []
    for j, (i, made_r) in enumerate(made):
        if (r := match.get(j)) is not None:
            if r.edited_fields:
                fields = r.model_dump(mode="json", include=set(r.edited_fields))
                out.append(Correction(reservation=i, edited=r.edited_fields, fields=fields))
        elif any(db.dismissed(made_r, d) for d in dismissals):
            out.append(Correction(reservation=i, deleted=True))
    paired = {r.id for r in match.values()}
    for r in current:
        if r.id not in paired and r.edited_fields:
            fields = r.model_dump(mode="json", include=set(ReservationFields.model_fields))
            out.append(Correction(reservation=None, edited=r.edited_fields, fields=fields))
    return out


# The trip page's field names (ReservationFields) where extraction's differ.
RENAMED = {"start_tz": "start_timezone", "end_tz": "end_timezone"}


def as_extracted(fields: dict[str, Any]) -> dict[str, Any]:
    """Reservation fields as the owner set them (JSON, as Correction has them), in extraction's
    terms. Fields extraction doesn't have are left out."""
    out = {}
    for name, value in fields.items():
        if name in ("start_local", "end_local") and value:
            value = value[:16]  # YYYY-MM-DDTHH:MM
        out[RENAMED.get(name, name)] = value
    return {k: v for k, v in out.items() if k in ExtractedReservation.model_fields}


def corrected(x: ExtractedReservation, c: Correction) -> ExtractedReservation:
    fixed = ExtractedReservation.model_validate({**x.model_dump(), **as_extracted(c.fields)})
    # A location the owner corrected to 'Paris (CDG)' corrects the airport too.
    for side in ("start", "end"):
        if (code := iata_of(c.fields.get(f"{side}_location"))) is not None:
            setattr(fixed, f"{side}_iata", code)
    if fixed.kind not in CODED:
        fixed.start_iata = fixed.end_iata = None
    return fixed


def apply(output: Extraction | None, corrections: list[Correction]) -> tuple[Extraction, list[str]]:
    """The run's answer as the owner corrected it, and what they changed (for notes)."""
    output = output or Extraction(is_travel_document=False, reservations=[])
    by_index = {c.reservation: c for c in corrections if c.reservation is not None}
    kept, changed = [], []
    for i, x in enumerate(output.reservations):
        name = x.title or x.kind.value
        if (c := by_index.get(i)) is None:
            kept.append(x)
        elif c.deleted:
            changed.append(f"deleted {name}")
        else:
            kept.append(corrected(x, c))
            changed.append(f"{name}: {', '.join(c.edited)}")
    for c in corrections:
        if c.reservation is None:
            added = ExtractedReservation.model_validate(as_extracted(c.fields))
            kept.append(added)
            changed.append(f"kept {added.title or added.kind.value}, which this run didn't find")
    return output.model_copy(update={"reservations": kept}), changed

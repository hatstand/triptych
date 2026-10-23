"""Regenerate src/triptych/data/places.tsv from OpenTravelData's points of reference.

OPTD (CC BY 4.0, https://github.com/opentraveldata/opentraveldata) maps each IATA location code
to its IATA *city* code(s): LHR, LGW and the St Pancras station QQS are all LON; JFK and EWR are
both NYC. That is what lets a journey treat a change of airport within a city as a connection.
We keep current airports and rail stations only.

    uv run python scripts/update_places.py [path/to/optd_por_public.csv]
"""

import csv
import io
import sys
import urllib.request
from pathlib import Path

URL = (
    "https://raw.githubusercontent.com/opentraveldata/opentraveldata/master/"
    "opentraveldata/optd_por_public.csv"
)
OUT = Path(__file__).resolve().parent.parent / "src" / "triptych" / "data" / "places.tsv"
# OPTD location types: A airport, R rail station (combined with C when the code is also a city's).
KINDS = {"A": "airport", "R": "rail"}
COLUMNS = ["iata", "kind", "name", "city_codes", "city", "country_code", "country", "timezone"]


def kind_of(location_type: str) -> str | None:
    return next((k for t, k in KINDS.items() if t in location_type), None)


def city_name(iata: str, codes: list[str], names: list[str]) -> str:
    """The city to show: the one sharing the location's own code (EWR -> Newark), else the first."""
    pairs = dict(zip(codes, names, strict=False))
    return pairs.get(iata) or names[0]


def main() -> None:
    if len(sys.argv) > 1:
        text = Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        with urllib.request.urlopen(URL) as r:
            text = r.read().decode("utf-8")

    best: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="^"):
        iata, kind = row["iata_code"], kind_of(row["location_type"])
        if len(iata) != 3 or kind is None or row["date_until"]:
            continue
        # One row per code; an airport wins over a station that shares its code.
        if iata in best and best[iata]["kind"] == "airport":
            continue
        codes = row["city_code_list"].split(",")
        names = row["city_name_list"].split("=")
        best[iata] = {
            "iata": iata,
            "kind": kind,
            "name": row["name"],
            "city_codes": ",".join(codes),
            "city": city_name(iata, codes, names),
            "country_code": row["country_code"],
            "country": row["country_name"],
            "timezone": row["timezone"],
        }

    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(best[k] for k in sorted(best))
    print(f"wrote {len(best)} places to {OUT}")


if __name__ == "__main__":
    main()

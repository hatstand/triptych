# Bundled data

`places.tsv` is an extract of the [OpenTravelData](https://github.com/opentraveldata/opentraveldata)
points of reference (`optd_por_public.csv`), © OpenTravelData contributors, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Changes: only current airports and
rail stations, reduced to code, name, IATA city codes, city, country and timezone.

Regenerate with `uv run python scripts/update_places.py`.

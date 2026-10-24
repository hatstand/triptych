import json
import re
from datetime import datetime
from pathlib import Path

from triptych.models import Kind, Reservation, Trip
from triptych.web import app as web

# The frontend build turns this into the /static/assets/icons-<hash>.svg sprite (vite.config.ts).
SPRITE = json.loads((Path(__file__).parents[1] / "frontend/src/icons.json").read_text())


def icons_module() -> dict:
    """_icons.html's exports (TemplateModule attributes are dynamic, so read its __dict__)."""
    return web.templates.get_template("_icons.html").module.__dict__


def test_every_kind_has_an_icon_in_the_sprite() -> None:
    kind_icons = icons_module()["kind_icons"]
    assert set(kind_icons) == {k.value for k in Kind}
    for html in kind_icons.values():
        [name] = re.findall(r'icons(?:-[\w-]+)?\.svg#([\w-]+)"', str(html))
        assert name in SPRITE


def test_icon_is_hidden_from_screen_readers_unless_labelled() -> None:
    icon = icons_module()["icon"]
    assert 'aria-hidden="true"' in str(icon("plane"))
    assert 'role="img" aria-label="Airport change"' in str(icon("warning", "Airport change"))


def test_trip_page_draws_kind_icons() -> None:
    r = Reservation(id="r", kind=Kind.RAIL, title="Eurostar", start_local=datetime(2027, 1, 21, 8, 31))
    trip = Trip(id="t", owner="u", name="Paris", reservations=[r])
    html = web.templates.get_template("trip.html").render(
        email=None, trip=trip, summary=trip.summary(), days=[(r.start_date, [r])]
    )
    assert re.search(r'<use href="/static/(assets/)?icons(-[\w-]+)?\.svg#train"/>', html)


def test_pages_carry_the_brand_mark() -> None:
    html = web.templates.get_template("base.html").render(email=None)
    mark = r"/static/(brand|assets)/mark(-[\w-]+)?\.svg"
    assert re.search(rf'<link rel="icon" type="image/svg\+xml" href="{mark}">', html)
    touch = r"/static/(brand|assets)/apple-touch-icon(-[\w-]+)?\.png"
    assert re.search(rf'<link rel="apple-touch-icon" href="{touch}">', html)
    assert re.search(rf'<a class="brand" href="/"><img src="{mark}" alt=""', html)

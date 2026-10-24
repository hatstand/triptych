# Triptych design system

What the web UI looks like today, written down from `frontend/src/style.css` and the
Jinja templates in `src/triptych/web/templates/`. It describes the system as it is (gaps
included, at the end), so a new page or component can match it without reverse-engineering the
CSS. If you change a token or a component, change this file in the same PR.

Everything lives in one stylesheet, `frontend/src/style.css`, loaded by `base.html`. Templates
link it with `static('style.css')`: the Vite build minifies it to a file named by its content
hash, so a deploy never leaves a browser (or Cloudflare's cache) on an old copy. There is no CSS
framework or preprocessor. Biome formats and lints it (`frontend/biome.json`), and
CI fails a PR whose CSS isn't formatted: run `npm run format:css` in `frontend/` before pushing. Pages may add a small `<style>` block in `{% block head %}`
for page-only rules (the admin page does).

## Colour

All colours are CSS custom properties on `:root`. Use the variable, never the hex value.

### Surfaces and text

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` | `#f4f5f7` | `#11141a` | Page background; also input fields and the answer box, which sit "inset" in a card |
| `--card` | `#fff` | `#1a1f28` | Cards, top bar, menus, drop panel |
| `--ink` | `#1d2330` | `#e6e9ef` | Body text |
| `--muted` | `#6b7280` | `#9aa3b2` | Secondary text: dates, labels, `<dt>`, hints |
| `--line` | `#e3e6eb` | `#2a313d` | 1px borders, dividers, the neutral badge |

### Actions and state

| Token | Value | Use |
| --- | --- | --- |
| `--accent` | `#1f6feb` | Links, primary buttons, card hover border, in-progress badges, drop-zone outline |
| `--accent-ink` | `#fff` | Text on `--accent` |
| `--danger` | `#c62828` | Destructive buttons, errors, failed badges |

### Reservation kinds

One colour per `models.Kind`. It is the 4px left border of a timeline card and the colour of
its icon (both via a `--kind` variable that `.segment.<kind>` sets).

| Kind | Token | Value | Icon |
| --- | --- | --- | --- |
| flight | `--flight` | `#1f6feb` | `plane` |
| lodging | `--lodging` | `#8e44ad` | `hotel` |
| car | `--car` | `#e67e22` | `car` |
| rail | `--rail` | `#16a085` | `train` |
| restaurant | `--restaurant` | `#c0392b` | `restaurant` |
| activity | `--activity` | `#d4a017` | `ticket` |
| other | `--other` | `#7f8c8d` | `place` |

`--activity` (amber) doubles as the attention colour: the Alerts card's left border uses it.

### Dark mode

Automatic, from `prefers-color-scheme: dark`; there is no toggle. Only the five surface and text
tokens change. Accent, danger, kind colours and badge colours are the same in both schemes.

## Typography

- **Family:** the system UI stack, `system-ui, -apple-system, "Segoe UI", sans-serif`. Code,
  confirmation codes and the calendar feed URL use `ui-monospace, monospace` at `.9em`
  (`code`, `.mono`).
- **Base:** `15px` with a line height of `1.45`, set on `body`. Inputs and buttons inherit it
  (`font: inherit`).

| Role | Size | Weight | Where |
| --- | --- | --- | --- |
| Page title `h1` | `1.6rem` | bold | Trip name, Admin |
| Trip name on a card `.trip-name` | `1.15rem` | 600 | Home page trip cards |
| Times `.segment .when`, journey route | `1.05rem` | normal | Timeline |
| Section title `h2` | `1.05rem` | bold | Card headings ("Inbox", "Ask about this trip") |
| Day heading `.day` | `.95rem` | 600, `--muted` | Timeline days |
| Body | `15px` (1rem) | normal | |
| Facts `.facts`, `.trip-dates` | `.9rem` | normal | |
| Small `.small` | `.85rem` | normal | Hints, secondary links, time zones |
| Badge `.badge` | `.72rem` | uppercase, `.04em` tracking | Status pills |

The brand name in the top bar is bold with `.02em` letter spacing, after the mark (see Brand mark).

## Spacing, shape and layout

There is no spacing scale variable; values in use cluster around a 2/4px rhythm:

- **Gaps:** `8px` inside stacks and between actions, `12px` between cards and in rows, `14px`
  between a timeline icon and its text, `20px` between the two layout columns.
- **Card padding:** `14px 16px`. Inputs `8px 10px`, buttons `8px 14px`.
- **Radius:** `10px` cards and panels, `8px` inputs, buttons, menus and toasts, `99px` badges.
- **Borders:** `1px solid var(--line)` everywhere; a `4px` left border marks a card's kind or
  an alert.
- **Layout tokens:** `--topbar-h` (`50px`, the top bar's fixed height, which the sticky sidebar
  subtracts), `--fade` (`24px`, the length of the inbox's scroll fade) and `--scrollbar` (the
  thumb colour of the sidebar's thin scrollbars: `--muted` at 45%, so it follows the colour scheme).
- **Shadow:** only floating things get one (the drop toast: `0 4px 16px rgb(0 0 0 / .15)`).
  Cards are flat.

Layout:

- `main` is centred, max `1100px` wide, padded `20px 16px 60px`.
- `.grid` is the two-column page: content plus a `340px` sidebar, collapsing to one column
  under `820px`. Home and trip pages use it.
- On the home page the sidebar (`aside.sidebar`) is sticky above `820px` and capped at the
  viewport height, so the inbox scrolls inside its card and the calendar feed stays on screen.
  If the viewport is too short even for a minimum-height inbox, the sidebar itself scrolls.
- `.narrow` is a single `420px` column for sign-in and registration.
- Utilities: `.row` (space-between flex row), `.stack` (vertical flex, 8px gap), `.actions`
  (button group), `.muted`, `.small`, `.mono`.

## Components

**Top bar** `.topbar`: `--topbar-h` tall, card-coloured strip with the brand (the mark at `24px`, then "Triptych") on the left and the
signed-in email plus a "Sign out" link button on the right.

**Card** `.card`: the basic container. Every sidebar section, trip card and timeline item is
one. `.trip-card` is a whole-card link whose border turns `--accent` on hover.

**Trip card** `.trip-card`: a row of the destination photo, `.trip-thumb` (`72px` square, `8px`
radius, cropped to fill), then `.trip-info` (dates, name, item count). A trip without a photo
yet gets `.trip-thumb.placeholder` instead: the same square in `--bg` with the `place` icon at
`24px` in `--muted`. The thumbnail's tooltip carries the photo's credit.

**Trip photo** `figure.trip-photo`: the destination photo across the top of the trip page, a
`5:1` frame (`5:2` under `820px`), `10px` radius, cropped to fill, with its credit in a
right-aligned `.muted.small` caption: author linked to the Commons file page, licence linked
to its deed, "via Wikimedia Commons" (`photo_credit` in `_photo.html`). Commons licences
require the credit wherever the photo appears, so don't show one without it.

Photos are cropped around their subject, not their middle. Gemma marks the main subject's box
once per photo; `Photo.position(frame)` turns it into the `object-position` that keeps the
subject in a frame of that shape (centred if it fits, else from its top). Frames therefore have
fixed shapes: the thumbnail is square and the trip photo's two shapes are passed in as
`--focus` / `--focus-narrow`. A new place that shows a photo needs a fixed shape too, and its own
`position(...)` call. Until the subject is known the crop is `50% 35%`.

**Buttons:**
- Primary `button` / `.button`: `--accent` fill, white text.
- Secondary `.button.secondary`: transparent with a `--line` border and `--ink` text (Export
  .ics, Edit).
- Link `button.link`: looks like a link; used for small inline actions (remove, Dismiss all,
  Sign out).
- Danger `button.danger`: `--danger` fill (Delete trip).

**Inputs:** full-width, `--bg` fill inside a card, `--line` border, `8px` radius.

**Badge** `.badge.<state>`: an uppercase pill for an ingest state. `done` green, `failed` red,
`no_travel` grey, and the in-progress states (`queued`, `parsing`, `extracting`, `filing`) blue
with a 1.2s pulse.

**Timeline item** `.card.segment.<kind>`: icon column (28px, icon 24px in the kind colour),
then the body: title row with a remove link, times (`.when`, with the zone in `.muted.small`),
route or address, and a `.facts` definition list (two columns: muted label, value).

**Alerts** `.card.alerts`: amber left border and a plain list of timestamped notifications.

**Inbox** `.card.inbox`: the heading, then `.inbox-scroll`, which wraps the live-swapped list
(`sse-swap="inbox"`) so its scroll position survives updates. On wide screens the list scrolls,
fading out over `--fade` at the bottom, and also at the top once scrolled where scroll-driven
animations are supported.

**Inbox item** `.inbox-item`: badge, label, optional message and "view trip →" links, divided
by `--line` rules.

**Menu** `details.menu`: a `<summary>` styled as a button opening an absolutely positioned
`.menu-panel` card (260px). No JS.

**Answer** `.answer`: `--bg` inset box that keeps the model's line breaks.

**Drop zone and toast** `.dropzone`, `.drop-status`: full-page dashed `--accent` outline over
an 18% accent wash while dragging files, then a centred bottom toast for the upload result
(`.error` turns it red).

## Icons

Icons are [Lucide](https://lucide.dev) (ISC licence), pinned as the `lucide-static` dev
dependency in `frontend/`. Lucide is drawn on a **24×24 grid with a 2px stroke, round caps and
joins, no fill**, in `currentColor`, which suits the flat, bordered look here and means an icon
takes the colour of the text or element around it.

Triptych uses its own names for the icons it needs, mapped to Lucide names in
`frontend/src/icons.json`. The Vite build turns that list into one SVG sprite,
`static('icons.svg')`, with a `<symbol>` per icon. To add an icon, add a line to
`icons.json` (browse names at lucide.dev) and rebuild.

| Name | Lucide | For |
| --- | --- | --- |
| `plane` | `plane` | Flights |
| `takeoff` | `plane-takeoff` | Departures |
| `landing` | `plane-landing` | Arrivals |
| `train` | `train-front` | Rail |
| `bus` | `bus` | Coaches and buses |
| `ferry` | `ship` | Ferries |
| `car` | `car` | Car journeys, transfers, taxis |
| `rental` | `key-round` | Car hire pick-up and drop-off |
| `hotel` | `bed-double` | Lodging |
| `restaurant` | `utensils` | Restaurants |
| `ticket` | `ticket` | Activities and events |
| `luggage` | `luggage` | Baggage |
| `layover` | `clock` | Layover / connection time |
| `transfer` | `arrow-left-right` | Change of airport or station |
| `warning` | `triangle-alert` | Tight or risky connections, alerts |
| `place` | `map-pin` | Other items, addresses |
| `calendar` | `calendar` | Dates, calendar export |

Only flight, lodging, car, rail, restaurant, activity and other are reservation kinds today.
The journey view uses `warning` for airport and station changes and `layover` / `transfer` on
the connections between legs; bus, ferry, takeoff, landing, rental and luggage are ready for
when the model grows.

In templates, import the macro from `_icons.html`:

```jinja
{% from "_icons.html" import icon, kind_icons %}
{{ icon('warning') }}                        {# decorative: aria-hidden #}
{{ icon('warning', 'Airport change') }}      {# meaningful on its own: role="img" + label #}
{{ kind_icons[r.kind.value] }}               {# the icon for a reservation kind #}
```

Sizing: `.i` is `1.25em` square and sits on the text baseline, so inline icons scale with the
text they are in. The timeline sets them to `24px` (the grid size) inside `.segment .icon`.
Draw icons at 24px or at text size; avoid other sizes so the 2px stroke stays crisp.

## Brand mark

Triptych's mark is three trip cards fanned out: rail (`--rail`) and lodging (`--lodging`) behind,
flight (`--flight`) in front with a white plane on it. It lives in one file,
`frontend/src/brand/mark.svg`, on the same 24×24 grid as the icons. The gaps between the cards are
cut out with masks rather than drawn in a background colour, so the one file works on light and dark
surfaces and needs no dark variant.

The Vite build (`brandIcons()` in `frontend/vite.config.ts`) uses it three ways:

- **Favicon:** the SVG itself, `<link rel="icon">` in `base.html`.
- **Top bar logo:** the same SVG at `24px` in `.brand`.
- **Home-screen icon:** the mark at 70% on a white square, rendered to a `180px` PNG with resvg for
  `<link rel="apple-touch-icon">` (iOS takes no SVG there and rounds the corners itself).

All three go through `static('brand/…')`, so they are hashed like the other assets. To change the
mark, edit `mark.svg` and rebuild; nothing else holds a copy.

## Known gaps

Written down so they are decisions, not accidents:

- Badge colours (`#d1f4dd`, `#146c2e`, `#fde0e0`, `#dbe9ff`, `#eceff3`) are hex values, not
  tokens, and do not change in dark mode, where the light pills are bright against dark cards.
- Kind colours, accent and danger are not adjusted for dark mode either.
- No spacing or radius tokens; values are repeated literally.
- No focus styles beyond the browser default.
- Contrast (WCAG) misses, measured on the current values: `--accent` text on dark cards is
  3.6:1 and `--danger` on dark cards 2.9:1 (text wants 4.5:1); `--muted` on `--bg` is 4.4:1;
  the queued and no_travel badges are 3.8:1 and 4.2:1. As icon colours (3:1 wanted),
  `--activity` (2.4:1) and `--car` (2.9:1) are faint on white cards, and `--lodging` (2.8:1)
  and `--restaurant` (3.0:1) on dark ones.

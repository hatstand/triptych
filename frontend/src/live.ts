// Live updates for the home page: /events streams re-rendered [data-live] sections whenever the
// user's trips, inbox or alerts change (see web/live.py), and we swap them in.

type Update = { sections: Record<string, string> };

const sections = new Map<string, HTMLElement>();
for (const el of document.querySelectorAll<HTMLElement>("[data-live]")) {
  if (el.dataset.live) sections.set(el.dataset.live, el);
}

const MAX_DELAY = 30_000;
let delay = 1000;

function apply(e: MessageEvent<string>): void {
  const update = JSON.parse(e.data) as Update;
  for (const [name, html] of Object.entries(update.sections)) {
    const el = sections.get(name);
    if (el) el.innerHTML = html;
  }
}

function connect(): void {
  const source = new EventSource("/events");
  source.addEventListener("open", () => {
    delay = 1000;
  });
  source.addEventListener("update", apply);
  source.addEventListener("error", () => {
    // EventSource retries a dropped connection by itself, but gives up for good on an HTTP error
    // (a 502 from the proxy while a deploy restarts the app, say). Start over in that case.
    if (source.readyState !== EventSource.CLOSED) return;
    setTimeout(connect, delay);
    delay = Math.min(delay * 2, MAX_DELAY);
  });
}

if (sections.size) connect();

export {};

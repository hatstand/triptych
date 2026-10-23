// Live updates for the home page. htmx's SSE extension does it all from the template's attributes
// (sse-connect="/events", sse-swap="<section>"): see index.html and web/live.py.
import htmx from "htmx.org";
import "htmx-ext-sse";

// We only swap in our own server-rendered HTML; nothing needs htmx to evaluate script.
htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;

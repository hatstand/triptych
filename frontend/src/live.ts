// Live updates for the home page. htmx's SSE extension does it all from the template's attributes
// (sse-connect="/events", sse-swap="<section>"): see index.html and web/live.py.
import htmx from "htmx.org";
import "htmx-ext-sse";

// We only swap in our own server-rendered HTML; nothing needs htmx to evaluate script.
htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;
// No <style> for hx-indicator, which nothing uses: the Content-Security-Policy would refuse it.
// htmx inserts it on DOMContentLoaded, after this module has run.
htmx.config.includeIndicatorStyles = false;

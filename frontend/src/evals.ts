// The eval labelling UI (python -m evals ui, evals/ui.py). htmx does the work from the templates'
// attributes: the form posts and the server sends back the whole form, saved or with its errors.
import htmx from "htmx.org";

// We only swap in our own server-rendered HTML; nothing needs htmx to evaluate script.
htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;
// A save that doesn't validate (422) or that would overwrite a change made elsewhere (409) comes back
// as the form with its errors, to swap in like any other.
htmx.config.responseHandling = [
  { code: "204", swap: false },
  { code: "[23]..", swap: true },
  { code: "409|422", swap: true, error: false },
  { code: "[45]..", swap: false, error: true },
];

// Ctrl+S (Cmd+S) saves.
document.addEventListener("keydown", (event) => {
  if (event.key !== "s" || !(event.ctrlKey || event.metaKey)) return;
  const save = document.getElementById("save");
  if (!(save instanceof HTMLButtonElement)) return;
  event.preventDefault();
  save.click();
});

// The form marks itself data-dirty when it holds changes that aren't saved; the server renders it
// that way after an add, a remove or a failed save, and a saved form comes back without it.
document.addEventListener("input", (event) => {
  if (event.target instanceof Element) event.target.closest("form#case-form")?.setAttribute("data-dirty", "");
});
window.addEventListener("beforeunload", (event) => {
  if (document.querySelector("form#case-form[data-dirty]")) event.preventDefault();
});

// "Save as verified, next draft": the server says where next once the saved form is in place.
document.body.addEventListener("verified", (event) => {
  const next = (event as CustomEvent<{ next?: string }>).detail.next;
  if (next) window.location.assign(next);
});

// Destination photo crops: the server works out where to crop each photo to keep its subject in frame
// (models.Photo.position) and puts it in data-focus (data-focus-narrow for the narrow-screen frame).
// Here it becomes the --focus custom properties style.css reads, because the Content-Security-Policy
// refuses style="" attributes; setting them from a script is allowed. Live updates swap in new trip
// cards, so each htmx:load (the page, then every swap) is covered too.

function applyFocus(root: ParentNode): void {
  for (const img of root.querySelectorAll<HTMLImageElement>("img[data-focus]")) {
    img.style.setProperty("--focus", img.dataset.focus ?? null);
    if (img.dataset.focusNarrow) img.style.setProperty("--focus-narrow", img.dataset.focusNarrow);
  }
}

applyFocus(document);
document.addEventListener("htmx:load", (event) => {
  if (event.target instanceof Element) applyFocus(event.target);
});

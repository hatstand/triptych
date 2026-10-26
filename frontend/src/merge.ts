// Merging trips on the home page: drag one trip card onto another, confirm in the #merge dialog,
// and everything in the dragged trip moves into the one it was dropped on (POST /trips/<id>/merge).
// Live updates re-render the cards, so this listens on the document and remembers trip ids, not
// elements. Without a mouse, the trip page's Edit dialog offers the same merge from a list.

const TYPE = "application/x-triptych-trip";

const dialog = document.getElementById("merge");
const form = dialog?.querySelector("form");
const into = form?.querySelector<HTMLInputElement>("input[name=into]");

let dragged: string | null = null;

function cardOf(target: EventTarget | null): HTMLElement | null {
  return target instanceof Element ? target.closest<HTMLElement>(".trip-card[data-trip-id]") : null;
}

// The card under a drag that could take the dragged trip: any other trip's.
function targetOf(e: DragEvent): HTMLElement | null {
  const card = cardOf(e.target);
  return dragged && card && card.dataset.tripId !== dragged ? card : null;
}

function clear(): void {
  for (const el of document.querySelectorAll(".trip-card.dragging, .trip-card.drop-target")) {
    el.classList.remove("dragging", "drop-target");
  }
  document.body.classList.remove("merging");
}

function nameOf(id: string): string {
  return document.querySelector<HTMLElement>(`.trip-card[data-trip-id="${CSS.escape(id)}"]`)?.dataset.tripName ?? "";
}

function confirmMerge(source: string, target: string): void {
  if (!(dialog instanceof HTMLDialogElement) || !form || !into) return;
  const names: Record<string, string> = { source: nameOf(source), into: nameOf(target) };
  for (const el of dialog.querySelectorAll<HTMLElement>("[data-merge]")) {
    el.textContent = names[el.dataset.merge ?? ""] ?? "";
  }
  form.action = `/trips/${encodeURIComponent(source)}/merge`;
  into.value = target;
  dialog.showModal();
}

if (dialog) {
  document.addEventListener("dragstart", (e) => {
    const card = cardOf(e.target);
    if (!card?.dataset.tripId || !e.dataTransfer) return;
    dragged = card.dataset.tripId;
    e.dataTransfer.setData(TYPE, dragged);
    e.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
    document.body.classList.add("merging");
  });
  document.addEventListener("dragover", (e) => {
    if (!targetOf(e) || !e.dataTransfer) return;
    e.preventDefault(); // marks the card as somewhere to drop
    e.dataTransfer.dropEffect = "move";
  });
  document.addEventListener("dragenter", (e) => {
    targetOf(e)?.classList.add("drop-target");
  });
  document.addEventListener("dragleave", (e) => {
    const card = cardOf(e.target);
    if (card && !card.contains(e.relatedTarget as Node | null)) card.classList.remove("drop-target");
  });
  document.addEventListener("drop", (e) => {
    const target = targetOf(e);
    if (!target?.dataset.tripId || !dragged) return;
    e.preventDefault();
    const source = dragged;
    clear();
    confirmMerge(source, target.dataset.tripId);
  });
  document.addEventListener("dragend", () => {
    dragged = null;
    clear();
  });
}

export {};

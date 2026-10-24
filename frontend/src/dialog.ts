// Modal dialogs: <dialog closedby="any"> opened and closed by buttons with commandfor/command, so
// Cancel, Escape and a click on the backdrop all close it natively. Closing without submitting
// discards edits: each form in the dialog is reset to the values the page was rendered with.
// Where a browser lacks invoker commands or closedby, the same behaviour is filled in here.

const hasCommands = "command" in HTMLButtonElement.prototype;
const hasClosedBy = "closedBy" in HTMLDialogElement.prototype;

for (const dialog of document.querySelectorAll<HTMLDialogElement>("dialog")) {
  dialog.addEventListener("close", () => {
    for (const form of dialog.querySelectorAll("form")) form.reset();
  });

  if (!hasClosedBy) {
    // A click on the backdrop lands on the dialog itself, outside its box.
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      const inside =
        event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) dialog.close();
    });
  }
}

if (!hasCommands) {
  for (const button of document.querySelectorAll<HTMLButtonElement>("button[commandfor]")) {
    const dialog = document.getElementById(button.getAttribute("commandfor") ?? "");
    if (!(dialog instanceof HTMLDialogElement)) continue;
    const command = button.getAttribute("command");
    button.addEventListener("click", () => {
      if (command === "show-modal") dialog.showModal();
      else if (command === "close") dialog.close();
    });
  }
}

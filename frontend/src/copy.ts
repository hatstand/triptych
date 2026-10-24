// Copy buttons: <button data-copy="<input id>"> copies that input's value and says "Copied" for a
// moment. The clipboard API needs a secure context, which every deployed origin is; where it is
// missing or refused, the input's text is selected instead so it can be copied by hand.

const COPIED_MS = 1500;

for (const button of document.querySelectorAll<HTMLButtonElement>("button[data-copy]")) {
  const input = document.getElementById(button.dataset.copy ?? "");
  const label = button.querySelector<HTMLElement>(".copied-label");
  if (!(input instanceof HTMLInputElement)) continue;
  let timer: number | undefined;

  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(input.value);
    } catch {
      input.select();
      return;
    }
    button.classList.add("copied");
    if (label) label.textContent = "Copied";
    clearTimeout(timer);
    timer = window.setTimeout(() => {
      button.classList.remove("copied");
      if (label) label.textContent = "";
    }, COPIED_MS);
  });
}

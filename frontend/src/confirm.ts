// Forms that ask first: <form data-confirm="Delete this trip?"> submits only if the question is
// answered OK. Listening on the document covers every form, including ones htmx swaps in later.

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const question = form.dataset.confirm;
  if (question && !confirm(question)) event.preventDefault();
});

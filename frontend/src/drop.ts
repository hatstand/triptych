// Page-wide drop zone (signed-in pages only): every dropped file goes to POST /plans, the same
// endpoint as the "Add plans" form. The new items show up in the home page's inbox by themselves
// (live.ts), so from anywhere else we go there.

const ACCEPT = /\.(eml|pdf|html?|txt)$/i;
const MAX_BYTES = 15 * 1024 * 1024; // keep in step with MAX_UPLOAD in web/app.py
const SKIPPED_KEY = "triptych-drop-skipped";

const zone = document.getElementById("dropzone");
const status = document.getElementById("drop-status");

function hasFiles(e: DragEvent): boolean {
  return Array.from(e.dataTransfer?.types ?? []).includes("Files");
}

function wanted(f: File): boolean {
  return ACCEPT.test(f.name) || f.type.startsWith("image/") || f.type === "message/rfc822";
}

function plural(n: number): string {
  return `${n} file${n === 1 ? "" : "s"}`;
}

function show(el: HTMLElement, text: string, error = false): void {
  el.textContent = text;
  el.classList.toggle("error", error);
  el.hidden = false;
}

function flash(el: HTMLElement, text: string): void {
  show(el, text);
  setTimeout(() => {
    el.hidden = true;
  }, 4000);
}

async function upload(files: File[]): Promise<string | null> {
  const body = new FormData();
  for (const f of files) body.append("file", f);
  try {
    // /plans answers with a redirect to "/"; don't follow it, just go there ourselves.
    const resp = await fetch("/plans", { method: "POST", body, redirect: "manual" });
    if (resp.type === "opaqueredirect" || resp.ok) return null;
    const detail = await resp.json().then(
      (j: { detail?: string }) => j.detail ?? resp.statusText,
      () => resp.statusText,
    );
    return `Upload failed: ${detail}`;
  } catch {
    return "Upload failed: network error.";
  }
}

if (zone && status) {
  let depth = 0;
  const hide = (): void => {
    depth = 0;
    zone.hidden = true;
  };

  window.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    zone.hidden = false;
  });
  window.addEventListener("dragover", (e) => {
    if (!hasFiles(e) || !e.dataTransfer) return;
    e.preventDefault(); // without this the browser opens the file instead of dropping it
    e.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth -= 1;
    if (depth <= 0) hide();
  });
  window.addEventListener("drop", async (e) => {
    if (!hasFiles(e) || !e.dataTransfer) return;
    e.preventDefault();
    hide();
    const all = Array.from(e.dataTransfer.files);
    const files = all.filter(wanted);
    const skipped = all.length - files.length;
    if (!files.length) {
      show(status, "Only emails, PDFs, images, HTML or text files can be added.", true);
      return;
    }
    const big = files.find((f) => f.size > MAX_BYTES);
    if (big) {
      show(status, `${big.name} is larger than 15 MB.`, true);
      return;
    }

    show(status, `Uploading ${plural(files.length)}…`);
    const error = await upload(files);
    if (error) {
      show(status, error, true);
      return;
    }
    if (document.querySelector("[data-live]")) {
      const note = skipped ? ` Skipped ${plural(skipped)} of an unsupported type.` : "";
      flash(status, `Added ${plural(files.length)}.${note}`);
      return;
    }
    try {
      if (skipped) sessionStorage.setItem(SKIPPED_KEY, String(skipped));
    } catch {}
    window.location.assign("/");
  });

  try {
    const skipped = Number(sessionStorage.getItem(SKIPPED_KEY));
    if (skipped) {
      sessionStorage.removeItem(SKIPPED_KEY);
      flash(status, `Skipped ${plural(skipped)} of an unsupported type.`);
    }
  } catch {}
}

export {};

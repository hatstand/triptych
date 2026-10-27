// Page-wide drop zone and paste (signed-in pages only): every dropped or pasted file goes to POST
// /plans, the same endpoint as the "Add plans" form. The new items show up in the home page's inbox by
// themselves (see index.html), so from anywhere else we go there.

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

// A pasted screenshot arrives as "image.png" (or with no name); give it one that says what it is and
// tells several apart in the inbox.
function named(f: File): File {
  if (f.name && !/^image\.\w+$/i.test(f.name)) return f;
  const ext = f.type.split("/")[1]?.replace("jpeg", "jpg") ?? "png";
  const d = new Date();
  const pad = (n: number): string => String(n).padStart(2, "0");
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const stamp = `${day} ${pad(d.getHours())}.${pad(d.getMinutes())}.${pad(d.getSeconds())}`;
  return new File([f], `Pasted ${stamp}.${ext}`, { type: f.type });
}

function editable(t: EventTarget | null): boolean {
  return t instanceof HTMLElement && (t.isContentEditable || t.matches("input, textarea, select"));
}

async function upload(files: File[], text = ""): Promise<string | null> {
  const body = new FormData();
  for (const f of files) body.append("file", f);
  if (text) body.append("text", text);
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
  // Sends what was dropped or pasted, then says how it went (or goes to the inbox to show it).
  const add = async (all: File[], text = ""): Promise<void> => {
    const files = all.filter(wanted);
    const skipped = all.length - files.length;
    if (!files.length && !text) {
      show(status, "Only emails, PDFs, images, HTML or text files can be added.", true);
      return;
    }
    const big = files.find((f) => f.size > MAX_BYTES);
    if (big) {
      show(status, `${big.name} is larger than 15 MB.`, true);
      return;
    }

    const what = files.length ? plural(files.length) : "text";
    show(status, `Uploading ${what}…`);
    const error = await upload(files, text);
    if (error) {
      show(status, error, true);
      return;
    }
    if (document.querySelector("[sse-connect]")) {
      const note = skipped ? ` Skipped ${plural(skipped)} of an unsupported type.` : "";
      flash(status, `Added ${what}.${note}`);
      return;
    }
    try {
      if (skipped) sessionStorage.setItem(SKIPPED_KEY, String(skipped));
    } catch {}
    window.location.assign("/");
  };

  window.addEventListener("drop", (e) => {
    if (!hasFiles(e) || !e.dataTransfer) return;
    e.preventDefault();
    hide();
    void add(Array.from(e.dataTransfer.files));
  });

  // Pasted files (screenshots of confirmations, mostly) are added from anywhere, even a text field,
  // which couldn't hold them. Pasted text is added only outside text fields, where it would otherwise
  // go nowhere; inside one (the "Add plans" box) it is just typed in as usual.
  window.addEventListener("paste", (e) => {
    const data = e.clipboardData;
    if (!data) return;
    const files = Array.from(data.files).map(named);
    if (files.length) {
      e.preventDefault();
      void add(files);
      return;
    }
    const text = data.getData("text/plain").trim();
    if (!text || editable(e.target)) return;
    e.preventDefault();
    void add([], text);
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

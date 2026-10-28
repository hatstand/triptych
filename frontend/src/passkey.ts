// Passkey ceremonies: fetch options from the server, hand them to the browser's WebAuthn API, and
// post the result back. Buttons opt in with data-passkey="login" or data-passkey="register" (plus
// data-invite when registering from an invite link, or data-signup when creating the account a
// verified signup asked for); errors go to the page's [data-passkey-error]. Clicks are handled on
// the document, so a button swapped in later (the signup page's, over SSE) works too.

interface Redirect {
  redirect: string;
}

async function postJSON<T>(url: string, body: unknown = {}): Promise<T> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || data.detail || `Request failed (${resp.status})`);
  return data as T;
}

async function login(): Promise<Redirect> {
  const options = await postJSON<PublicKeyCredentialRequestOptionsJSON>("/auth/login/options");
  const credential = (await navigator.credentials.get({
    publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options),
  })) as PublicKeyCredential;
  return postJSON("/auth/login/verify", credential.toJSON());
}

async function register(button: HTMLButtonElement): Promise<Redirect> {
  const invite = button.dataset.invite ?? null;
  const signup = button.dataset.signup !== undefined;
  const options = await postJSON<PublicKeyCredentialCreationOptionsJSON>("/auth/register/options", {
    invite,
    signup,
  });
  const credential = (await navigator.credentials.create({
    publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options),
  })) as PublicKeyCredential;
  const name = document.querySelector<HTMLInputElement>("[name=passkey-name]")?.value ?? "";
  return postJSON("/auth/register/verify", { credential: credential.toJSON(), name });
}

function showError(message: string): void {
  const el = document.querySelector<HTMLElement>("[data-passkey-error]");
  if (el) {
    el.textContent = message;
    el.hidden = false;
  } else {
    alert(message);
  }
}

// Older browsers lack the JSON helpers even where they have WebAuthn itself.
const supported = typeof window.PublicKeyCredential?.parseRequestOptionsFromJSON === "function";
const UNSUPPORTED = "This browser doesn't support passkeys. Try an up-to-date Safari, Chrome, Edge or Firefox.";

if (!supported && document.querySelector("button[data-passkey]")) {
  for (const button of document.querySelectorAll<HTMLButtonElement>("button[data-passkey]")) {
    button.disabled = true;
  }
  showError(UNSUPPORTED);
}

document.addEventListener("click", async (event) => {
  const button = (event.target as Element | null)?.closest<HTMLButtonElement>("button[data-passkey]");
  if (!button || button.disabled) return;
  if (!supported) {
    showError(UNSUPPORTED);
    return;
  }
  button.disabled = true;
  try {
    const { redirect } = await (button.dataset.passkey === "login" ? login() : register(button));
    window.location.assign(redirect);
  } catch (e) {
    // NotAllowedError: the user cancelled or the prompt timed out. Nothing to explain.
    if (!(e instanceof DOMException && e.name === "NotAllowedError")) {
      showError(e instanceof Error ? e.message : String(e));
    }
    button.disabled = false;
  }
});

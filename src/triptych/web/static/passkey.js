// Passkey ceremonies: fetch options from the server, hand them to the browser's WebAuthn API, and
// post the result back. Buttons opt in with data-passkey="login" or data-passkey="register" (plus
// data-invite when registering from an invite link); errors go to the page's [data-passkey-error].

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

async function login() {
  const options = await postJSON("/auth/login/options");
  const credential = await navigator.credentials.get({
    publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options),
  });
  return postJSON("/auth/login/verify", credential.toJSON());
}

async function register(button) {
  const invite = button.dataset.invite || null;
  const options = await postJSON("/auth/register/options", { invite });
  const credential = await navigator.credentials.create({
    publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options),
  });
  const name = document.querySelector("[name=passkey-name]")?.value ?? "";
  return postJSON("/auth/register/verify", { credential: credential.toJSON(), name });
}

function showError(message) {
  const el = document.querySelector("[data-passkey-error]");
  if (el) {
    el.textContent = message;
    el.hidden = false;
  } else {
    alert(message);
  }
}

for (const button of document.querySelectorAll("[data-passkey]")) {
  if (!window.PublicKeyCredential?.parseRequestOptionsFromJSON) {
    button.disabled = true;
    showError("This browser doesn't support passkeys. Try an up-to-date Safari, Chrome, Edge or Firefox.");
    continue;
  }
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const { redirect } = await (button.dataset.passkey === "login" ? login() : register(button));
      window.location.assign(redirect);
    } catch (e) {
      // NotAllowedError: the user cancelled or the prompt timed out. Nothing to explain.
      if (e.name !== "NotAllowedError") showError(e.message);
      button.disabled = false;
    }
  });
}

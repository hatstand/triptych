"""Runtime configuration, read once from the environment."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Settings:
    restate_ingress_url: str
    database_url: str
    restate_admin_url: str
    worker_public_url: str
    ollama_url: str
    llm_model: str
    # A candidate model that extracts every new ingest again, recorded but never applied (Reprocess
    # "shadow" runs, compared on /admin). Unset = no shadow runs.
    shadow_model: str | None
    # Show the model a user's own corrected documents before each new one of theirs (examples.py).
    # Off until the evals say it helps.
    llm_examples: bool
    # Embeds examples and new documents to find the examples most like each (examples.py).
    embed_model: str
    llm_num_ctx: int
    llm_timeout_s: float
    public_base_url: str
    llm_concurrency: int
    secret_key: str
    inbound_token: str | None
    inbound_address: str
    admin_logins: frozenset[str]
    restate_ui_url: str | None
    # Where originals are kept (inputs.py): an S3-compatible bucket (R2 in production) and the Tink
    # master keyset that encrypts each user's keys. No bucket = originals aren't kept.
    inputs_bucket: str | None
    inputs_endpoint_url: str | None
    inputs_access_key_id: str | None
    inputs_secret_access_key: str | None
    inputs_master_keyset: str | None
    # The private bucket holding the extraction eval set (evalset.py): plaintext copies of the
    # originals of accounts that opted in. Same endpoint and credentials as the inputs bucket, so
    # that token must reach both. Unset = sharing isn't offered.
    evals_bucket: str | None
    # What's deployed, recorded with each extraction run (the image tag on marvin).
    code_version: str

    # Passkeys are bound to the RP ID: the UI's hostname, or a parent domain of it. Changing it
    # later strands every registered passkey, so set WEBAUTHN_RP_ID to the parent domain up front
    # if the UI might move between subdomains (ts.net itself is a public suffix, so never that).
    webauthn_rp_id: str
    # Origins the UI is served from (scheme://host[:port], exactly as in the address bar). Passkey
    # ceremonies and state-changing requests from any other origin are refused.
    allowed_origins: tuple[str, ...]
    # Where the admin pages are served from (the tailnet name), when that isn't the public UI's.
    admin_origins: tuple[str, ...]

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")


def origin_of(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def origins(value: str) -> tuple[str, ...]:
    return tuple(o.strip().rstrip("/") for o in value.split(",") if o.strip())


def load() -> Settings:
    env = os.environ.get
    public_base_url = env("PUBLIC_BASE_URL", "http://localhost:8000")
    allowed_origins = origins(env("WEBAUTHN_ORIGINS", "")) or (origin_of(public_base_url),)
    return Settings(
        restate_ingress_url=env("RESTATE_INGRESS_URL", "http://localhost:8080"),
        database_url=env("DATABASE_URL", "postgresql://triptych:triptych@localhost:5432/triptych"),
        restate_admin_url=env("RESTATE_ADMIN_URL", "http://localhost:9070"),
        worker_public_url=env("WORKER_PUBLIC_URL", "http://worker:9080"),
        ollama_url=env("OLLAMA_URL", "http://localhost:11434"),
        llm_model=env("LLM_MODEL", "gemma4:26b-a4b-it-qat"),
        shadow_model=env("SHADOW_MODEL") or None,
        llm_examples=env("LLM_EXAMPLES", "") in ("1", "true", "yes"),
        embed_model=env("EMBED_MODEL", "embeddinggemma:300m"),
        llm_num_ctx=int(env("LLM_NUM_CTX", "16384")),
        llm_timeout_s=float(env("LLM_TIMEOUT_S", "600")),
        public_base_url=public_base_url,
        # Must not exceed OLLAMA_NUM_PARALLEL, or the excess just queues inside Ollama instead.
        llm_concurrency=int(env("LLM_CONCURRENCY", "1")),
        secret_key=env("SECRET_KEY", "dev-only-change-me"),
        # Shared secret between the Cloudflare Email Worker and POST /inbound (unset = disabled).
        inbound_token=env("INBOUND_TOKEN") or None,
        # Where users forward bookings (shown on the account page; routed by the Email Worker).
        inbound_address=env("INBOUND_ADDRESS", "plans@triptych.purplehatstands.com"),
        webauthn_rp_id=env("WEBAUTHN_RP_ID") or urlsplit(public_base_url).hostname or "localhost",
        allowed_origins=allowed_origins,
        admin_origins=origins(env("ADMIN_ORIGINS", "")) or allowed_origins,
        # Tailscale logins (Tailscale-User-Login) allowed into the admin app; empty = nobody.
        admin_logins=frozenset(
            a.strip().lower() for a in env("ADMIN_TAILSCALE_LOGINS", "").split(",") if a.strip()
        ),
        # Where a browser reaches the Restate UI (RESTATE_ADMIN_URL is the in-network address).
        restate_ui_url=env("RESTATE_UI_URL") or None,
        inputs_bucket=env("INPUTS_BUCKET") or None,
        # R2: https://<account id>.r2.cloudflarestorage.com
        inputs_endpoint_url=env("INPUTS_ENDPOINT_URL") or None,
        inputs_access_key_id=env("INPUTS_ACCESS_KEY_ID") or None,
        inputs_secret_access_key=env("INPUTS_SECRET_ACCESS_KEY") or None,
        # A Tink JSON keyset holding one AEAD key (`python -m triptych.inputs new-master-keyset`).
        inputs_master_keyset=env("INPUTS_MASTER_KEYSET") or None,
        evals_bucket=env("EVALS_BUCKET") or None,
        code_version=env("CODE_VERSION", "dev"),
    )


settings = load()

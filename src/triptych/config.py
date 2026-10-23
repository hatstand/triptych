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
    llm_num_ctx: int
    llm_timeout_s: float
    public_base_url: str
    llm_concurrency: int
    secret_key: str
    inbound_token: str | None
    inbound_allowed_senders: frozenset[str]
    admin_logins: frozenset[str]

    # Passkeys are bound to the RP ID: the UI's hostname, or a parent domain of it. Changing it
    # later strands every registered passkey, so set WEBAUTHN_RP_ID to the parent domain up front
    # if the UI might move between subdomains (ts.net itself is a public suffix, so never that).
    webauthn_rp_id: str
    # Origins the UI is served from (scheme://host[:port], exactly as in the address bar). Passkey
    # ceremonies and state-changing requests from any other origin are refused.
    allowed_origins: tuple[str, ...]

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")


def origin_of(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def load() -> Settings:
    env = os.environ.get
    public_base_url = env("PUBLIC_BASE_URL", "http://localhost:8000")
    return Settings(
        restate_ingress_url=env("RESTATE_INGRESS_URL", "http://localhost:8080"),
        database_url=env("DATABASE_URL", "postgresql://triptych:triptych@localhost:5432/triptych"),
        restate_admin_url=env("RESTATE_ADMIN_URL", "http://localhost:9070"),
        worker_public_url=env("WORKER_PUBLIC_URL", "http://worker:9080"),
        ollama_url=env("OLLAMA_URL", "http://localhost:11434"),
        llm_model=env("LLM_MODEL", "gemma4:26b-a4b-it-qat"),
        llm_num_ctx=int(env("LLM_NUM_CTX", "16384")),
        llm_timeout_s=float(env("LLM_TIMEOUT_S", "600")),
        public_base_url=public_base_url,
        # Must not exceed OLLAMA_NUM_PARALLEL, or the excess just queues inside Ollama instead.
        llm_concurrency=int(env("LLM_CONCURRENCY", "1")),
        secret_key=env("SECRET_KEY", "dev-only-change-me"),
        # Shared secret between the Cloudflare Email Worker and POST /inbound (unset = disabled).
        inbound_token=env("INBOUND_TOKEN") or None,
        inbound_allowed_senders=frozenset(
            a.strip().lower() for a in env("INBOUND_ALLOWED_SENDERS", "").split(",") if a.strip()
        ),
        webauthn_rp_id=env("WEBAUTHN_RP_ID") or urlsplit(public_base_url).hostname or "localhost",
        allowed_origins=tuple(
            o.strip().rstrip("/") for o in env("WEBAUTHN_ORIGINS", "").split(",") if o.strip()
        )
        or (origin_of(public_base_url),),
        # Tailscale logins (Tailscale-User-Login) allowed into the admin app; empty = nobody.
        admin_logins=frozenset(
            a.strip().lower() for a in env("ADMIN_TAILSCALE_LOGINS", "").split(",") if a.strip()
        ),
    )


settings = load()

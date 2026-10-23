"""Runtime configuration, read once from the environment."""

import os
from dataclasses import dataclass


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


def load() -> Settings:
    env = os.environ.get
    return Settings(
        restate_ingress_url=env("RESTATE_INGRESS_URL", "http://localhost:8080"),
        database_url=env("DATABASE_URL", "postgresql://triptych:triptych@localhost:5432/triptych"),
        restate_admin_url=env("RESTATE_ADMIN_URL", "http://localhost:9070"),
        worker_public_url=env("WORKER_PUBLIC_URL", "http://worker:9080"),
        ollama_url=env("OLLAMA_URL", "http://localhost:11434"),
        llm_model=env("LLM_MODEL", "gemma4:26b-a4b-it-qat"),
        llm_num_ctx=int(env("LLM_NUM_CTX", "16384")),
        llm_timeout_s=float(env("LLM_TIMEOUT_S", "600")),
        public_base_url=env("PUBLIC_BASE_URL", "http://localhost:8000"),
        # Must not exceed OLLAMA_NUM_PARALLEL, or the excess just queues inside Ollama instead.
        llm_concurrency=int(env("LLM_CONCURRENCY", "1")),
        secret_key=env("SECRET_KEY", "dev-only-change-me"),
        # Shared secret between the Cloudflare Email Worker and POST /inbound (unset = disabled).
        inbound_token=env("INBOUND_TOKEN") or None,
        inbound_allowed_senders=frozenset(
            a.strip().lower() for a in env("INBOUND_ALLOWED_SENDERS", "").split(",") if a.strip()
        ),
    )


settings = load()

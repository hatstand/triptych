# -*- mode: python -*-
# tilt up                       → Strix Halo / ROCm (default)
# tilt up -- --llm=vulkan       → AMD via Vulkan
# tilt up -- --llm=nvidia       → NVIDIA dev box
# tilt up -- --llm=host         → reuse an Ollama already running on the host

config.define_string("llm", usage="LLM backend: rocm | vulkan | nvidia | host")
cfg = config.parse()
llm = cfg.get("llm", os.getenv("TRIPTYCH_LLM", "rocm"))

files = ["compose.yaml"]
if llm == "host":
    files.append("compose/host-llm.yaml")
elif llm in ("rocm", "vulkan", "nvidia"):
    files += ["compose/ollama.yaml", "compose/gpu-%s.yaml" % llm]
else:
    fail("unknown --llm=%s" % llm)
files.append("compose/dev.yaml")

docker_compose(files, env_file=".env" if os.path.exists(".env") else None)

docker_build(
    "triptych-app",
    ".",
    only=["pyproject.toml", "uv.lock", "src", "frontend"],
    live_update=[
        fall_back_on(["pyproject.toml", "uv.lock", "frontend/package.json", "frontend/package-lock.json"]),
        sync("src", "/app/src"),  # watchfiles / uvicorn --reload restart the process
    ],
)

dc_resource("restate", labels=["infra"], links=[link("http://localhost:9070", "Restate UI")])
dc_resource("postgres", labels=["infra"])
dc_resource("migrate", labels=["infra"], resource_deps=["postgres"])
dc_resource("worker", labels=["app"], resource_deps=["restate", "migrate"])
dc_resource("web", labels=["app"], resource_deps=["worker"])
dc_resource("proxy", labels=["app"], resource_deps=["web"], links=[link("http://localhost:8000", "Triptych")])

# Saving a new/edited migration applies it and refreshes db/schema.sql (commit both).
local_resource(
    "db-schema",
    cmd="scripts/dbmate.sh --wait up",
    deps=["db/migrations"],
    labels=["infra"],
    resource_deps=["migrate"],
)
dc_resource("smtp", labels=["app"], resource_deps=["restate"])

if llm != "host":
    dc_resource("ollama", labels=["llm"])
    dc_resource("ollama-pull", labels=["llm"], resource_deps=["ollama"])

# Vite rebuilds frontend/src into src/triptych/web/static, which the sync above ships.
local_resource(
    "frontend",
    serve_cmd="npm ci && npm run watch",
    serve_dir="frontend",
    labels=["tools"],
)

local_resource(
    "send-samples",
    cmd="uv run python scripts/send_samples.py",
    labels=["tools"],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
    resource_deps=["smtp"],
)

local_resource(
    "check",
    cmd="uv run ruff check src tests scripts && uv run ty check"
    + " && uv run python scripts/lint_migrations.py && uv run pytest -q",
    # DB tests create and drop their own throwaway database on the compose Postgres.
    env={"TEST_DATABASE_URL": "postgresql://triptych:triptych@127.0.0.1:5432/postgres"},
    deps=["src", "tests", "db"],
    resource_deps=["db-schema"],
    labels=["tools"],
    allow_parallel=True,
)

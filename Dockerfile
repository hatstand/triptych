# The browser code is TypeScript (frontend/), bundled by Vite with its npm dependencies (htmx)
# into plain JS, with the stylesheet and icon sprite, served from /static under hashed names.
FROM docker.io/library/node:22.23.4-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend ./
RUN npm run build -- --outDir /out

# One image for all three Python processes (worker, web, smtp); compose picks the command.
FROM python:3.14.10-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --system --uid 10001 app

# Dependencies first so code edits don't bust the layer cache.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --no-dev

# The project is installed editable, so Tilt can live-sync ./src (owned by `app`) without a rebuild.
COPY --chown=app src ./src
COPY --chown=app --from=frontend /out ./src/triptych/web/static
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

USER app
CMD ["python", "-m", "triptych.worker"]

# tl-agent — application image.
#
# Runs the 8-phase loop (`tl-agent run`), the CLI (`init-db`, `status`,
# `import-jira`, `reset`), and the Phase 8 web UI (uvicorn). The dependency
# services (Mattermost, jira_mock, GitLab, Phoenix) live in
# infra/docker-compose.yml; this image is wired to reach them by service DNS.
#
# Editable install on purpose: settings.py derives REPO_ROOT from
# `__file__.parents[2]`, so the package must stay at /app/src/tl_agent for
# /app/{config,prompts,data,traces} to resolve. A plain site-packages install
# would point REPO_ROOT at the interpreter dir and break config loading.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# uv for fast, resolver-consistent installs (no committed lock required).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependency + build inputs first so the install layer caches across edits to
# config/prompts. README.md is referenced by pyproject `readme = ...`.
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache -e .

# Runtime data (LAYER 1 markdown config + versioned prompts). Also bind-mounted
# in compose for live edits; COPYed here so the image runs standalone too.
COPY config ./config
COPY prompts ./prompts

# Writable run state. Compose mounts host volumes over these; the mkdir keeps
# standalone `docker run` working without an explicit mount.
RUN mkdir -p /app/data /app/traces

ENTRYPOINT ["tl-agent"]
CMD ["--help"]

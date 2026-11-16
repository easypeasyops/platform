# syntax=docker/dockerfile:1
#
# Two stages: a builder with uv that installs the locked dependencies, and a runtime with
# only the virtualenv and the code. What the platform's service chart needs of the image:
# it runs as UID 10001 (the chart's runAsUser), writes nothing to its filesystem (the chart
# mounts the root read-only) and handles SIGTERM (exec-form CMD).
#
# Base images are pinned by digest (tags move); Dependabot bumps the digests. Keep the two
# FROM lines identical so builder and runtime share one Python.

# ---------------------------------------------------------------------------- builder
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Third-party dependencies only: this layer is rebuilt only when the lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --locked --no-dev --no-install-project

# ---------------------------------------------------------------------------- runtime
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime

# Fixed, non-root UID/GID: predictable file ownership and Kubernetes `runAsNonRoot`.
# The base image's pip, ensurepip's bundled wheels and Debian's apt go: nothing installs at
# run time, so they are attack surface and scanner findings.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && python -m pip uninstall --yes --quiet pip \
 && rm -rf /usr/local/lib/python3.*/ensurepip \
 && dpkg --purge apt libapt-pkg7.0

WORKDIR /app

# Code and dependencies stay owned by root and only readable by `app`: a compromised
# process can't rewrite them. (.dockerignore keeps .env, tests and caches out.)
COPY --from=builder /opt/venv /opt/venv
COPY app ./app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000 9100

# Exec form, so SIGTERM reaches Python and the server drains.
CMD ["python", "-m", "app.main"]

FROM docker.io/node:22.22.0-bookworm-slim AS node-runtime
FROM docker.io/node:24.21.0-bookworm-slim AS node24-runtime

# npm ci stays on Node 22 although eve's engines field asks for Node >=24.
# npm only warns (EBADENGINE) and still installs eve, and the eve harness runs
# it with /opt/node24 at run time. Node 24's npm 11 skips the install scripts
# that the other harnesses need (opencode-ai, koffi, prime-agent).
FROM node-runtime AS harness-deps

# npm installs only the opencode binary packages that match the build CPU, so
# the package names depend on the target architecture (ECS runs arm64).
ARG TARGETARCH
COPY sandbox-runtime/package.json sandbox-runtime/package-lock.json /usr/local/lib/skynet-harnesses/
RUN case "$TARGETARCH" in \
        amd64) OPENCODE_PKGS="opencode-linux-x64 opencode-linux-x64-baseline" ;; \
        arm64) OPENCODE_PKGS="opencode-linux-arm64" ;; \
        *) echo "unsupported arch for opencode: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && npm ci --omit=dev --no-audit --no-fund --prefix /usr/local/lib/skynet-harnesses \
    && rm -rf \
        /usr/local/lib/skynet-harnesses/node_modules/opencode-linux-*-musl \
    && mkdir -p \
        /opt/skynet-harness-layers/anthropic \
        /opt/skynet-harness-layers/codex \
        /opt/skynet-harness-layers/opencode \
        /opt/skynet-harness-layers/pi \
        /opt/skynet-harness-layers/prime/@earendil-works \
    && mv /usr/local/lib/skynet-harnesses/node_modules/@anthropic-ai \
        /opt/skynet-harness-layers/anthropic/ \
    && mv /usr/local/lib/skynet-harnesses/node_modules/@openai \
        /opt/skynet-harness-layers/codex/ \
    && mv /usr/local/lib/skynet-harnesses/node_modules/prime-agent \
        /opt/skynet-harness-layers/prime/ \
    && mv \
        /usr/local/lib/skynet-harnesses/node_modules/@earendil-works/pi-agent-core \
        /usr/local/lib/skynet-harnesses/node_modules/@earendil-works/pi-ai \
        /usr/local/lib/skynet-harnesses/node_modules/@earendil-works/pi-tui \
        /opt/skynet-harness-layers/prime/@earendil-works/ \
    && mv /usr/local/lib/skynet-harnesses/node_modules/@earendil-works \
        /opt/skynet-harness-layers/pi/ \
    && mv /usr/local/lib/skynet-harnesses/node_modules/opencode-ai \
        /opt/skynet-harness-layers/opencode/ \
    && for PKG in $OPENCODE_PKGS; do \
        mv "/usr/local/lib/skynet-harnesses/node_modules/$PKG" /opt/skynet-harness-layers/opencode/ || exit 1; \
    done

# 3.12 for PEP 683 immortal objects: core interpreter objects stop taking
# refcount writes, so job children fork()ed from the fat parent dirty far
# fewer copy-on-write pages. Full test suite verified green on 3.12.
FROM docker.io/python:3.12.15-slim

WORKDIR /app

# OSMesa gives PyOpenGL renderers (pyrender, trimesh viewers) a software GL
# context in the headless sandbox; the scorer runs with PYOPENGL_PLATFORM=osmesa.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl unzip git git-lfs \
        libosmesa6 libgl1 libglu1-mesa tzdata \
    && rm -rf /var/lib/apt/lists/*

# Use UID 1001 with GID 0 — works everywhere (Docker, Podman, OpenShift restricted-v2 SCC).
# OpenShift assigns a random UID at runtime but always with GID 0. The install umask below
# keeps generated runtime files group-writable without copying them into a second OCI layer.
RUN groupadd -g 1001 appuser 2>/dev/null || true \
    && useradd -u 1001 -g 0 -m -s /sbin/nologin appuser \
    && chown 1001:0 /app \
    && chmod 0775 /app

# The pinned harness CLIs require Node >=22; Debian's nodejs may be older.
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# eve needs Node >=24. Keep the other harnesses on Node 22 and give eve its
# own binary; the eve launch puts /opt/node24/bin first on PATH.
COPY --from=node24-runtime /usr/local/bin/node /opt/node24/bin/node

# VCR limits compressed OCI layers to 500 MB. Keep each pinned harness in its
# own layer and omit musl binaries because this Debian image uses glibc.
COPY --from=harness-deps /usr/local/lib/skynet-harnesses /usr/local/lib/skynet-harnesses
COPY --from=harness-deps /opt/skynet-harness-layers/anthropic/ /usr/local/lib/skynet-harnesses/node_modules/
COPY --from=harness-deps /opt/skynet-harness-layers/codex/ /usr/local/lib/skynet-harnesses/node_modules/
COPY --from=harness-deps /opt/skynet-harness-layers/opencode/ /usr/local/lib/skynet-harnesses/node_modules/
COPY --from=harness-deps /opt/skynet-harness-layers/pi/ /usr/local/lib/skynet-harnesses/node_modules/
COPY --from=harness-deps /opt/skynet-harness-layers/prime/ /usr/local/lib/skynet-harnesses/node_modules/
RUN for CLI in pi codex opencode prime-agent; do \
        if [ -e "/usr/local/lib/skynet-harnesses/node_modules/.bin/$CLI" ]; then \
            ln -s "/usr/local/lib/skynet-harnesses/node_modules/.bin/$CLI" "/usr/local/bin/$CLI"; \
        fi; \
    done \
    && printf '#!/bin/sh\nexec /opt/node24/bin/node /usr/local/lib/skynet-harnesses/node_modules/eve/bin/eve.js "$@"\n' \
        > /usr/local/bin/eve \
    && chmod 0755 /usr/local/bin/eve \
    && eve --version | grep -Eq '(^| )1[.]0[.]0-rc[.]6( |$)'

COPY pyproject.toml uv.lock requirements.txt ./

# Dependency installer toggle. PKG_MANAGER=uv (default) installs the exact
# uv.lock wheel set via uv. PKG_MANAGER=pip installs requirements.txt, the
# same set exported from uv.lock, for hosts without uv:
#   docker build --build-arg PKG_MANAGER=pip ...
# Either way the deps land in their own layer (before the source copy) so it
# caches until the lockfile / requirements change. requirements.txt is a
# complete compiled lockfile, so we install with --no-deps to skip
# re-resolution. It pins a gepa git commit for the upstream OA engines and
# GEPA PxN sampling, but dspy's metadata hard-pins gepa==0.1.4 from PyPI, so a
# resolving install would pull the release without gepa.oa. --no-deps
# preserves this deliberate override; the uv path carries the same override
# in uv.lock ([tool.uv] override-dependencies). Compatibility is exercised by
# the pip-path image smoke tests.
ARG PKG_MANAGER=uv
RUN if [ "$PKG_MANAGER" = "pip" ]; then \
        umask 0002; \
        pip install --no-cache-dir --no-deps -r requirements.txt; \
    else \
        umask 0002; \
        pip install --no-cache-dir uv \
        && UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-install-project; \
    fi

# Prime Agent runs model code in a Python kernel it normally bootstraps on
# first use by downloading uv, Python and packages. Protected sandboxes have no
# network, so build that kernel here; the Prime launch uses it read-only when
# present (harness.PRIME_AGENT_IMAGE_KERNEL).
COPY sandbox-runtime/prime-agent-kernel.txt /opt/prime-agent/requirements.txt
RUN python -m venv /opt/prime-agent/kernel-venv \
    && /opt/prime-agent/kernel-venv/bin/python -m pip install --no-cache-dir --no-deps \
        -r /opt/prime-agent/requirements.txt \
        /usr/local/lib/skynet-harnesses/node_modules/prime-agent/dist/prime-agent-runtime \
    && /opt/prime-agent/kernel-venv/bin/python -c "import rlm, dill, yaml, dotenv, pandas, scipy, bs4, lxml, pydantic, tyro"

# ShinkaEvolve pins its own closure (openai, anthropic, numpy, pandas, ...) that
# conflicts with the backend's, so it gets its own venv instead of /app/.venv.
# Protected sandboxes have no network to build it at run time; the native
# runtime uses this interpreter when present (native_runtime.SHINKA_IMAGE_PYTHON)
# and the readiness probe reports shinka_ready from it.
# Off by default: the layer is ~700 MB and carries a GPL package (Levenshtein),
# so only the sandbox image build opts in with
# --build-arg INCLUDE_SHINKA_EVOLVE=1; API/worker images stay without it.
ARG INCLUDE_SHINKA_EVOLVE=false
COPY core/service_gateway/optimization/blackbox/shinka_requirements.txt /opt/shinka/requirements.txt
RUN if [ "$INCLUDE_SHINKA_EVOLVE" = "1" ] || [ "$INCLUDE_SHINKA_EVOLVE" = "true" ]; then \
        python -m venv /opt/shinka/venv \
        && /opt/shinka/venv/bin/python -m pip install --no-cache-dir --no-deps \
            -r /opt/shinka/requirements.txt \
        && /opt/shinka/venv/bin/python -c "import shinka.core.async_runner"; \
    fi

# dspy.Flex serve-time dependency. An optimized Flex program ALWAYS executes
# its generated Python in a Deno/Pyodide WASM sandbox (dspy.PythonInterpreter
# shells out to `deno` on PATH) — even the un-optimized baseline runs through
# the interpreter — so the runtime image needs the deno binary plus a warm
# npm:pyodide cache (~40MB binary + ~14MB pyodide).
# DENO_DIR must match at build and run time so the runtime sandbox hits the
# cache primed here; it lives under /app so the chgrp/chmod below makes it
# group-0 writable for the runtime v8 code cache.
ARG DENO_VERSION=2.6.6
ENV DENO_DIR=/app/.deno
RUN umask 0002; \
        case "$(dpkg --print-architecture)" in \
            amd64) DENO_TARGET=x86_64-unknown-linux-gnu ;; \
            arm64) DENO_TARGET=aarch64-unknown-linux-gnu ;; \
            *) echo "unsupported arch for Deno: $(dpkg --print-architecture)" >&2; exit 1 ;; \
        esac \
        && curl -fsSL --retry 10 --retry-all-errors --retry-delay 2 --continue-at - \
            "https://github.com/denoland/deno/releases/download/v$DENO_VERSION/deno-$DENO_TARGET.zip" -o /tmp/deno.zip \
        && unzip -o /tmp/deno.zip -d /usr/local/bin \
        && rm /tmp/deno.zip \
        && chmod +x /usr/local/bin/deno \
        && if [ "$PKG_MANAGER" = "uv" ]; then PY=/app/.venv/bin/python; else PY=python; fi \
        && RUNNER_JS=$("$PY" -c 'import os, dspy.primitives.python_interpreter as m; print(os.path.join(os.path.dirname(m.__file__), "runner.js"))') \
        && deno cache "$RUNNER_JS"

COPY --chown=1001:0 . .

# Install the project itself. pip does an editable --no-deps install (every
# dependency is already present from the layer above); uv re-syncs now that the
# project source is in place.
RUN if [ "$PKG_MANAGER" = "pip" ]; then \
        umask 0002; \
        pip install --no-cache-dir --no-deps -e .; \
    else \
        umask 0002; \
        UV_PYTHON_DOWNLOADS=never uv sync --frozen; \
    fi

ARG GIT_SHA=unknown

# Login shells must retain the same interpreter used to install the pinned dependencies.
RUN printf '%s\n' 'export PATH="/app/.venv/bin:$PATH"' > /etc/profile.d/skynet-runtime.sh

# Codex turns unified exec on unless a root-owned /etc/codex/requirements.toml
# turns it off (SKY-389). The image ships that file, so a Vercel box needs no
# root step and the image carries no sudo: Codex runs as appuser with its own
# sandbox off, and any root path would let the model rewrite the file.
# Keep the content equal to CODEX_REQUIREMENTS in
# core/service_gateway/optimization/blackbox/sandbox_runtime.py.
RUN install -d -o root -g root -m 0755 /etc/codex \
    && printf '[features]\nunified_exec = false\n' > /etc/codex/requirements.toml \
    && chown root:root /etc/codex/requirements.toml \
    && chmod 0644 /etc/codex/requirements.toml

USER 1001

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV WORKER_CONCURRENCY=4
ENV WORKER_POLL_INTERVAL=2.0
ENV SKYNET_CODE_VERSION=$GIT_SHA
ENV LOG_LEVEL=INFO
# Containerized runs default to structured logs; override to "text" only
# when running the image interactively for debugging.
ENV LOG_FORMAT=json
# litellm uses its bundled cost map instead of fetching model_prices_and_context_window.json from GitHub on import.
ENV LITELLM_LOCAL_MODEL_COST_MAP=True
# Recommendation embeddings are served by an internal OpenAI-compatible API;
# no model weights are loaded inside the backend container.
# REMOTE_DB_URL must be set at runtime (e.g., via docker-compose or -e flag)
# ENV REMOTE_DB_URL=postgresql://user:password@host:5432/skynet

EXPOSE 8000

# One image, two roles: the API (default) and the standalone job worker
# (SERVICE_ROLE=worker, no HTTP). Both services deploy from this
# same Dockerfile; the env var picks the entrypoint so neither service needs
# a dashboard-only custom start command.
# Bind the platform-provided PORT when present (PaaS runtimes inject it and
# route edge traffic to it), else 8000. The command exports that port as
# API_PORT, which settings.port reads first, so settings.port and the
# generalist agent's derived MCP self-dial URL always match the bind, even when
# docker compose passes an API_PORT from backend/.env. The sh -c + exec keeps
# the process as PID 1 for clean SIGTERM handling.
# Python decides the bind, so local mode is read exactly as Settings reads it
# (any case, and backend/.env). It prints the checked loopback API_HOST in
# local mode and nothing otherwise. If the local-mode guard fails, the import
# fails too and the server refuses to start, so it never binds 0.0.0.0 in
# local mode.
# Uvicorn trusts X-Forwarded-For/-Proto only from FORWARDED_ALLOW_IPS; behind a
# load balancer, set it to the balancer's subnet CIDRs. Unset keeps uvicorn's
# own default (127.0.0.1).
CMD ["sh", "-c", "if [ \"$SERVICE_ROLE\" = \"worker\" ]; then exec python worker_main.py; else export API_PORT=\"${PORT:-8000}\"; LOCAL_HOST=$(python -c 'from core.config import local_bind_host; print(local_bind_host())' 2>/dev/null | tail -n 1); exec uvicorn main:app --host \"${LOCAL_HOST:-0.0.0.0}\" --port \"$API_PORT\" --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\"; fi"]

# syntax=docker/dockerfile:1
# This is a slimmed-down Open WebUI build:
#   - no CUDA / torch / sentence-transformers / faster-whisper
#   - embeddings are provided by an OpenAI-compatible API (RAG_EMBEDDING_ENGINE)
#   - no Ollama
#
# The python dependencies are installed in a builder stage (with the compiler)
# and only the resulting virtualenv is copied into the runtime image, so the
# toolchain never ships to production.
ARG USE_PERMISSION_HARDENING=false

# Tiktoken encoding name; models to use can be found at https://huggingface.co/models?library=tiktoken
ARG USE_TIKTOKEN_ENCODING_NAME="cl100k_base"

ARG BUILD_HASH=dev-build
# Override at your own risk - non-root configurations are untested
ARG UID=0
ARG GID=0

######## WebUI frontend ########
FROM --platform=$BUILDPLATFORM node:22-alpine3.20 AS build
ARG BUILD_HASH

# Keep V8 heap high enough for this frontend build, but still bounded for a small VPS.
ENV NODE_OPTIONS="--max-old-space-size=4096"
ENV CYPRESS_INSTALL_BINARY=0
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

# to store git revision in build
RUN apk add --no-cache git

COPY package.json package-lock.json ./
RUN npm config set fetch-retries 5 \
    && npm config set fetch-retry-factor 2 \
    && npm config set fetch-retry-mintimeout 20000 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && i=0; until [ "$i" -ge 3 ]; do \
        npm ci --force && break; \
        i=$((i + 1)); \
        echo "npm ci failed, retrying (${i}/3)..." >&2; \
        sleep 5; \
    done \
    && [ "$i" -lt 3 ]

COPY . .
ENV APP_BUILD_HASH=${BUILD_HASH}
RUN npm run build

######## WebUI backend - dependency builder ########
FROM python:3.11-slim-bookworm AS builder

ARG USE_TIKTOKEN_ENCODING_NAME

ENV PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UV_LINK_MODE=copy \
    TIKTOKEN_ENCODING_NAME=$USE_TIKTOKEN_ENCODING_NAME \
    TIKTOKEN_CACHE_DIR="/app/backend/data/cache/tiktoken"

# Compiler and headers are needed only here, not in the runtime image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential gcc \
    libmariadb-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY ./backend/requirements.txt ./requirements.txt

RUN set -e; \
    pip3 install --no-cache-dir uv; \
    uv venv /opt/venv; \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache-dir -r requirements.txt; \
    /opt/venv/bin/python -c "import os, tiktoken; tiktoken.get_encoding(os.environ['TIKTOKEN_ENCODING_NAME'])";

######## WebUI backend - runtime ########
FROM python:3.11-slim-bookworm AS base

# Use args
ARG USE_PERMISSION_HARDENING
ARG USE_TIKTOKEN_ENCODING_NAME
ARG UID
ARG GID

# Python settings
ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

## Basis ##
ENV ENV=prod \
    PORT=8080 \
    USE_SLIM_DOCKER=true \
    USE_PERMISSION_HARDENING_DOCKER=${USE_PERMISSION_HARDENING}

## Basis URL Config ##
ENV OLLAMA_BASE_URL="" \
    OPENAI_API_BASE_URL=""

## API Key and Security Config ##
ENV OPENAI_API_KEY="" \
    WEBUI_SECRET_KEY="" \
    SCARF_NO_ANALYTICS=true \
    DO_NOT_TRACK=true \
    ANONYMIZED_TELEMETRY=false

## Tiktoken model settings ##
ENV TIKTOKEN_ENCODING_NAME=$USE_TIKTOKEN_ENCODING_NAME \
    TIKTOKEN_CACHE_DIR="/app/backend/data/cache/tiktoken"

## Hugging Face download cache ##
ENV HF_HOME="/app/backend/data/cache/embedding/models"

WORKDIR /app/backend

ENV HOME=/root
# Create user and group if not root
RUN if [ $UID -ne 0 ]; then \
    if [ $GID -ne 0 ]; then \
    addgroup --gid $GID app; \
    fi; \
    adduser --uid $UID --gid $GID --home $HOME --disabled-password --no-create-home app; \
    fi

RUN mkdir -p $HOME/.cache/chroma
RUN echo -n 00000000-0000-0000-0000-000000000000 > $HOME/.cache/chroma/telemetry_user_id

# Make sure the user has access to the app and root directory
RUN chown -R $UID:$GID /app $HOME

# Runtime system dependencies only (no compiler, no dev headers).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git pandoc \
    netcat-openbsd curl jq \
    ffmpeg libsm6 libxext6 zstd \
    && rm -rf /var/lib/apt/lists/*

# Copy the prepared virtualenv with all python dependencies.
COPY --from=builder --chown=$UID:$GID /opt/venv /opt/venv

# copy built frontend files
COPY --chown=$UID:$GID --from=build /app/build /app/build
COPY --chown=$UID:$GID --from=build /app/CHANGELOG.md /app/CHANGELOG.md
COPY --chown=$UID:$GID --from=build /app/package.json /app/package.json

# copy backend files
COPY --chown=$UID:$GID ./backend .

EXPOSE 8080

HEALTHCHECK CMD curl --silent --fail http://localhost:${PORT:-8080}/health | jq -ne 'input.status == true' || exit 1

# Minimal, atomic permission hardening for OpenShift (arbitrary UID):
# - Group 0 owns /app and /root
# - Directories are group-writable and SGID so new files inherit GID 0
RUN if [ "$USE_PERMISSION_HARDENING" = "true" ]; then \
    set -eux; \
    chgrp -R 0 /app /root || true; \
    chmod -R g+rwX /app /root || true; \
    find /app -type d -exec chmod g+s {} + || true; \
    find /root -type d -exec chmod g+s {} + || true; \
    fi

USER $UID:$GID

ARG BUILD_HASH
ENV WEBUI_BUILD_VERSION=${BUILD_HASH}
ENV DOCKER=true

CMD [ "bash", "start.sh"]

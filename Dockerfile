# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS builder

WORKDIR /build

COPY requirements.build.lock requirements.runtime.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.build.lock \
    && python -m pip wheel --no-cache-dir --require-hashes \
        --wheel-dir /wheelhouse -r requirements.runtime.lock

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --no-deps --no-build-isolation \
    --wheel-dir /wheelhouse .

FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ARG SOURCE_REVISION=unknown
ARG PROJECT_VERSION=0.3.0
LABEL org.opencontainers.image.title="obsidian-mattermost-notifier" \
      org.opencontainers.image.description="Mattermost notifications for LiveSync-mirrored Obsidian vaults" \
      org.opencontainers.image.version="${PROJECT_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.source="https://github.com/smilejk930/obsidian-mattermost-notifier"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=builder /wheelhouse /wheelhouse
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheelhouse \
        "obsidian-mattermost-notifier==${PROJECT_VERSION}" \
    && rm -rf /wheelhouse \
    && mkdir -p /app \
    && chown 10001:10001 /app

COPY --chmod=0555 deploy/notifier/entrypoint.sh /usr/local/bin/notifier-entrypoint

WORKDIR /app
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["notifier-entrypoint"]
CMD ["obsidian-mattermost-notifier", "--config", "/etc/obsidian-mattermost-notifier/config.yaml"]

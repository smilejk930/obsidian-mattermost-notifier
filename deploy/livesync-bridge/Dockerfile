FROM denoland/deno:2.6.9@sha256:2221eafd8a7556693307ad9889a79d4974e1844a6964acbe466ab9faa15d37bf

ARG LIVESYNC_BRIDGE_REVISION=unknown
ARG DEPLOYMENT_REVISION=unknown

ENV DENO_DIR=/var/lib/livesync-bridge/deno \
    DENO_NO_PROMPT=1 \
    DENO_NO_UPDATE_CHECK=1 \
    LSB_BAKED_DENO_DIR=/opt/livesync-bridge-deno-cache

WORKDIR /app
RUN install -d -o deno -g deno /app /var/lib/livesync-bridge \
    && install -d -o deno -g deno "${LSB_BAKED_DENO_DIR}" \
    && printf '%s:%s:%s\n' \
        "${DENO_VERSION}" "${LIVESYNC_BRIDGE_REVISION}" "${DEPLOYMENT_REVISION}" \
        > /usr/local/share/livesync-bridge-cache-version

COPY --chmod=0555 .notifier-deploy/entrypoint.sh /usr/local/bin/livesync-bridge-entrypoint

USER deno

COPY --chown=deno:deno . .
RUN DENO_DIR="${LSB_BAKED_DENO_DIR}" \
    deno install --frozen --entrypoint main.ts

ENTRYPOINT ["/tini", "--", "livesync-bridge-entrypoint"]
CMD ["deno", "run", "--cached-only", "--frozen", "-A", "main.ts"]

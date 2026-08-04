FROM denoland/deno:2.6.9@sha256:2221eafd8a7556693307ad9889a79d4974e1844a6964acbe466ab9faa15d37bf

WORKDIR /app
RUN chown deno:deno /app

USER deno

COPY --chown=deno:deno . .
RUN deno install --frozen

CMD ["deno", "task", "run"]

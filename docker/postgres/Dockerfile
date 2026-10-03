# Keep the same PostgreSQL major, distribution and pgvector ABI as existing volumes.
FROM pgvector/pgvector:0.8.6-pg16-bookworm AS vector_base
FROM vector_base

# Release checksums from the upstream v0.25.11 release assets (not a floating latest).
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) checksum="6dae5ed256c7eac0d1e3b8aed8011ef35ec26157db6f682b34187accbe736d35" ;; \
        arm64) checksum="9f9fe916fc5927337df50235296eed84061303673643e7db701b5099507380cb" ;; \
        *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --retry 3 --output /tmp/pg_search.deb \
       "https://github.com/paradedb/paradedb/releases/download/v0.25.11/postgresql-16-pg-search_0.25.11-1PARADEDB-bookworm_${arch}.deb" \
    && echo "${checksum}  /tmp/pg_search.deb" | sha256sum --check - \
    && apt-get install --no-install-recommends -y /tmp/pg_search.deb \
    && rm /tmp/pg_search.deb \
    && rm -rf /var/lib/apt/lists/*

# Installing package dependencies must not replace pgvector 0.8.6 with an older ABI.
COPY --from=vector_base /usr/lib/postgresql/16/lib/vector.so /usr/lib/postgresql/16/lib/vector.so
COPY --from=vector_base /usr/share/postgresql/16/extension/vector* /usr/share/postgresql/16/extension/

RUN test -f /usr/share/postgresql/16/extension/pg_search.control \
    && grep -q "0.8.6" /usr/share/postgresql/16/extension/vector.control

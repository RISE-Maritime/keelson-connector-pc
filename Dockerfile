FROM python:3.13-slim-bookworm

# tini for signal handling (signed Debian package)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# The package directory is copied before the install because, unlike connectors
# whose bin/ scripts are self-contained, pc2keelson imports keelson_connector_pc
# and setuptools needs the sources present to install it.
COPY pyproject.toml pyproject.toml
COPY README.md README.md
COPY keelson_connector_pc keelson_connector_pc
RUN uv pip install --system --no-cache .

COPY --chmod=555 ./bin/* /usr/local/bin/

# Run as non-root (principle of least privilege). Reading host metrics needs no
# privilege beyond what the bind mounts in docker-compose.yml already grant;
# per-process metrics for processes owned by other users need `pid: host` and
# will report only what this uid may see.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/bin/bash", "-c"]

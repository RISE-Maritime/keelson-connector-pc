FROM python:3.13-slim-bookworm

# tini for signal handling (signed Debian package)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Build in a directory of its own. Without a WORKDIR the build runs at /, and
# [tool.setuptools.packages.find] has no `where`, so setuptools' package
# auto-discovery walks the entire root filesystem before filtering by the
# include pattern -- which stalls the build for minutes or forever.
WORKDIR /app

# Installing the package is what puts `pc2keelson` on PATH: setuptools generates
# it from [project.scripts]. bin/pc2keelson.py is only a checkout convenience and
# is deliberately NOT copied in -- it would shadow the console script with a file
# that is not executable under that name.
COPY pyproject.toml pyproject.toml
COPY README.md README.md
COPY keelson_connector_pc keelson_connector_pc
RUN uv pip install --system --no-cache .

# Run as non-root (principle of least privilege). Reading host metrics needs no
# privilege beyond what the bind mounts in docker-compose.computer.yml already grant;
# per-process metrics for processes owned by other users need `pid: host` and
# will report only what this uid may see.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/bin/bash", "-c"]

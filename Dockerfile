FROM python:3.13-slim-bookworm

# tini for signal handling (signed Debian package)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Never build from /. With no WORKDIR the project root is the filesystem root,
# and setuptools' package discovery walks it with os.walk(followlinks=True):
# /proc/<pid>/root links back to /, so the walk never ends and the build is
# eventually OOM-killed ("cannot allocate memory" after ~100 min under QEMU).
WORKDIR /app

# The package directory is copied before the install because, unlike connectors
# whose bin/ scripts are self-contained, pc2keelson imports keelson_connector_pc
# and setuptools needs the sources present to install it.
COPY pyproject.toml pyproject.toml
COPY README.md README.md
COPY keelson_connector_pc keelson_connector_pc
RUN uv pip install --system --no-cache .

# Installed without the .py suffix: docker-compose.computer.yml and the README
# run the command as `pc2keelson`.
COPY --chmod=555 ./bin/pc2keelson.py /usr/local/bin/pc2keelson

# Run as non-root (principle of least privilege). Reading host metrics needs no
# privilege beyond what the bind mounts in docker-compose.computer.yml already grant;
# per-process metrics for processes owned by other users need `pid: host` and
# will report only what this uid may see.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/bin/bash", "-c"]

# ser2net — serial-to-network bridge with a web admin UI.
# Build:  docker build -t ser2net .
# Run:    docker compose up -d (docker-compose.yml), or the `docker run` command in
#         docs/DOCKER.md: restart policy, mapping ports, serial devices, log limits.
# The base: CPython 3.12 on Debian 13 "trixie" (what python:3.12-slim points at today). The
# codename keeps a rebuild on Debian point releases and 3.12.x patches, never a new Debian
# major under an existing version tag. Releases and the weekly refresh
# (.github/workflows/image-refresh.yml) build FROM this tag resolved to a digest, and
# record it in the label org.opencontainers.image.base.digest (docs/DOCKER.md).
ARG BASE_IMAGE=python:3.12-slim-trixie
FROM ${BASE_IMAGE}

WORKDIR /app

# Install dependencies first for better layer caching. We install straight from
# PyPI (the bundled vendor/wheels offline path is for air-gapped hosts, not the
# image build), so the app runs with --no-bootstrap. requirements-lock.txt holds the
# pins of requirements.txt and of the optional feature packages (MQTT / LDAP / OIDC,
# requirements-optional.txt), so those work in the container out of the box, each with
# the sha256 of its files: pip refuses any other file and any unlocked dependency.
COPY requirements-lock.txt ./
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-lock.txt

# ser2net runs as this unprivileged account (tools/docker-entrypoint.sh drops to it;
# it also takes over a /data volume that an older, root-running image wrote). /data is
# private from the start, so a new volume is that also with a folder mounted inside it
# (ser2net locks only a data directory that holds nothing but its own files).
RUN groupadd --system --gid 10001 ser2net \
 && useradd --system --uid 10001 --gid ser2net --home-dir /data --no-create-home \
      --shell /usr/sbin/nologin ser2net \
 && mkdir -p /data && chown ser2net:ser2net /data && chmod 0700 /data

# (.dockerignore lets in only what the image runs, plus the license texts)
COPY . .
# Compile the app once here: the server runs as an unprivileged user that can't write
# /app, so it would otherwise compile every module in memory at each start. checked-hash
# .pyc files stay valid whatever timestamps the layers keep, and are checked against the
# source, so sources mounted over /app/app are still used.
RUN chmod 0755 /app/tools/docker-entrypoint.sh \
 && python -m compileall -q -j0 --invalidation-mode checked-hash /app/app

# Config + logs live on a volume so they survive container recreation.
VOLUME ["/data"]
EXPOSE 8080

# Bind the admin UI to all interfaces inside the container (the container network
# boundary + the published port are the real perimeter). Always set an admin
# password on first access, and put TLS in front for untrusted networks.
ENV SER2NET_BIND_IP=0.0.0.0 \
    SER2NET_PORT=8080

# "healthy" once the web UI answers on /healthz (also before the first-run setup)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "/app/ser2net.py", "--no-bootstrap", "--data-dir", "/data", "--healthcheck"]

ENTRYPOINT ["/app/tools/docker-entrypoint.sh"]

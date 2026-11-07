# ser2net — serial-to-network bridge with a web admin UI.
# Build:  docker build -t ser2net .
# Run:    docker compose up -d (docker-compose.yml), or the `docker run` command in
#         docs/DOCKER.md: restart policy, mapping ports, serial devices, log limits.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching. We install straight from
# PyPI (the bundled vendor/wheels offline path is for air-gapped hosts, not the
# image build), so the app runs with --no-bootstrap. The optional feature deps
# (MQTT / LDAP / OIDC, requirements-optional.txt) are included so those work in the
# container out of the box.
COPY requirements.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-optional.txt

# ser2net runs as this unprivileged account (tools/docker-entrypoint.sh drops to it;
# it also takes over a /data volume that an older, root-running image wrote).
RUN groupadd --system --gid 10001 ser2net \
 && useradd --system --uid 10001 --gid ser2net --home-dir /data --no-create-home \
      --shell /usr/sbin/nologin ser2net \
 && mkdir -p /data && chown ser2net:ser2net /data

COPY . .
RUN chmod 0755 /app/tools/docker-entrypoint.sh

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

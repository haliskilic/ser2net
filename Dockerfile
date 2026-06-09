# ser2net — serial-to-network bridge with a web admin UI.
# Build:  docker build -t ser2net .
# Run:    docker run -d --name ser2net -p 8080:8080 \
#           --device /dev/ttyUSB0 --group-add dialout \
#           -v ser2net-data:/data ser2net
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching. We install straight from
# PyPI (the bundled vendor/wheels offline path is for air-gapped hosts, not the
# image build), so the app runs with --no-bootstrap. The optional feature deps
# (MQTT / LDAP / OIDC) are included so those work in the container out of the box.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt paho-mqtt ldap3 authlib

COPY . .

# Config + logs live on a volume so they survive container recreation.
VOLUME ["/data"]
EXPOSE 8080

# Bind the admin UI to all interfaces inside the container (the container network
# boundary + the published port are the real perimeter). Always set an admin
# password on first access, and put TLS in front for untrusted networks.
ENV SER2NET_BIND_IP=0.0.0.0 \
    SER2NET_PORT=8080

ENTRYPOINT ["python", "ser2net.py", "--no-bootstrap", "--data-dir", "/data"]

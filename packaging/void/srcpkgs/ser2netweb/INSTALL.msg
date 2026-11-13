Enable the service with: ln -s /etc/sv/ser2netweb /var/service/
The web UI then listens on 127.0.0.1:8080 (/etc/sv/ser2netweb/conf can move it);
set the first admin password there, or with
  chpst -u _ser2netweb ser2netweb --data-dir /var/lib/ser2netweb --set-password admin
Optional features: python3-paho_mqtt (MQTT), python3-ldap3 (LDAP sign-in),
python3-cryptography (self-signed certificates). OpenID Connect sign-in needs
Authlib, which Void doesn't package yet.

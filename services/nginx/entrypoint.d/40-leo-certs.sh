#!/bin/sh
# Wait for a certificate (certbot writes a temporary self-signed one first), then reload
# nginx whenever certbot replaces it with the real / renewed Let's Encrypt certificate.
set -eu
cert=/etc/leo-certs/fullchain.pem
i=0
while [ ! -s "$cert" ] && [ $i -lt 120 ]; do sleep 1; i=$((i + 1)); done
[ -s "$cert" ] || { echo "leo: no certificate in /etc/leo-certs after 120 s" >&2; exit 1; }
(
  last="$(md5sum "$cert")"
  while sleep 60; do
    now="$(md5sum "$cert" 2>/dev/null || true)"
    if [ -n "$now" ] && [ "$now" != "$last" ]; then
      last="$now"
      echo "leo: certificate changed — reloading nginx"
      nginx -s reload || true
    fi
  done
) &

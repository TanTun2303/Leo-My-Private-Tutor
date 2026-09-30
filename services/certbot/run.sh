#!/bin/sh
# Obtain and renew the Let's Encrypt certificate for $LEO_EDGE_DOMAIN (HTTP-01 via nginx's webroot)
# and publish it to /etc/leo-certs, which nginx watches.
set -eu
: "${LEO_EDGE_DOMAIN:?}"
out=/etc/leo-certs
live="/etc/letsencrypt/live/$LEO_EDGE_DOMAIN"
mkdir -p "$out" /var/www/certbot

publish() {
  cp -L "$live/fullchain.pem" "$out/fullchain.pem.new"
  cp -L "$live/privkey.pem" "$out/privkey.pem.new"
  chmod 600 "$out/privkey.pem.new"
  mv "$out/privkey.pem.new" "$out/privkey.pem"
  mv "$out/fullchain.pem.new" "$out/fullchain.pem"
  echo "leo: published certificate for $LEO_EDGE_DOMAIN"
}

# Temporary self-signed certificate so nginx can start before the first issuance.
if [ ! -s "$out/fullchain.pem" ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 2 -subj "/CN=$LEO_EDGE_DOMAIN" \
    -keyout "$out/privkey.pem" -out "$out/fullchain.pem" 2>/dev/null
  echo "leo: wrote temporary self-signed certificate"
fi

staging=""
[ "${LEO_EDGE_STAGING:-false}" = "true" ] && staging="--staging"

while true; do
  if [ -s "$live/fullchain.pem" ]; then
    certbot renew --webroot -w /var/www/certbot --quiet --non-interactive && publish
  else
    # --register-unsafely-without-email: no personal data sent; set LEO_EDGE_EMAIL for expiry mails.
    if [ -n "${LEO_EDGE_EMAIL:-}" ]; then acct="--email $LEO_EDGE_EMAIL"; else acct="--register-unsafely-without-email"; fi
    # shellcheck disable=SC2086
    if certbot certonly --webroot -w /var/www/certbot -d "$LEO_EDGE_DOMAIN" $acct $staging \
         --agree-tos --non-interactive --keep-until-expiring; then
      publish
    else
      echo "leo: issuance failed — retrying in 10 minutes" >&2
      sleep 600
      continue
    fi
  fi
  sleep 43200   # 12 h
done

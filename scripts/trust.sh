#!/usr/bin/env bash
# Export Caddy's local root CA so browsers/devices can trust https://$LEO_DOMAIN.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

out="$LEO_ROOT/leo-root-ca.crt"
compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$out" \
  || die "could not read the CA — is caddy running and has it served one HTTPS request?"
log "Wrote $out"
cat <<MSG

  Linux (system + Chrome/Firefox using NSS):
    sudo cp leo-root-ca.crt /usr/local/share/ca-certificates/leo-root-ca.crt && sudo update-ca-certificates
    certutil -d sql:\$HOME/.pki/nssdb -A -t C,, -n "Leo Local CA" -i leo-root-ca.crt   # Chrome (libnss3-tools)
    Firefox: Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import

  Windows (PowerShell as the user):
    Import-Certificate -FilePath .\\leo-root-ca.crt -CertStoreLocation Cert:\\CurrentUser\\Root

  Then restart the browser and open https://$(env_get LEO_DOMAIN || echo leo.localhost)
MSG

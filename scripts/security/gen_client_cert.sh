#!/usr/bin/env bash
# Issue the GCS / station client certificate signed by the station CA
# (ADR-0007 §4). Used by `rescue_ai/infrastructure/rpi_client.py` via
# httpx `cert=(client_crt, client_key)` when TLS_MODE=mtls.

set -euo pipefail

OUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/out"
CA_KEY="$OUT_DIR/station-root-ca.key"
CA_CRT="$OUT_DIR/station-root-ca.crt"

if [[ ! -f "$CA_KEY" || ! -f "$CA_CRT" ]]; then
  echo "Run scripts/security/gen_ca.sh first." >&2
  exit 1
fi

CN="${CLIENT_CN:-gcs-client}"
DAYS="${CLIENT_DAYS:-365}"

KEY="$OUT_DIR/gcs-client.key"
CSR="$OUT_DIR/gcs-client.csr"
CRT="$OUT_DIR/gcs-client.crt"
EXT="$OUT_DIR/gcs-client.ext"

cat > "$EXT" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

openssl genrsa -out "$KEY" 2048
openssl req -new -key "$KEY" -subj "/CN=$CN" -out "$CSR"
openssl x509 -req \
  -in "$CSR" \
  -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
  -out "$CRT" \
  -days "$DAYS" \
  -sha256 \
  -extfile "$EXT"

chmod 600 "$KEY"
chmod 644 "$CRT"
rm -f "$CSR" "$EXT"

echo "Created:"
echo "  $CRT"
echo "  $KEY"
echo "Set in .env:"
echo "  TLS_MODE=mtls"
echo "  TLS_CA_CERT_PATH=$CA_CRT"
echo "  TLS_CLIENT_CERT_PATH=$CRT"
echo "  TLS_CLIENT_KEY_PATH=$KEY"

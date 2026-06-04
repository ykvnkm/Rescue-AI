#!/usr/bin/env bash
# Generate the self-signed station root CA used by the GCS<->RPi mTLS
# link (ADR-0007 §4). One-shot: run on the operator's laptop, then
# distribute the output to the station and Raspberry Pi.

set -euo pipefail

OUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/out"
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

CA_KEY="$OUT_DIR/station-root-ca.key"
CA_CRT="$OUT_DIR/station-root-ca.crt"
DAYS="${CA_DAYS:-3650}"
SUBJECT="${CA_SUBJECT:-/C=RU/O=Rescue-AI/OU=Station/CN=station-root-ca}"

if [[ -f "$CA_KEY" || -f "$CA_CRT" ]]; then
  echo "CA already exists at $OUT_DIR; refusing to overwrite." >&2
  exit 1
fi

openssl genrsa -out "$CA_KEY" 4096
openssl req -x509 -new -nodes \
  -key "$CA_KEY" \
  -sha256 \
  -days "$DAYS" \
  -subj "$SUBJECT" \
  -out "$CA_CRT"

chmod 600 "$CA_KEY"
chmod 644 "$CA_CRT"

echo "Created:"
echo "  $CA_CRT"
echo "  $CA_KEY"

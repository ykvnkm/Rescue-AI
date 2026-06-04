#!/usr/bin/env bash
# Generate a shared-secret bearer token for the HTTP API (cloud/controlled
# deployments). Print it once; store it in the deployment secret (Vault / k8s
# Secret) as API_AUTH_TOKEN and hand it to authorized operators out-of-band.
#
# Public demo deployments leave API_AUTH_TOKEN unset (gate disabled) and rely
# on APP_RATE_LIMIT_PER_MIN + the single-active-session guard instead — see
# docs/runbooks/public_demo_access.md.

set -euo pipefail

TOKEN="$(openssl rand -hex 32)"

echo "API_AUTH_TOKEN=$TOKEN"
echo
echo "Next steps:"
echo "  1) Store it in the deployment secret, e.g.:"
echo "       kubectl create secret generic rescue-ai-api-auth \\"
echo "         --from-literal=API_AUTH_TOKEN=$TOKEN"
echo "  2) Wire the secret into the api Deployment env (helm cloud values)."
echo "  3) Give the token to authorized operators; they paste it into the"
echo "     'API-токен' field in the UI header."

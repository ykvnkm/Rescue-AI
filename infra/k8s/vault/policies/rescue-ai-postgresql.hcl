# Vault policy for the offline local PostgreSQL pod.
#
# The pod receives only its own bootstrap password from KV v2 through
# Vault Agent. No Kubernetes Secret is used as a database credential
# source of truth.

path "secret/data/rescue-ai/postgresql" {
  capabilities = ["read"]
}

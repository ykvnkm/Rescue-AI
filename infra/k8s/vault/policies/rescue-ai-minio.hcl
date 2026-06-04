# Vault policy for the offline local MinIO pod and bucket-init job.
#
# The pod receives root credentials from KV v2 through Vault Agent.
# No Kubernetes Secret is used as an object-storage credential source
# of truth.

path "secret/data/rescue-ai/minio" {
  capabilities = ["read"]
}

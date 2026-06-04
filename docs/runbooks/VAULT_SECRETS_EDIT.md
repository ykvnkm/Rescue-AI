export KUBECONFIG=~/.kube/rescue-k3s.yaml
# 1. Туннель к Vault + root-токен
kubectl -n vault port-forward svc/rescue-ai-vault 8200:8200 &
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=$(python3 -c 'import json;print(json.load(open("scripts/security/out/vault-init-offline.json"))["root_token"])')

# 2. Посмотреть текущее (значения видны)
vault kv get secret/rescue-ai/api

# 3a. Поменять ОДИН ключ (merge, остальные не трогает)
vault kv patch secret/rescue-ai/api RPI_BASE_URL=https://<...>
# 3b. Заменить секрет целиком
#   vault kv put secret/rescue-ai/api DB_DSN=... ARTIFACTS_S3_ACCESS_KEY_ID=... ...

# 4. ОБЯЗАТЕЛЬНО перезапустить потребителя — Vault Agent рендерит файл только при старте пода
kubectl -n rescue-ai rollout restart deploy/rescue-ai-rescue-ai-api
kubectl -n rescue-ai rollout status   deploy/rescue-ai-rescue-ai-api

# 5. Убедиться, что новое значение доехало в под
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -c api -- sh -c 'grep RPI_BASE_URL /vault/secrets/app.env'
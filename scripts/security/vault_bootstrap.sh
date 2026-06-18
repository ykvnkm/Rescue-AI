#!/usr/bin/env bash
# Vault bootstrap для Rescue-AI (ADR-0008 §3).
#
# Запускается ОДНОКРАТНО после `helm install vault ...`, когда Vault
# уже инициализирован и распечатан (см. docs/runbooks/vault_setup.md
# раздел «Init + Unseal»).
#
# Что делает скрипт:
#   1. Включает KV v2 на пути `secret/`.
#   2. Включает Kubernetes auth method.
#   3. Загружает per-role политики (postgresql / minio / api /
#      sync-worker и опционально batch-exporter / airflow для cloud).
#   4. Создаёт role'и, привязанные к ServiceAccount-ам подов 1:1
#      с политикой.
#   5. Пишет в KV v2 реальные значения из переменных окружения.
#
# Запуск (offline профиль на k3s):
#   VAULT_ADDR=http://127.0.0.1:8200 \
#   VAULT_TOKEN=<unseal-root-token> \
#   NAMESPACE=rescue-ai \
#   ENABLE_PG_BACKUP=true \
#   POSTGRES_PASSWORD=… \
#   MINIO_ROOT_USER=… \
#   MINIO_ROOT_PASSWORD=… \
#   ENABLE_RPI_MTLS=true \
#   MTLS_CA_CERT_FILE=scripts/security/out/station-root-ca.crt \
#   MTLS_CLIENT_CERT_FILE=scripts/security/out/gcs-client.crt \
#   MTLS_CLIENT_KEY_FILE=scripts/security/out/gcs-client.key \
#   RPI_BASE_URL=https://<rpi-lan-ip>:9100 \   # адрес дрона ЭТОЙ станции
#   RPI_MISSIONS_DIR=/home/<user>/missions \  # корень миссий на дроне
#   DB_DSN=postgresql://… \
#   ./scripts/security/vault_bootstrap.sh
#
# Запуск (cloud, отдельный Vault-релиз с включённым Airflow):
#   …та же команда, плюс
#   ENABLE_BATCH_EXPORTER=true \
#   ENABLE_AIRFLOW=true \
#   AIRFLOW_DB_CONN='postgresql://…' \
#   AIRFLOW_S3_CONN='{"conn_type":"aws", …}' \
#   ./scripts/security/vault_bootstrap.sh

set -euo pipefail

: "${VAULT_ADDR:=http://127.0.0.1:8200}"
: "${VAULT_TOKEN:?set VAULT_TOKEN (root token из распечатки vault operator init)}"
: "${NAMESPACE:=rescue-ai}"               # namespace ПОДОВ ПРИЛОЖЕНИЯ (online)
: "${BATCH_NAMESPACE:=rescue-batch}"      # namespace batch-контура (Airflow,
                                          # batch-exporter, ephemeral batch-worker)
: "${VAULT_NAMESPACE:=$NAMESPACE}"        # namespace, где живёт сам Vault
                                          # (offline: rescue-ai, cloud: vault)
: "${VAULT_SERVICE_ACCOUNT:=rescue-ai-vault}"
: "${ENABLE_LOCAL_INFRA:=true}"
: "${POSTGRES_USER:=rescue}"
: "${POSTGRES_DB:=rescue_ai}"
: "${DB_DSN:?set DB_DSN}"
: "${ENABLE_BATCH_EXPORTER:=false}"
: "${ENABLE_AIRFLOW:=false}"
: "${ENABLE_RPI_MTLS:=false}"
# Адрес бортового компьютера БПЛА (RPi) — параметр КОНКРЕТНОЙ станции, а
# не миссии: дрон у станции один, его адрес в LAN стабилен. Задаётся ОДИН
# раз на бутстрапе станции и кладётся в Vault-секрет api (а не в git и не в
# открытый configmap), откуда Vault Agent источает его в окружение пода и
# перетирает пустой дефолт из values. Пусто → api стартует без RPi-линка
# (загрузка файлов/ZIP/S3 работает; /ready не зависит от RPi). RPI_RTSP_PORT
# опционален — при пустом используется дефолт чарта. RPI_MISSIONS_DIR —
# корень папок миссий НА самом дроне (RpiClient строит {dir}/{mission_id});
# путь зависит от пользователя дрона, поэтому тоже параметр станции.
: "${RPI_BASE_URL:=}"
: "${RPI_RTSP_PORT:=}"
: "${RPI_MISSIONS_DIR:=}"
: "${ENABLE_PG_BACKUP:=false}"
: "${ENABLE_GRAFANA:=false}"
: "${ENABLE_ALERTMANAGER:=false}"

if [ "$ENABLE_LOCAL_INFRA" = "true" ]; then
    : "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD when ENABLE_LOCAL_INFRA=true}"
    : "${MINIO_ROOT_USER:?set MINIO_ROOT_USER when ENABLE_LOCAL_INFRA=true}"
    : "${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD when ENABLE_LOCAL_INFRA=true}"
    : "${ARTIFACTS_S3_ACCESS_KEY_ID:=$MINIO_ROOT_USER}"
    : "${ARTIFACTS_S3_SECRET_ACCESS_KEY:=$MINIO_ROOT_PASSWORD}"
    # Sync-worker реплицирует артефакты из локального MinIO в этот же
    # bucket удалённого S3 (Yandex Object Storage), что и cloud-api
    # пишет напрямую. Bucket / prefix должны совпадать в обоих
    # профилях, иначе batch-сервис не увидит станционные миссии.
    # Дефолт жёстко прибит к каноническому имени из cloud.yaml; если
    # bucket другой, оператор обязан передать его явно.
    : "${DEPLOYMENT_REMOTE_S3_BUCKET:=rescue-ai-mission-artifacts}"
    : "${DEPLOYMENT_REMOTE_S3_ENDPOINT:=https://storage.yandexcloud.net}"
    : "${DEPLOYMENT_REMOTE_S3_REGION:=ru-central1}"
else
    : "${ARTIFACTS_S3_ACCESS_KEY_ID:?set ARTIFACTS_S3_ACCESS_KEY_ID}"
    : "${ARTIFACTS_S3_SECRET_ACCESS_KEY:?set ARTIFACTS_S3_SECRET_ACCESS_KEY}"
fi

if [ "$ENABLE_RPI_MTLS" = "true" ]; then
    : "${MTLS_CA_CERT_FILE:?set MTLS_CA_CERT_FILE when ENABLE_RPI_MTLS=true}"
    : "${MTLS_CLIENT_CERT_FILE:?set MTLS_CLIENT_CERT_FILE when ENABLE_RPI_MTLS=true}"
    : "${MTLS_CLIENT_KEY_FILE:?set MTLS_CLIENT_KEY_FILE when ENABLE_RPI_MTLS=true}"
fi

POLICY_DIR="$(cd "$(dirname "$0")/../../infra/k8s/vault/policies" && pwd)"

export VAULT_ADDR VAULT_TOKEN

echo "==> Vault status"
vault status >/dev/null

# ── 1. Включаем KV v2 ────────────────────────────────────────────
echo "==> Enable KV v2 at secret/"
vault secrets enable -path=secret -version=2 kv 2>/dev/null \
    || echo "    (already enabled)"

# ── 2. Kubernetes auth ───────────────────────────────────────────
echo "==> Enable Kubernetes auth"
vault auth enable kubernetes 2>/dev/null || echo "    (already enabled)"

KUBE_HOST="https://kubernetes.default.svc"
# token_reviewer_jwt — токен SA, под которым Vault обращается к
# kube-apiserver для проверки JWT'ов входящих подов. SA создаётся
# самим Vault Helm-чартом в namespace Vault'а (в offline это
# rescue-ai, в cloud — vault).
TOKEN_REVIEWER_JWT="$(kubectl -n "$VAULT_NAMESPACE" create token "$VAULT_SERVICE_ACCOUNT" \
    --duration=8760h 2>/dev/null || \
    kubectl -n "$VAULT_NAMESPACE" get secret \
        "$(kubectl -n "$VAULT_NAMESPACE" get sa "$VAULT_SERVICE_ACCOUNT" \
            -o jsonpath='{.secrets[0].name}')" \
        -o jsonpath='{.data.token}' | base64 -d)"
CA_CERT="$(kubectl config view --raw --minify --flatten \
    -o jsonpath='{.clusters[].cluster.certificate-authority-data}' | base64 -d)"

vault write auth/kubernetes/config \
    token_reviewer_jwt="$TOKEN_REVIEWER_JWT" \
    kubernetes_host="$KUBE_HOST" \
    kubernetes_ca_cert="$CA_CERT"

# ── 3. Per-role политики ─────────────────────────────────────────
echo "==> Load per-service policies"
if [ "$ENABLE_LOCAL_INFRA" = "true" ]; then
    vault policy write rescue-ai-postgresql "$POLICY_DIR/rescue-ai-postgresql.hcl"
    vault policy write rescue-ai-minio      "$POLICY_DIR/rescue-ai-minio.hcl"
fi
vault policy write rescue-ai-api            "$POLICY_DIR/rescue-ai-api.hcl"
vault policy write rescue-ai-sync-worker    "$POLICY_DIR/rescue-ai-sync-worker.hcl"
if [ "$ENABLE_BATCH_EXPORTER" = "true" ]; then
    vault policy write rescue-ai-batch-exporter "$POLICY_DIR/rescue-ai-batch-exporter.hcl"
fi
if [ "$ENABLE_PG_BACKUP" = "true" ]; then
    vault policy write rescue-ai-pg-backup "$POLICY_DIR/rescue-ai-pg-backup.hcl"
fi
if [ "$ENABLE_GRAFANA" = "true" ]; then
    vault policy write rescue-ai-grafana "$POLICY_DIR/rescue-ai-grafana.hcl"
fi
if [ "$ENABLE_ALERTMANAGER" = "true" ]; then
    vault policy write rescue-ai-alertmanager "$POLICY_DIR/rescue-ai-alertmanager.hcl"
fi

# ── 4. Привязка role 1:1 политике + ServiceAccount-у ─────────────
echo "==> Bind roles to ServiceAccounts (1 role = 1 policy = 1 SA)"
if [ "$ENABLE_LOCAL_INFRA" = "true" ]; then
    vault write auth/kubernetes/role/rescue-ai-postgresql \
        bound_service_account_names=rescue-ai-postgresql \
        bound_service_account_namespaces="$NAMESPACE" \
        policies=rescue-ai-postgresql \
        ttl=24h

    vault write auth/kubernetes/role/rescue-ai-minio \
        bound_service_account_names=rescue-ai-minio \
        bound_service_account_namespaces="$NAMESPACE" \
        policies=rescue-ai-minio \
        ttl=24h
fi

vault write auth/kubernetes/role/rescue-ai-api \
    bound_service_account_names=rescue-ai-rescue-ai-api \
    bound_service_account_namespaces="$NAMESPACE" \
    policies=rescue-ai-api \
    ttl=24h

vault write auth/kubernetes/role/rescue-ai-sync-worker \
    bound_service_account_names=rescue-ai-rescue-ai-sync-worker \
    bound_service_account_namespaces="$NAMESPACE" \
    policies=rescue-ai-sync-worker \
    ttl=24h

if [ "$ENABLE_BATCH_EXPORTER" = "true" ]; then
    # batch-exporter живёт в namespace rescue-batch (см. ADR-0008 §4).
    # SA-имя формируется helm-чартом umbrella'а rescue-batch как
    # <release>-rescue-ai-batch-exporter; релиз называется rescue-batch.
    vault write auth/kubernetes/role/rescue-ai-batch-exporter \
        bound_service_account_names=rescue-batch-rescue-ai-batch-exporter \
        bound_service_account_namespaces="$BATCH_NAMESPACE" \
        policies=rescue-ai-batch-exporter \
        ttl=24h
fi

if [ "$ENABLE_PG_BACKUP" = "true" ]; then
    vault write auth/kubernetes/role/rescue-ai-pg-backup \
        bound_service_account_names=rescue-ai-pg-backup \
        bound_service_account_namespaces="$NAMESPACE" \
        policies=rescue-ai-pg-backup \
        ttl=1h
fi

if [ "$ENABLE_GRAFANA" = "true" ]; then
    : "${GRAFANA_ADMIN_USER:?set GRAFANA_ADMIN_USER when ENABLE_GRAFANA=true}"
    : "${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD when ENABLE_GRAFANA=true}"
    : "${MONITORING_NAMESPACE:=monitoring}"

    vault write auth/kubernetes/role/rescue-ai-grafana \
        bound_service_account_names=rescue-ai-observability-grafana \
        bound_service_account_namespaces="$MONITORING_NAMESPACE" \
        policies=rescue-ai-grafana \
        ttl=24h

    vault kv put secret/rescue-ai/grafana \
        admin_user="$GRAFANA_ADMIN_USER" \
        admin_password="$GRAFANA_ADMIN_PASSWORD"
fi

if [ "$ENABLE_ALERTMANAGER" = "true" ]; then
    : "${ALERT_SMTP_FROM:?set ALERT_SMTP_FROM when ENABLE_ALERTMANAGER=true}"
    : "${ALERT_SMTP_SMARTHOST:?set ALERT_SMTP_SMARTHOST when ENABLE_ALERTMANAGER=true (host:port)}"
    : "${ALERT_SMTP_USERNAME:?set ALERT_SMTP_USERNAME when ENABLE_ALERTMANAGER=true}"
    : "${ALERT_SMTP_PASSWORD:?set ALERT_SMTP_PASSWORD when ENABLE_ALERTMANAGER=true}"
    : "${ALERT_WARNING_RECIPIENT:?set ALERT_WARNING_RECIPIENT when ENABLE_ALERTMANAGER=true}"
    : "${ALERT_CRITICAL_RECIPIENT:?set ALERT_CRITICAL_RECIPIENT when ENABLE_ALERTMANAGER=true}"
    : "${MONITORING_NAMESPACE:=monitoring}"

    vault write auth/kubernetes/role/rescue-ai-alertmanager \
        bound_service_account_names=rescue-ai-observability-alertmanager \
        bound_service_account_namespaces="$MONITORING_NAMESPACE" \
        policies=rescue-ai-alertmanager \
        ttl=24h

    vault kv put secret/rescue-ai/alertmanager \
        smtp_from="$ALERT_SMTP_FROM" \
        smtp_smarthost="$ALERT_SMTP_SMARTHOST" \
        smtp_username="$ALERT_SMTP_USERNAME" \
        smtp_password="$ALERT_SMTP_PASSWORD" \
        warning_recipient="$ALERT_WARNING_RECIPIENT" \
        critical_recipient="$ALERT_CRITICAL_RECIPIENT"
fi

# ── 5. Прикладные секреты в KV v2 ────────────────────────────────
echo "==> Write application secrets (KV v2)"
if [ "$ENABLE_LOCAL_INFRA" = "true" ]; then
    vault kv put secret/rescue-ai/postgresql \
        POSTGRES_USER="$POSTGRES_USER" \
        POSTGRES_DB="$POSTGRES_DB" \
        POSTGRES_PASSWORD="$POSTGRES_PASSWORD"

    vault kv put secret/rescue-ai/minio \
        MINIO_ROOT_USER="$MINIO_ROOT_USER" \
        MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD"
fi

# Адрес RPi станции добавляется в секрет только если оператор его передал,
# чтобы не затирать дефолт чарта пустой строкой при air-gapped установке.
api_rpi_args=()
if [ -n "$RPI_BASE_URL" ]; then
    api_rpi_args+=( "RPI_BASE_URL=$RPI_BASE_URL" )
    [ -n "$RPI_RTSP_PORT" ] && api_rpi_args+=( "RPI_RTSP_PORT=$RPI_RTSP_PORT" )
    [ -n "$RPI_MISSIONS_DIR" ] && api_rpi_args+=( "RPI_MISSIONS_DIR=$RPI_MISSIONS_DIR" )
fi
# DEPLOYMENT_REMOTE_S3_BUCKET нужен И api: в offline-профиле api оборачивает
# artifact_storage в outbox-репликацию только когда знает ИМЯ удалённого
# бакета (пишет его в строку outbox как назначение копии). Сами remote-креды
# api не нужны — копию local→remote делает sync-worker. Бакет обязан совпадать
# с тем, что в секрете sync-worker, иначе кадры уедут не туда.
vault kv put secret/rescue-ai/api \
    DB_DSN="$DB_DSN" \
    ARTIFACTS_S3_ACCESS_KEY_ID="$ARTIFACTS_S3_ACCESS_KEY_ID" \
    ARTIFACTS_S3_SECRET_ACCESS_KEY="$ARTIFACTS_S3_SECRET_ACCESS_KEY" \
    DEPLOYMENT_REMOTE_S3_BUCKET="${DEPLOYMENT_REMOTE_S3_BUCKET:-}" \
    ${api_rpi_args[@]+"${api_rpi_args[@]}"}

if [ "$ENABLE_RPI_MTLS" = "true" ]; then
    vault kv put secret/rescue-ai/rpi-mtls \
        ca_crt="$(cat "$MTLS_CA_CERT_FILE")" \
        client_crt="$(cat "$MTLS_CLIENT_CERT_FILE")" \
        client_key="$(cat "$MTLS_CLIENT_KEY_FILE")"
fi

vault kv put secret/rescue-ai/sync-worker \
    DB_DSN="$DB_DSN" \
    DEPLOYMENT_REMOTE_DB_DSN="${DEPLOYMENT_REMOTE_DB_DSN:-}" \
    DEPLOYMENT_REMOTE_S3_ENDPOINT="${DEPLOYMENT_REMOTE_S3_ENDPOINT:-https://storage.yandexcloud.net}" \
    DEPLOYMENT_REMOTE_S3_REGION="${DEPLOYMENT_REMOTE_S3_REGION:-ru-central1}" \
    DEPLOYMENT_REMOTE_S3_BUCKET="${DEPLOYMENT_REMOTE_S3_BUCKET:-}" \
    DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID="${DEPLOYMENT_REMOTE_S3_ACCESS_KEY_ID:-}" \
    DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY="${DEPLOYMENT_REMOTE_S3_SECRET_ACCESS_KEY:-}" \
    ARTIFACTS_S3_ENDPOINT="${ARTIFACTS_S3_ENDPOINT:-http://rescue-ai-minio.rescue-ai.svc.cluster.local:9000}" \
    ARTIFACTS_S3_REGION="${ARTIFACTS_S3_REGION:-ru-central1}" \
    ARTIFACTS_S3_BUCKET="${ARTIFACTS_S3_BUCKET:-rescue-artifacts}" \
    ARTIFACTS_S3_ACCESS_KEY_ID="${ARTIFACTS_S3_ACCESS_KEY_ID:-$MINIO_ROOT_USER}" \
    ARTIFACTS_S3_SECRET_ACCESS_KEY="${ARTIFACTS_S3_SECRET_ACCESS_KEY:-$MINIO_ROOT_PASSWORD}"

if [ "$ENABLE_BATCH_EXPORTER" = "true" ]; then
    vault kv put secret/rescue-ai/batch-exporter \
        DB_DSN="$DB_DSN"
fi

# ── 6. Опционально: Airflow Secrets Backend ──────────────────────
#
# В cloud-профиле Airflow читает connection-объекты из Vault
# KV-путей `secret/data/airflow/connections/<conn_id>`. Скрипт
# выставляет два дефолтных коннекшена: rescue_app_db и rescue_s3.
if [ "$ENABLE_AIRFLOW" = "true" ]; then
    : "${AIRFLOW_DB_CONN:?set AIRFLOW_DB_CONN when ENABLE_AIRFLOW=true}"
    : "${AIRFLOW_S3_CONN:?set AIRFLOW_S3_CONN when ENABLE_AIRFLOW=true}"

    echo "==> Load Airflow policy + bind role to its ServiceAccounts"
    vault policy write rescue-ai-airflow "$POLICY_DIR/rescue-ai-airflow.hcl"

    # Airflow живёт в namespace rescue-batch (см. ADR-0008 §4).
    # apache-airflow Helm chart создаёт SA <release>-webserver /
    # -scheduler / -worker / -triggerer; релиз называется rescue-batch.
    vault write auth/kubernetes/role/rescue-ai-airflow \
        bound_service_account_names=rescue-batch-airflow-webserver,rescue-batch-airflow-scheduler,rescue-batch-airflow-worker,rescue-batch-airflow-triggerer \
        bound_service_account_namespaces="$BATCH_NAMESPACE" \
        policies=rescue-ai-airflow \
        ttl=24h

    echo "==> Write Airflow connections to KV v2"
    vault kv put secret/airflow/connections/rescue_app_db \
        conn_uri="$AIRFLOW_DB_CONN"
    vault kv put secret/airflow/connections/rescue_s3 \
        conn_uri="$AIRFLOW_S3_CONN"
fi

echo
echo "Vault bootstrap done."
echo "Policies: $( [ "$ENABLE_LOCAL_INFRA" = "true" ] && echo 'rescue-ai-postgresql, rescue-ai-minio, ' )rescue-ai-api, rescue-ai-sync-worker$( [ "$ENABLE_BATCH_EXPORTER" = "true" ] && echo ', rescue-ai-batch-exporter' )$( [ "$ENABLE_AIRFLOW" = "true" ] && echo ', rescue-ai-airflow' )"
echo "Roles:    same names, bound 1:1 to ServiceAccount-ам в namespace $NAMESPACE."
echo "Secrets:  secret/rescue-ai/{$( [ "$ENABLE_LOCAL_INFRA" = "true" ] && echo 'postgresql,minio,' )api,$( [ "$ENABLE_RPI_MTLS" = "true" ] && echo 'rpi-mtls,' )sync-worker$( [ "$ENABLE_BATCH_EXPORTER" = "true" ] && echo ',batch-exporter' )}$( [ "$ENABLE_AIRFLOW" = "true" ] && echo ', secret/airflow/connections/{rescue_app_db,rescue_s3}' )."

# Vault policy для Apache Airflow (ADR-0008 §3, cloud-only).
#
# Airflow используется как Vault Secrets Backend: connection-объекты
# (rescue_app_db, rescue_s3) и variables читаются прямо из KV v2 на
# путях `secret/data/airflow/connections/*` и
# `secret/data/airflow/variables/*`. Это убирает необходимость в
# k8s-Secret'е `rescue-airflow-conns` со встроенным `secretKeyRef`.
#
# Привязывается к ServiceAccount-у `airflow-webserver`,
# `airflow-scheduler` (и `airflow-worker` при KubernetesExecutor — но
# самим тасковым подам секреты не нужны, наш batch-worker читает их
# из переменных окружения, передаваемых KubernetesPodOperator-ом).

path "secret/data/airflow/connections/*" {
  capabilities = ["read"]
}

path "secret/data/airflow/variables/*" {
  capabilities = ["read"]
}

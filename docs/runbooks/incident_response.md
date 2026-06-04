# Incident response — пошаговая первая помощь по сценариям

Этот runbook — карманная книжка оператора на случай отказа. Она НЕ
заменяет проектирование отказоустойчивости из §4 ML design-doc; она
говорит, **что делать руками**, когда автоматический механизм
сработал, но миссия ещё в работе и оператору надо как-то реагировать.

Каждая секция — один сценарий:

1. **Симптом** — что увидит оператор (UI, метрики, kubectl).
2. **Что уже сделал k8s сам** — какой автоматический механизм
   отработал.
3. **Команды первой помощи** — конкретные `kubectl`/`helm`/`vault`,
   которые оператор может выполнить.
4. **Когда эскалировать** — критерий перехода к разбору инцидента.

Все команды даны для namespace `rescue-ai` (offline / cloud single
namespace). В cloud Vault живёт в namespace `vault` — отдельно
отмечено в Vault-секции.

---

## 1. Pod падает в `CrashLoopBackOff`

**Симптом.** `kubectl -n rescue-ai get pods` показывает RESTART > 5
у одного из подов, status = `CrashLoopBackOff`.

**Что уже сделал k8s.** Deployment-restartPolicy=Always: каждое
падение приводит к рестарту, но backoff растёт (10s → 20s → 40s →
80s → … → 5min). Pod в endpoints Service'а не появляется (readiness
не проходит).

**Первая помощь:**
```bash
kubectl -n rescue-ai describe pod <pod-name>      # причина из Events
kubectl -n rescue-ai logs <pod-name> --previous   # лог упавшей попытки
kubectl -n rescue-ai logs <pod-name>              # лог текущей попытки

# Самые частые причины:
#   - Vault sealed → секреты не отрисовались → DB_DSN пустой
#       → см. секцию «Vault sealed»
#   - Init-контейнер wait-for-postgres timeout → Postgres лёг
#       → см. секцию «Postgres недоступен»
#   - OOMKilled (Exit 137) → не хватило memory.limit
#       → см. секцию «OOMKilled»
```

**Эскалация:** > 30 минут CrashLoopBackOff после исправления видимой
причины — собирай `kubectl describe` + `logs --previous` + текущий
тег образа и иди в репозиторий.

---

## 2. Vault sealed

**Симптом.** `vault status` показывает `Sealed: true`. Прикладные
поды зависают в `Init:0/2` (вверху Vault Agent init с ошибкой
`Vault is sealed`).

**Что уже сделал k8s.** Никак не исправил: unseal требует human
unseal keys, у k8s их нет (это by design — оператор имеет
физический контроль).

**Первая помощь (одинаково для offline и cloud).** Vault в обоих
профилях живёт в namespace `vault` отдельным helm release-ом
`rescue-ai-vault`:
```bash
# Достать 3 из 5 unseal keys из 1Password / сейфа.
for key in <key-1> <key-2> <key-3>; do
    kubectl -n vault exec rescue-ai-vault-0 -- vault operator unseal $key
done

# Через минуту прикладные поды сами стартанут.
kubectl -n rescue-ai get pods -w
```

**Эскалация:** утрачены 3 из 5 unseal keys — это **катастрофа**, без
них Vault невосстановим. Восстановление через snapshot tar.gz из
`PVC vault-data` + новый unseal-цикл.

---

## 3. Postgres недоступен

**Симптом.** Pod-ы api/sync-worker/batch-exporter в Init с зависшим
`wait-for-postgres`. В логах: `pg_isready: connection refused`.

**Что уже сделал k8s.** Init-контейнер ждёт до 120 секунд (api) /
180 секунд (sync-worker), затем падает с timeout. Pod уйдёт в
CrashLoopBackOff и попробует снова через 10–80 секунд.

**Первая помощь (offline — наш localPostgresql):**
```bash
kubectl -n rescue-ai get pod -l app.kubernetes.io/name=postgresql
kubectl -n rescue-ai describe pod rescue-ai-postgresql-0
kubectl -n rescue-ai logs rescue-ai-postgresql-0

# Частые причины:
#   - PVC не примонтировался (`Pending`) → проверить StorageClass:
kubectl get pvc -n rescue-ai
kubectl get sc

#   - Vault Agent не вернул POSTGRES_PASSWORD → проверить role:
kubectl -n rescue-ai exec rescue-ai-postgresql-0 -c vault-agent -- \
    cat /vault/secrets/postgres-password

#   - Диск переполнен (`No space left on device` в логах) → почистить:
kubectl -n rescue-ai exec rescue-ai-postgresql-0 -- df -h /var/lib/postgresql
```

**Первая помощь (cloud — managed Postgres вне кластера):**
```bash
# Управляемая Postgres → consoleProvider (Yandex Cloud Console).
# Из k8s можно проверить только что сами поды api корректно
# резолвят DSN и видят сеть до managed-host:
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -- \
    nc -zv rescue-app-pgsql.mdb.yandexcloud.net 6432
```

**Эскалация:** Postgres pod `Running` но `pg_isready` фейлится
> 5 минут — собирай `pg_stat_activity` (через `psql` exec) для
анализа locking.

---

## 4. OOMKilled

**Симптом.** `kubectl get pods` показывает `Last State: Terminated`
с `Reason: OOMKilled`, exit code 137. Обычно после раунда
CrashLoopBackOff.

**Что уже сделал k8s.** Pod был убит kernel'ом cgroups за выход за
`resources.limits.memory`. Deployment пересоздал. Если limit
слишком жёсткий, цикл повторяется.

**Первая помощь:**
```bash
# Какой именно лимит был у пода:
kubectl -n rescue-ai describe pod <pod-name> | grep -A2 Limits

# Что подросло — текущая память (если ещё жив):
kubectl -n rescue-ai top pod <pod-name>

# Временно поднять лимит на 50 %:
helm upgrade rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai \
    -f infra/k8s/values/offline.yaml \
    --set rescue-ai-detection.resources.limits.memory=3Gi
```

**Эскалация:** OOM повторяется на новом лимите — есть memory leak,
смотреть `py-spy dump` и trends в Grafana (`container_memory_rss`).

---

## 5. Raspberry Pi (RPi) недоступен

**Симптом.** UI оператора показывает «соединение с RPi потеряно». В
логах api: `httpx.ConnectError: <rpi-host>:9100`.

**Что уже сделал k8s.** Ничего напрямую — RPi не часть кластера.
NetworkPolicy api разрешает egress на 9100 и RTSP-порт, но если RPi
сам выключен — поможет только проверка на месте.

**Первая помощь:**
```bash
# Ping из api пода:
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -- \
    curl -fsS --cacert /vault/secrets/station-root-ca.crt \
    --cert /vault/secrets/gcs-client.crt \
    --key  /vault/secrets/gcs-client.key \
    https://<rpi-host>:9100/health

# mTLS-сертификаты протухли? Проверь срок действия:
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -- \
    openssl x509 -in /vault/secrets/gcs-client.crt -noout -dates
```

**Эскалация:** > 60 секунд `LINK_LOSS` — миссия сама перейдёт в
`LINK_LOSS` state (§4.3.2). При сценарии `EMERGENCY_TERMINATE`
RPi уже не вернётся; смотри журнал миссии.

---

## 6. Sync-worker не дренирует outbox

**Симптом.** `select count(*) from replication_outbox where
status='pending'` растёт без признаков снижения. В логах
sync-worker: `httpx.ConnectError: storage.yandexcloud.net`.

**Что уже сделал k8s.** Worker restartPolicy=Always — но он не
крашится, просто ретраит. NetworkPolicy позволяет egress на 443
(remote S3) и 5432/6432 (remote Postgres).

**Первая помощь:**
```bash
kubectl -n rescue-ai logs deploy/rescue-ai-rescue-ai-sync-worker --tail=100

# Связь с remote есть?
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-sync-worker -- \
    nc -zv storage.yandexcloud.net 443

# Long-running batch застрял? Force-restart:
kubectl -n rescue-ai rollout restart deploy/rescue-ai-rescue-ai-sync-worker
```

**Эскалация:** outbox > 10 000 записей > 6 часов — связь надолго,
доставай станцию вручную. Никаких данных при этом не теряется (PK
по `(mission_id, …)` гарантирует upsert).

---

## 7. Detection HPA взлетел до maxReplicas (cloud)

**Симптом.** `kubectl get hpa -n rescue-ai` показывает
`REPLICAS: 6/6` (maxReplicas) с CPU >> 70 %. Возможны 504 от api
при вызове detection.

**Что уже сделал k8s.** HPA ровно так и работает: при росте CPU
добавил реплики до лимита. Дальше — потолок нагрузки.

**Первая помощь:**
```bash
# Кто заваливает запросами? Чаще всего — несколько одновременных миссий:
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -- \
    curl -fsS localhost:8000/metrics | grep ^rescue_ai_alerts_created_total

# Поднять потолок до 10 (требует ресурсов в кластере):
helm upgrade rescue-ai infra/k8s/charts/rescue-ai \
    -n rescue-ai -f infra/k8s/values/cloud.yaml \
    --set rescue-ai-detection.autoscaling.maxReplicas=10
```

**Эскалация:** даже 10 реплик не справляются — узкое место не CPU,
а I/O (например, S3 для download кадров) — нужна оптимизация
прикладного кода, не масштабирование.

---

## 8. PodDisruptionBudget блокирует drain

**Симптом.** `kubectl drain node-X` зависает с «cannot evict pod:
no available pods to disrupt». Часто при upgrade k3s/managed K8s.

**Что уже сделал k8s.** PDB сделал свою работу: запретил выселение,
которое нарушило бы доступность. В offline PDB.maxUnavailable=0 →
любой drain тут блокируется специально.

**Первая помощь (offline):**
```bash
# Ручное снятие сервиса перед drain (потеря миссии на N секунд):
kubectl -n rescue-ai scale deploy/rescue-ai-rescue-ai-api --replicas=0
kubectl drain <node>
# … апгрейд / обслуживание …
kubectl uncordon <node>
kubectl -n rescue-ai scale deploy/rescue-ai-rescue-ai-api --replicas=1
```

**Первая помощь (cloud):** drain должен работать сам — replicaCount=2
+ PDB.maxUnavailable=1 позволяет уронить 1 из 2 реплик. Если не
работает: пробежись по `kubectl get pdb -n rescue-ai` и проверь,
правильно ли selector совпадает с подами.

---

## 9. CronJob backup'а Postgres не запускается

**Симптом.** `kubectl get cronjob -n rescue-ai` показывает
`LAST SCHEDULE: <none>` или `LAST SCHEDULE` > 25 часов назад.

**Что уже сделал k8s.** Зависит от причины: если `successfulJobsHistoryLimit`
исчерпан с failed-only — Jobs накапливаются, новые не запускаются.

**Первая помощь:**
```bash
kubectl -n rescue-ai get jobs
kubectl -n rescue-ai logs job/rescue-ai-rescue-ai-postgresql-backup-<timestamp>
kubectl -n rescue-ai logs job/<...> -c pg-dump      # initContainer
kubectl -n rescue-ai logs job/<...> -c mc-upload    # main container

# Ручной запуск backup'а сейчас (для проверки):
kubectl -n rescue-ai create job manual-backup \
    --from=cronjob/rescue-ai-rescue-ai-postgresql-backup
```

**Эскалация:** backup не работает > 2 дней — проверь MinIO bucket
вручную (`mc ls local/rescue-artifacts/backups/postgres/`). Если
последний дамп старше суток, поднимай alert и проводи manual
`pg_dump` локально, чтобы был свежий бэкап.

---

## 10. Network partition внутри namespace

**Симптом.** api логирует `httpx.ConnectError` к detection /
nav-engine, при этом сами поды Running. Запрос curl-ом из api
пода к ClusterIP сервиса фейлится.

**Что уже сделал k8s.** retry-обёртка (`request_with_retry` +
`HTTPTransport(retries=2)`) сделала несколько попыток. NetworkPolicy
не должна была заблокировать internal Service.

**Первая помощь:**
```bash
# Что говорит DNS?
kubectl -n rescue-ai exec deploy/rescue-ai-rescue-ai-api -- \
    nslookup rescue-ai-rescue-ai-detection.rescue-ai.svc.cluster.local

# NetworkPolicy конфликтует?
kubectl -n rescue-ai get networkpolicy
kubectl -n rescue-ai describe networkpolicy rescue-ai-rescue-ai-detection

# Сам Service резолвит endpoints?
kubectl -n rescue-ai get svc rescue-ai-rescue-ai-detection
kubectl -n rescue-ai get endpoints rescue-ai-rescue-ai-detection
```

**Эскалация:** endpoints пусты → readiness detection-пода не прошёл,
смотри секцию 1.

---

## 11. Airflow scheduler не видит DAG-и (cloud)

**Симптом.** Airflow UI пустой, DAG `rescue_batch_pipeline` не
появляется. CronJob не триггерится.

**Что уже сделал k8s.** git-sync sidecar в Airflow scheduler-поде
тянет репу. Если SSH-доступ к GitHub отвалился — sidecar логирует
ошибку.

**Первая помощь:**
```bash
kubectl -n rescue-ai logs deploy/rescue-ai-airflow-scheduler -c git-sync
kubectl -n rescue-ai logs deploy/rescue-ai-airflow-scheduler -c scheduler

# Vault Secrets Backend жив?
kubectl -n rescue-ai exec deploy/rescue-ai-airflow-scheduler -- \
    python -c "from airflow.hooks.base import BaseHook; \
               print(BaseHook.get_connection('rescue_app_db'))"
```

**Эскалация:** Vault Secrets Backend возвращает None → проверь
policy и role rescue-ai-airflow:
```bash
kubectl -n vault exec rescue-ai-vault-0 -- \
    vault read auth/kubernetes/role/rescue-ai-airflow
```

---

## Связанные документы

- [§4 ML system design doc](../ml_system_design_doc.md) — теория FT
- [Vault setup runbook](vault_setup.md) — init/unseal/bootstrap
- [k3s field deploy](k3s_field_deploy.md) — установка станции
- [RPi mTLS setup](rpi_mtls_setup.md) — сертификаты

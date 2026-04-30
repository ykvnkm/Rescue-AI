# Runbook: настройка mTLS на Raspberry Pi

**Контекст:** ADR-0007 §4 — защищённый канал между наземной станцией
(Rescue-AI) и Raspberry Pi на борту БПЛА. Без публичных туннелей,
без облачных сервисов, без белых IP — самоподписанный CA + взаимная
TLS-аутентификация.

Этот runbook описывает работу **владельца Pi**. Клиентскую сторону
(станция) уже покрывает P2: см.
[`rescue_ai/infrastructure/rpi_client.py`](../../rescue_ai/infrastructure/rpi_client.py)
и [`scripts/security/`](../../scripts/security/).

## Что нужно от человека на станции (предусловия)

На ноуте оператора уже один раз прогнаны три скрипта:

```bash
./scripts/security/gen_ca.sh
./scripts/security/gen_rpi_cert.sh
./scripts/security/gen_client_cert.sh
```

В `scripts/security/out/` лежит 6 файлов. На Pi нужно перенести
**только три**:

| Файл | Зачем на Pi |
|---|---|
| `station-root-ca.crt` | корневой CA — Pi им проверяет подлинность клиентского серта |
| `rpi-server.crt` | серверный сертификат самого Pi |
| `rpi-server.key` | приватный ключ Pi (никому не показывать) |

**На Pi НЕ передавать:** `station-root-ca.key`, `gcs-client.crt`,
`gcs-client.key`. Корневой ключ хранится только на ноуте оператора;
клиентский — только на станции.

## Шаг 1. Скопировать сертификаты на Pi

Со станции:

```bash
scp scripts/security/out/station-root-ca.crt pi@192.168.0.118:/tmp/
scp scripts/security/out/rpi-server.crt      pi@192.168.0.118:/tmp/
scp scripts/security/out/rpi-server.key      pi@192.168.0.118:/tmp/
```

## Шаг 2. Разложить и закрыть права

На Pi:

```bash
sudo mkdir -p /etc/rescue-ai
sudo mv /tmp/station-root-ca.crt /etc/rescue-ai/
sudo mv /tmp/rpi-server.crt      /etc/rescue-ai/
sudo mv /tmp/rpi-server.key      /etc/rescue-ai/
sudo chown -R root:root /etc/rescue-ai
sudo chmod 600 /etc/rescue-ai/rpi-server.key
sudo chmod 644 /etc/rescue-ai/rpi-server.crt /etc/rescue-ai/station-root-ca.crt
```

## Шаг 3. Перевести `rpi_source_service` на TLS listen

Сейчас сервис на Pi стартует в plain HTTP. Перевести uvicorn на mTLS —
это **четыре параметра** в запуске:

```python
# rpi_source_service: точка входа / main
uvicorn.run(
    app,
    host="0.0.0.0",
    port=9100,
    ssl_certfile="/etc/rescue-ai/rpi-server.crt",
    ssl_keyfile="/etc/rescue-ai/rpi-server.key",
    ssl_ca_certs="/etc/rescue-ai/station-root-ca.crt",
    ssl_cert_reqs=2,  # ssl.CERT_REQUIRED — отвергать клиентов без серта
)
```

Что делает каждый параметр:

- `ssl_certfile` + `ssl_keyfile` — Pi становится HTTPS-сервером
  (обычный TLS, *не* взаимный).
- `ssl_ca_certs` + `ssl_cert_reqs=CERT_REQUIRED` — Pi требует, чтобы
  клиент **тоже** предъявил сертификат, подписанный нашим
  `station-root-ca`. Любой клиент без серта (или с чужим CA)
  получит `alert handshake failure`. Это и делает связь *взаимной*.

Если запуск идёт через systemd-юнит — пути обычно прокидываются как
переменные окружения и читаются в коде. Содержательного кода менять
не нужно.

После правки — перезапустить сервис:

```bash
sudo systemctl restart rpi-source-service
sudo systemctl status  rpi-source-service
```

## Шаг 4. Sanity-проверка на самом Pi

```bash
openssl s_client -connect 127.0.0.1:9100 -servername rpi.local </dev/null
```

Что искать в выводе:

- `subject=CN = rpi.local`
- `issuer=CN = station-root-ca`
- `Verify return code: 0 (ok)` — если запустить с
  `-CAfile /etc/rescue-ai/station-root-ca.crt`.

## Шаг 5. Проверка со станции

С ноута или из контейнера API:

```bash
# Должно вернуть {"status":"ok"}
curl --cacert scripts/security/out/station-root-ca.crt \
     --cert   scripts/security/out/gcs-client.crt \
     --key    scripts/security/out/gcs-client.key \
     https://192.168.0.118:9100/health

# Без клиентского серта — должно отказать
curl --cacert scripts/security/out/station-root-ca.crt \
     https://192.168.0.118:9100/health
# → curl: (35) ... alert handshake failure
```

После этого `GET /rpi/status` в Rescue-AI API возвращает 200 вместо
`httpx.ConnectTimeout` (см. логи P2-проверки от 2026-04-27).

## Открыть порт на Pi (если firewall активен)

```bash
sudo ufw allow from 192.168.0.0/24 to any port 9100 proto tcp
```

## Ротация сертификатов

Сроки по умолчанию (см. `scripts/security/gen_*.sh`):

| Файл | Срок | Что делать при истечении |
|---|---|---|
| `station-root-ca.{crt,key}` | 10 лет | перевыпуск всей цепочки — отдельный мероприятие |
| `rpi-server.{crt,key}` | 1 год | перевыпустить, скопировать на Pi, перезапустить сервис |
| `gcs-client.{crt,key}` | 1 год | перевыпустить, обновить `TLS_CLIENT_*` пути в env |

Перевыпуск серверного серта на станции:

```bash
rm scripts/security/out/rpi-server.*
RPI_DAYS=365 ./scripts/security/gen_rpi_cert.sh
# затем повторить шаги 1-2 этого runbook'а и перезапустить сервис на Pi
```

## Troubleshooting

| Симптом со станции | Вероятная причина | Что проверить на Pi |
|---|---|---|
| `httpx.ConnectTimeout: handshake operation timed out` | Pi слушает HTTP, не HTTPS | `ss -tlnp \| grep 9100` — порт открыт? процесс — uvicorn? |
| `curl: (60) SSL certificate problem` со станции | На Pi другой `rpi-server.crt`, не подписанный нашим CA | `openssl x509 -in /etc/rescue-ai/rpi-server.crt -issuer -noout` |
| `alert handshake failure` даже с клиентским сертом | На Pi не указан `ssl_ca_certs` или серт клиента подписан другим CA | проверить uvicorn-параметры (шаг 3) |
| Connection refused | Сервис не запущен / упал на старте | `journalctl -u rpi-source-service -n 100` |

## Связанные документы

- [ADR-0007: Автономный деплой и offline sync](../adr/ADR-0007-autonomous-deployment-and-offline-sync.md)
- [Скрипты генерации сертификатов](../../scripts/security/)
- [Offline-стек станции](../../infra/offline/README.md)
- [RpiClient (клиентская сторона mTLS)](../../rescue_ai/infrastructure/rpi_client.py)

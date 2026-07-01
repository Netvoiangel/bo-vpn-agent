# BO/VPN Agent: ТЗ и статус реализации

Дата фиксации: 2026-07-01.

Этот документ фиксирует текущую сверку реализации `bo-vpn-agent` с исходным ТЗ. Сейчас рано заниматься полировкой результата `basic_status`; сначала важно держать ясную картину, что уже закрыто, что подтверждено стендом, а что остаётся частью будущего MVP.

## Общий статус

```text
ТЗ в целом: выполнено примерно 35-45%
MVP control plane: выполнено примерно 75-85%
Стендовый existing_container MVP: выполнено примерно 65-75%
Compose/container_namespace MVP: выполнено примерно 30-40%, experimental
Целевая job_container-модель: выполнено примерно 10-15%
```

Уже есть рабочая стендовая вертикаль:

```text
Dockerized worker
  -> host-side systemd runner
    -> existing_container
      -> nsenter в namespace univpn-service
        -> BO/ТС
```

Подтверждено на реальной ТС:

```text
6217 / 172.26.129.119
vehicle_reachability: OK
basic_status: OK
```

## 1. Назначение и границы ответственности

| Пункт ТЗ | Статус | Комментарий |
| --- | ---: | --- |
| Worker вынесен отдельно от Telegram-бота | Сделано | Worker реализован отдельным сервисом. |
| Бот остаётся UI-слоем | Частично | В коде worker это учтено, но интеграции с реальным ботом пока нет. |
| Worker отвечает за диагностику и структурированный результат | Сделано частично | `vehicle_reachability` и `basic_status` возвращают структурированный результат. |
| Не смешивать bot/VPN/SSH/browser logic | Сделано | Worker и runner разделены. Docker socket в worker не монтируется. |
| File-based inventory номер ТС -> IP | Сделано | Стендовое MVP-решение внутри worker-а; lookup по `vehicle_id` подтверждён на сервере. Позже заменить на внешний inventory service или bot-side resolver. |

Вывод: архитектурное разделение соблюдено.

## 2. Общая схема worker / runner / UniVPN

| Пункт ТЗ | Статус | Комментарий |
| --- | ---: | --- |
| `diagnostic worker` | Сделано | API, задачи, статусы, capabilities, auth. |
| `vpn runner` | Сделано частично | Есть runner-daemon, systemd service, existing_container execution. |
| `existing_container` режим | Подтверждено | Проверен на стенде через `univpn-service`. |
| `job_container` режим | Placeholder | Есть boundary/placeholder, реального запуска job-контейнера пока нет. |
| `container_namespace` режим | Сделано, experimental | Runner работает внутри shared namespace с `univpn-service`, без Docker и `nsenter`. Требует stand smoke-test. |
| Одноразовая VPN-сессия на задачу | Не сделано | В стендовом режиме используется уже поднятый `univpn-service`. |
| Managed VPN session | Сделано частично | Для `container_namespace` добавлен login через control path и preflight wait. Safe disconnect пока требует подтверждения UniVPN exit sequence. |
| Cleanup одноразового контейнера | Не сделано | Актуально для будущего `job_container`. |

Вывод: стендовый режим работает, целевая модель с одноразовым job-контейнером ещё впереди.

## 3. Проверенный стенд UniVPN

| Пункт ТЗ | Статус | Комментарий |
| --- | ---: | --- |
| UniVPN внутри Docker namespace | Подтверждено | `cnem_vnic` и маршруты проверены. |
| Хост напрямую не видит BO | Подтверждено | Требуется `nsenter`. |
| Доступ через `nsenter` | Подтверждено | Runner работает через `nsenter`. |
| Проверка `cnem_vnic` | Сделано | Добавлен VPN preflight. |
| Проверка маршрута `172.26.0.0/15` | Сделано | Добавлено в preflight. |
| Различение VPN-проблемы и недоступности ТС | Сделано | `vpn_client_error` и `vehicle_unreachable` разделены. |
| Права runner на `nsenter` | Решено | Runner оформлен как systemd service от root. |

Вывод: стендовая база сейчас подтверждена лучше, чем в исходном ТЗ.

## 4. API Worker

| Endpoint | Статус | Комментарий |
| --- | ---: | --- |
| `GET /health` | Сделано | Проверено через Dockerized worker. |
| `GET /capabilities` | Сделано | MVP capabilities ограничены read-only операциями. |
| `POST /tasks` | Сделано | Создаёт задачу, возвращает `task_id`. |
| `GET /tasks/{task_id}` | Сделано | Возвращает результат, ошибки, timing, runner mode. |

Дополнительно сделано сверх базового ТЗ:

| Функция | Статус |
| --- | ---: |
| Service auth | Сделано |
| `X-Request-Id` | Сделано |
| Idempotency по `request_id` | Сделано |
| Контрактные тесты API | Сделано |
| Docker packaging worker-а | Сделано |
| `GET /vehicles/resolve` | Сделано |
| File-based CSV inventory | Сделано |
| External Telegram bot HTTP contract | Сделано |

Вывод: API-слой MVP в хорошем состоянии.

## 5. Lifecycle задач

Исходное ТЗ требует явные состояния задачи: `queued`, `starting_vpn`, `vpn_connected`, `checking_vehicle`, `running_operation`, `collecting_result`, `cleanup`, `finished`, `failed`, `timeout`.

| Пункт | Статус | Комментарий |
| --- | ---: | --- |
| In-memory lifecycle | Сделано |  |
| `created/finished/failed` | Сделано |  |
| `phase` / `phase_message` | Сделано |  |
| `timeout` | Частично | Timeout внешних команд есть, полный lifecycle timeout требует дополнительной проверки. |
| `cleanup` как отдельная стадия | Частично | Для existing_container cleanup почти отсутствует по смыслу. Для job_container ещё не реализован. |
| Хранение задач после рестарта | Не сделано | Нет persistent storage. |

Вывод: lifecycle достаточно хорош для стендового MVP, но для production нужна персистентность и более строгие переходы.

## 6. MVP-операции

### Выполнено

| Операция | Статус | Проверка |
| --- | ---: | --- |
| `vehicle_reachability` | Сделано и проверено | `22=open`, `443=open`, `80=closed`. |
| `basic_status` | Сделано и проверено | SSH, hostname, uptime, time, disk, memory. |

### Ещё нужно

| Операция | Статус | Комментарий |
| --- | ---: | --- |
| `validators_status` read-only | Не сделано | Один из следующих реальных MVP-пунктов. |
| `collect_bundle_light` | Не сделано | Нужны правила состава bundle и artifact storage. |

### Второй этап

| Операция | Статус |
| --- | ---: |
| `ui_screenshot` | Отложено |
| `run_command` | Отложено |
| `select_route` | Отложено |
| `gps_gsm_status` | Отложено |
| `display_status` | Отложено |

Вывод: из 4 MVP-операций реально готовы 2. Это хороший момент для сверки с ТЗ.

## 7. Каналы доступа к ТС

| Канал | Статус | Комментарий |
| --- | ---: | --- |
| TCP checks | Сделано | `22/443/80`. |
| SSH | Сделано | Используется для `basic_status`. |
| `systemctl` | Не сделано | Понадобится для `validators_status`. |
| `journalctl` | Не сделано | Понадобится для `validators_status` и bundle. |
| HTTPS/UI BO | Не сделано | Оставлено на второй этап. |
| HTTP API BO | Не сделано | Пока нет данных по endpoint-ам. |
| Файлы/логи на ТС | Не сделано | Следующий этап для bundle/validators. |
| Browser automation | Отложено | Для `ui_screenshot`. |

Вывод: пока подтверждён SSH/TCP-минимум. Этого достаточно для первых read-only операций.

## 8. Безопасность

| Пункт ТЗ | Статус | Комментарий |
| --- | ---: | --- |
| Не писать VPN-пароль в логи | Сделано | Есть redaction tests. |
| Не возвращать секреты в API | Сделано |  |
| Worker без Docker socket | Сделано |  |
| Service auth worker API | Сделано |  |
| Runner как доверенный host-side компонент | Сделано |  |
| `inline_once` VPN mode | Сделано на уровне API | В existing_container фактическая VPN-сессия уже поднята заранее. |
| Encrypted secret store | Не сделано | Отложено. |
| TTL для сохранённых VPN-данных | Не сделано | Актуально для `stored_ref`. |
| Команда “забыть VPN-данные” | Не сделано | Больше относится к боту/secret store. |
| Удаление сообщения с паролем в Telegram | Не сделано | Это зона бота. |
| Auth worker -> runner | Не сделано | Сейчас runner слушает `0.0.0.0:8091`, это надо закрыть позже. |

Вывод: базовая безопасность worker-а соблюдена, но runner API требует защиты перед реальным использованием вне стенда.

## 9. Ограничения MVP

| Ограничение | Статус | Комментарий |
| --- | ---: | --- |
| Одна активная задача | Сделано |  |
| `worker_busy` | Сделано |  |
| Read-only первый этап | Соблюдено |  |
| Worker без Docker socket | Соблюдено |  |
| Все операции имеют timeout | Частично | Timeout команд есть, общий task timeout надо проверить отдельно. |
| VPN-сессия на задачу | Не сделано | В existing_container используется уже активная сессия. |
| Cleanup после задачи | Частично | Для existing_container почти нечего чистить; для job_container ещё нет. |
| Worker/bot без Docker socket | Соблюдено | Full-compose дизайн сохраняет Docker socket только вне worker/bot. |
| Не давать shell обычным пользователям | Соблюдено |  |
| Не делать state-changing операции | Соблюдено |  |

Вывод: MVP-ограничения соблюдены для стендового режима, кроме целевой модели “VPN-сессия на задачу”.

## 10. Нормализованные ошибки

| Ошибка | Статус |
| --- | ---: |
| `vpn_client_error` | Сделано |
| `vehicle_unreachable` | Сделано |
| `ssh_failed` | Вероятно реализовано, нужен smoke-test ошибки |
| `operation_timeout` | Сделано |
| `worker_busy` | Сделано |
| `invalid_request` | Сделано |
| `operation_not_allowed` | Сделано |
| `vpn_auth_failed` | Нет реального UniVPN auth lifecycle |
| `vpn_timeout` | Нет реального UniVPN auth lifecycle |
| `https_failed` | HTTPS ещё не реализован |
| `cleanup_failed` | В модели есть, реальные сценарии cleanup ещё не проверены |

Вывод: ошибки для existing_container покрыты неплохо. Ошибки полноценного UniVPN lifecycle пока неактуальны из-за отсутствия job_container.

## 11. Audit log

| Пункт | Статус | Комментарий |
| --- | ---: | --- |
| Audit без секретов | Сделано |  |
| Telegram user id | Сделано |  |
| Operation | Сделано |  |
| Vehicle number/IP | Сделано |  |
| task/request id | Сделано |  |
| Result/error code/duration | Сделано |  |
| State-changing flag | Частично | State-changing операций пока нет. |
| Audit для `run_command`/`select_route` | Отложено | Операции не реализованы. |

Вывод: audit для текущих read-only операций достаточный.

## 12. Artifact storage

| Пункт ТЗ | Статус |
| --- | ---: |
| Artifact metadata | Каркас есть |
| TTL metadata | Каркас есть |
| Реальное файловое хранилище | Нет |
| Download API | Нет |
| Сбор архивов | Нет |
| Очистка файлов по TTL | Нет |

Вывод: пока это каркас. Для `collect_bundle_light` станет обязательным.

## 13. Deployment

| Компонент | Статус |
| --- | ---: |
| Worker Dockerfile | Сделано |
| docker-compose для worker | Сделано |
| Worker слушает `0.0.0.0:8000` | Сделано |
| Worker без Docker socket | Сделано |
| Runner systemd service | Сделано |
| Runner env-file | Сделано |
| Runner healthcheck | Сделано |
| Проверка worker -> runner | Сделано |
| Full compose stack | Experimental | `docker-compose.full.yml` приведён к фактическому `docker inspect univpn-service`; нужен успешный smoke-test. |
| Runner container без Docker/nsenter | Сделано, mock-tested | Новый режим `container_namespace`. |
| Managed VPN login через control path | Сделано частично | Login sequence реализован; safe disconnect sequence ещё нужно уточнить. |
| External bot API contract | Сделано | См. `docs/bot_worker_api.md`. Бот остаётся внешним репозиторием. |
| Runner auth/firewall | Не сделано |
| Production deployment layout `/opt` | Не сделано |

Вывод: deployment для стенда уже рабочий. Для production нужно закрыть runner API.

## 14. Бот и пользовательский сценарий

Исходный сценарий включает ввод номера ТС, поиск IP, запрос VPN-данных, создание задачи, показ статуса и результата.

| Шаг | Статус |
| --- | ---: |
| Пользователь вводит номер ТС | Бот ещё не подключён |
| Бот ищет IP по справочнику | Нет интеграции |
| Бот запрашивает VPN-данные | Нет интеграции |
| Бот создаёт задачу в worker | Нет интеграции |
| Worker выполняет задачу | Сделано |
| Бот показывает результат | Нет интеграции |
| Контракт для внешнего бота | Сделано |
| Реализация бота в этом репозитории | Не требуется | Бот живёт в отдельном репозитории. |

Вывод: backend-часть worker-а работает, пользовательский сценарий через Telegram ещё не реализован.

## 15. Что уже можно считать выполненным

```text
1. Worker API.
2. Service auth worker-а.
3. X-Request-Id / idempotency.
4. In-memory task lifecycle.
5. worker_busy.
6. MVP capabilities.
7. Dockerized worker.
8. Host-side runner-daemon.
9. systemd service для runner-а.
10. existing_container executor.
11. VPN preflight: cnem_vnic + route 172.26.0.0/15.
12. Разделение vpn_client_error / vehicle_unreachable.
13. vehicle_reachability через реальный стенд.
14. basic_status через реальный стенд.
15. Audit redaction.
16. Command output limit.
17. Контрактные и mock-based тесты.
18. File-based CSV inventory для разрешения номера ТС в IP.
19. Endpoint `GET /vehicles/resolve`.
20. Inventory смонтирован в Dockerized worker через `/app/config/vehicles.csv`.
21. Создание задачи без явного `vehicle.ip` подтверждено на сервере через lookup по `vehicle_id`.
22. `vehicle_reachability` через resolved IP подтверждён на сервере.
23. `container_namespace` runner mode без Docker/nsenter.
24. Managed VPN login hook через UniVPN control path.
25. Experimental full compose deployment.
26. HTTP contract для внешнего Telegram-бота.
```

### File-based vehicle inventory: стендовый статус

```text
Status:
- implemented;
- mounted into Dockerized worker;
- resolve by vehicle_id confirmed on server;
- task creation without explicit vehicle.ip confirmed;
- vehicle_reachability through resolved IP confirmed.

Known data issue:
- current exported garage_number column contains row/index value, not real garage number;
- lookup by real garage number requires corrected export;
- for current smoke-tests use vehicle_id.
```

Текущий рабочий идентификатор для smoke-test ТС `6217 / 172.26.129.119`:

```text
vehicle_id = 81006217
```

В текущем CSV эта запись резолвится как:

```text
garage_number = 376
vehicle_id = 81006217
ip = 172.26.129.119
```

Это означает, что `garage_number` сейчас семантически является индексом строки выгрузки. Перед интеграцией с ботом формат inventory нужно привести к виду, где реальный номер ТС хранится в `garage_number`, а индекс строки, если нужен, вынесен в отдельное поле `inventory_row`.

## 16. Что осталось до честного стендового MVP

```text
1. validators_status read-only.
2. collect_bundle_light без download API, хотя бы с локальным artifact path/metadata.
3. Проверенный общий timeout задачи.
4. Проверенный worker_busy на долгой задаче.
5. Проверенный ssh_failed на недоступном SSH.
6. Проверенный vehicle_unreachable на ТС без открытых 22/443.
7. Runner API ограничен хотя бы firewall-ом или bind-адресом.
8. README содержит фактический статус: existing_container подтверждён, job_container отложен.
```

### Failure scenarios

| Scenario | Expected error | Status |
| --- | --- | --- |
| Inventory включён, но идентификатор ТС не найден | `vehicle_ip_not_found` | implemented/tested |
| Inventory содержит несколько подходящих записей | `vehicle_inventory_ambiguous` | implemented/tested |
| VPN container не найден, остановлен или PID не получен | `vpn_client_error` | implemented/tested |
| Нет `cnem_vnic` | `vpn_client_error` | implemented/tested |
| Нет маршрута `172.26.0.0/15` | `vpn_client_error` | implemented/tested |
| `nsenter` возвращает permission denied | `vpn_client_error` with clear message | implemented/tested |
| TCP `22/443/80` недоступны после успешного preflight | `vehicle_unreachable` | implemented/tested; requires real stand for operational proof |
| `basic_status` не может подключиться по SSH | `ssh_failed` | implemented/tested; requires real stand for operational proof |
| Внешняя команда превышает timeout | `operation_timeout` | implemented/tested |
| Вторая задача приходит при активной задаче | `worker_busy` | implemented/tested; requires real stand for long-task proof |
| `container_namespace` runner вызывает Docker/nsenter | Не должен вызывать | implemented/tested |
| `container_namespace` preflight без `cnem_vnic`/route | `vpn_client_error` | implemented/tested |
| Managed VPN login уже подключённой сессии | login skipped | implemented/tested |
| Managed VPN cleanup на success/failure | cleanup hook called | implemented/tested |
| Managed VPN cleanup failure | warning, success not hidden | implemented/tested |

Operational note:

```text
- If vehicle_ip_not_found: check freshness and format of vehicles.csv first.
- If vehicle_unreachable: check VPN preflight, resolved IP and TCP ports from inside UniVPN namespace.
- If ssh_failed: check SSH key, user, port 22 and SSH availability inside namespace.
```

## 17. Что осталось до целевого MVP по изначальному ТЗ

```text
1. Stand discovery текущего univpn-service через docker inspect.
2. Stand smoke-test full compose / container_namespace.
3. Подтвердить safe UniVPN disconnect sequence.
4. Реальный job_container runner.
5. Передача inline_once VPN-секретов в job-контейнер.
6. Проверка auth failure / vpn timeout / vpn interactive required.
7. Cleanup job-контейнера после success/failed/timeout.
8. Artifact storage и download API.
9. Persistent storage задач/idempotency.
10. Интеграция с Telegram-ботом во внешнем репозитории.
11. Production-источник номер ТС -> IP: внешний inventory service или bot-side resolver вместо file-based MVP.
12. Роли и политика доступа к конкретным ТС.
13. Защита runner API.
```

## 18. Что точно пока не трогать

Сейчас рано трогать улучшения “красоты” результата. Пока не давать задачи на:

```text
- нормализацию basic_status;
- ui_screenshot;
- run_command;
- select_route;
- stored_ref;
- encrypted secret store;
- persistent DB;
- job_container;
- сложные artifacts/download API.
```

Сначала лучше добить оставшиеся MVP-read-only операции и проверки отказов.

## 19. Следующий разумный инкремент

Не нормализация, а проверки отказов и устойчивость `existing_container`:

```text
Инкремент: hardening existing_container MVP

1. Провести real-stand smoke-test для worker_busy на долгой задаче.
2. Провести real-stand smoke-test общего timeout задачи.
3. Провести real-stand smoke-test vehicle_unreachable на заведомо недоступном IP.
4. Провести real-stand smoke-test ssh_failed при закрытом/недоступном SSH.
```

После этого можно переходить к:

```text
validators_status read-only
```

Это будет прямое движение по ТЗ, без преждевременной полировки уже работающего `basic_status`.

# Standalone UniVPN Deployment Guide

Инструкция по первичному развёртыванию и проверке изолированного VPN-сервиса UniVPN без Telegram-ботов для обслуживания нескольких независимых приложений.

---

## 1. Сборка Docker-образа UniVPN

> [!NOTE]
> Образ `local/univpn:10781.16.0.0730` собирается воспроизводимо из официального пакета разработчика (Shenzhen Leagsoft Technology Co., Ltd. / 联软科技) через [Dockerfile.univpn](file:///Users/w6itec6apel/Github/bo-vpn-agent/Dockerfile.univpn) с автоматической валидацией криптографических контрольных сумм:
> * Пакет: `https://download.leagsoft.com/download/UniVPN/linux/univpn-linux-64-10781.16.0.0730.zip`
> * SHA-256 zip: `f230bcdf198d5c3398f555b7bcb157f5f35cc7b95567e9d0b08967bcc1270c17`
> * MD5 инсталлятора `.run`: `99b8b779721114734767ced278455af9` (совпадает с [документацией вендора](https://www.leagsoft.com/doc/article/103197.html))

Сборка образа выполняется автоматически при вызове Compose:
```bash
docker compose -f docker-compose.vpn.yml build univpn-service
```
Либо вручную скриптом:
```bash
./scripts/build_univpn_image.sh
```

При необходимости образ также можно загрузить из архива:
```bash
docker load -i univpn-10781.16.0.0730.tar
```

---

## 2. Системные требования и хост-окружение

* **ОС**: AlmaLinux 8 / 9 (x86_64).
* **Ресурсы**: 4 vCPU, 6 ГБ RAM (достаточно с запасом).
* **Модуль ядра TUN/TAP**: модуль ядра Linux `tun` должен быть загружен на хосте:
  ```bash
  lsmod | grep tun || sudo modprobe tun
  # Для постоянной загрузки:
  echo tun | sudo tee /etc/modules-load.d/tun.conf
  ```
* **Права на устройство**: файл `/dev/net/tun` должен существовать на хосте:
  ```bash
  ls -la /dev/net/tun
  ```

---

## 3. Подготовка директорий, профиля и секретов на хосте

По умолчанию стек ожидает структуру файлов в `/opt/univpn` (можно переопределить через переменные окружения):

```bash
sudo mkdir -p /opt/univpn/{profile,logs,keys}
sudo chown -R $USER:$USER /opt/univpn
chmod 755 /opt/univpn
chmod 700 /opt/univpn/profile /opt/univpn/logs /opt/univpn/keys
```

### 3.1. Первичная инициализация профиля подключения UniVPN

> [!NOTE]
> На чистом сервере UniVPN не имеет сохранённых профилей подключения. Конфигурация профиля состоит из двух взаимосвязанных файлов:
> * `/home/vpn/UniVPN/sysconfig.ini` — глобальный список сессий (`[Session0] ProfileName = ...`);
> * `/home/vpn/UniVPN/config/<ProfileName>.ini` — параметры шлюза, порта и режима туннеля.
> 
> Создание профиля выполняется через утилиту `UniVPNCS` (пункт `1:New Connection -> 1:SSL VPN`). Для автоматизации этого шага без ручного ввода подготовлен скрипт `scripts/init_univpn_profile.sh`.

#### Вариант A: Автоматическая инициализация профиля (рекомендуется)
Выполните команду с указанием адреса корпоративного VPN-шлюза:
```bash
./scripts/init_univpn_profile.sh --gateway vpn.yourdomain.com --name CorporateVPN --port 443
```
Скрипт автоматически запустит изолированный контейнер `univpn-service`, настроит профиль через консольный интерфейс `UniVPNCS`, сохранит сгенерированные конфигурационные файлы в `/opt/univpn/profile` и выставит безопасные права доступа.

#### Вариант B: Интерактивная настройка через меню UniVPNCS
Если требуется ввести специфические параметры (например, выбор GmSSL или кастомного режима туннелирования):
```bash
./scripts/init_univpn_profile.sh --interactive
```
После запуска введите последовательно:
1. `1` (New Connection)
2. `1` (SSL VPN)
3. `1` (Connection Name) -> введите имя профиля
4. `3` (Gateway Address) -> введите IP или FQDN шлюза
5. `4` (Port) -> введите порт (по умолчанию 443)
6. `7` (Save)
7. `2` (Exit)

### 3.2. Безопасное хранение учётных данных VPN
Создайте файл `/opt/univpn/secret.env` с правами доступа `600`:
```bash
tee /opt/univpn/secret.env << 'EOF'
VPN_USERNAME=your_username_here
VPN_PASSWORD=your_password_here
EOF
chmod 600 /opt/univpn/secret.env
```
> [!IMPORTANT]
> Учётные данные VPN хранятся исключительно в этом локальном файле на хосте. Они никогда не включаются в образы Docker, историю git, параметры команд запуска или HTTP-запросы API. Файл монтируется в `bo-vpn-runner` в режиме `read-only` (`/run/secrets/univpn.env:ro`).

### 3.3. SSH-ключ для доступа к оборудованию (опционально)
Наличие SSH-ключа **не является блокирующим** для старта и базовой работы стека (включая TCP-прокси и задачи `vehicle_reachability`).

Если требуется выполнение задач с SSH-доступом к бортам (например, `basic_status`), поместите закрытый ключ в `/opt/univpn/keys/rsa.key`:
```bash
cp /path/to/id_rsa /opt/univpn/keys/rsa.key
chmod 600 /opt/univpn/keys/rsa.key
```
Директория `/opt/univpn/keys` монтируется в контейнер Runner как `/run/keys:ro`. Если ключ отсутствует, контейнеры запускаются без ошибок, а попытка вызова `basic_status` возвращает понятную ошибку `ssh_failed: SSH-ключ не найден`.

---

## 4. Настройка конфигурации стека

В корне репозитория скопируйте файл переменных окружения:
```bash
cp vpn.env.example .env
```
Отредактируйте `.env`:
* Установите стойкий `BO_VPN_WORKER_AUTH_TOKEN` (токен сервисной авторизации к Worker API).
* При необходимости скорректируйте пути к директориям, если они отличаются от `/opt/univpn`.
* `BO_VPN_UNIVPN_PROFILE_MENU_INDEX`: индекс профиля в меню UniVPNCS (по умолчанию `3` для первого созданного профиля). При наличии нескольких профилей в UniVPNCS этот параметр позволяет точно выбрать нужную сессию.

---

## 5. Запуск стека

Стек запускается отдельным изолированным Compose-файлом:

```bash
docker compose -f docker-compose.vpn.yml up -d --build
```

Состав запущенных сервисов:
* **`univpn-service`**: клиент UniVPN в изолированном сетевом пространстве с правами `NET_ADMIN` и `/dev/net/tun`.
* **`bo-vpn-runner`**: демон управления сессией UniVPN и SSH/TCP проверок (в `network_mode: service:univpn-service`).
* **`vpn-tcp-proxy`**: мультиплексор исходящих TCP-соединений в подсеть бортов (порт `18080`).
* **`bo-vpn-worker`**: управляющий REST API (порт `8000`, строго на `127.0.0.1`).

---

## 6. Проверка работоспособности (Healthcheck & Validation)

### 6.1. Проверка контейнеров
```bash
docker compose -f docker-compose.vpn.yml ps
```
Все 4 контейнера должны находиться в состоянии `Up`.

### 6.2. Проверка Worker API
```bash
curl -s http://127.0.0.1:8000/health
# Ожидаемый ответ:
# {"ok": true, "worker": "bo-vpn-worker", "status": "idle"}
```

Проверка capabilities с токеном:
```bash
curl -s -H "Authorization: Bearer <ВАШ_ТОКЕН>" \
        -H "X-Request-Id: $(uuidgen)" \
        http://127.0.0.1:8000/capabilities
```

### 6.3. Проверка TCP-прокси
Проверка прослушивания порта на loopback хоста:
```bash
nc -z 127.0.0.1 18080 && echo "TCP Proxy is listening"
```

### 6.4. Проверка изоляции хоста (Routing Leak Check)
Убедитесь, что маршруты и интерфейс VPN **не попали** на хостовую ОС:
```bash
ip link show dev cnem_vnic
# Должно вернуть ошибку: Device "cnem_vnic" does not exist

ip route show 172.26.0.0/15
# Вывод должен быть ПУСТЫМ (маршрута на хосте нет)
```

### 6.5. Проверка доступности борта через Worker (Canary Check)
Отправьте тестовую задачу проверки доступности по известному IP борта (`172.26.x.x`):
```bash
REQ_ID=$(uuidgen)
TASK=$(curl -s -X POST http://127.0.0.1:8000/tasks \
  -H "Authorization: Bearer <ВАШ_ТОКЕН>" \
  -H "X-Request-Id: $REQ_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "'"$REQ_ID"'",
    "telegram_user_id": 0,
    "user_role": "engineer",
    "vehicle": {"number": "canary", "ip": "172.26.130.165"},
    "vpn": {"mode": "container_secret"},
    "operation": "vehicle_reachability",
    "params": {},
    "runner_mode": "container_namespace",
    "timeout_sec": 90
  }')

echo $TASK
TASK_ID=$(echo $TASK | grep -o '"task_id":"[^"]*' | cut -d'"' -f4)

# Проверка статуса выполнения задачи:
sleep 5
curl -s -H "Authorization: Bearer <ВАШ_ТОКЕН>" \
        -H "X-Request-Id: $REQ_ID" \
        http://127.0.0.1:8000/tasks/$TASK_ID
```

---

## 7. Способы подключения сторонних Docker-приложений

### Способ 1: Подключение через TCP-прокси (Рекомендуется для SSH, HTTP/HTTPS и кастомных протоколов)
Внешнее приложение (даже работающее в отдельном `docker-compose`) отправляет трафик на хостовый адрес `127.0.0.1:18080`:
* Формат команды: `CONNECT <target_ip> <target_port>\n`
* Ответ прокси: `OK\n`
* Далее соединение переходит в прозрачный стриминг TCP-сокета.

Пример для OpenSSH (`~/.ssh/config` или флаг `-o ProxyCommand`):
```text
Host vehicle-*
    ProxyCommand /path/to/proxy_connect.py 127.0.0.1 18080 %h %p
    StrictHostKeyChecking no
    User root
    IdentityFile /path/to/rsa.key
```

### Способ 2: Подключение через общую внутреннюю сеть Docker
Сеть `bo-vpn-internal` объявлена с флагом `attachable: true`. Сторонний `docker-compose.yml` может подключиться к ней напрямую:
```yaml
services:
  my-app:
    image: my-app:latest
    networks:
      - default
      - vpn-net

networks:
  vpn-net:
    name: bo-vpn-internal
    external: true
```
Внутри сети `my-app` может:
* Обращаться к Worker API: `http://bo-vpn-worker:8000`
* Обращаться к TCP Proxy: `univpn-service:18080` (без выхода на хостовый loopback).

### Способ 3: Подключение через REST API Worker
Для сервисов, которым нужны только результаты предварительной диагностики доступности оборудования (порты 22, 80, 443) или базовый сбор SSH-метрик (`basic_status`), достаточно делать HTTP-запросы в Worker API по адресу `http://127.0.0.1:8000` с заголовком авторизации.

---

## 8. Архитектура и требования для многопользовательского режима (Multi-Engineer Architecture)

В будущем VPN Gateway будет обслуживать нескольких инженеров одновременно или поочерёдно. Ниже описаны ключевые архитектурные ограничения и требования к масштабированию системы.

### 8.1. Монопольная природа туннеля UniVPN (Single-Session Nature)
* Проприетарный клиент `UniVPNCS` поднимает **один виртуальный сетевой интерфейс** (`cnem_vnic`) в своём сетевом пространстве и поддерживает ровно одну активную сессию авторизации на шлюзе.
* Клиент не поддерживает одновременное туннелирование трафика от разных учётных записей через один сетевой интерфейс.
* **Архитектурные стратегии:**
  1. **Режим общего шлюза (Shared Service Tunnel)** — текущая базовая модель:
     * VPN-туннель удерживается постоянно через выделенный сервисный аккаунт (`BO_VPN_STOP_VPN_AFTER_TASK=false`).
     * Все инженеры и внешние приложения обращаются к бортам через общий туннель (по TCP-прокси или Worker API).
     * Персональная ответственность обеспечивается на уровне Worker API и аудита.
  2. **Режим сериализованных персональных сессий (Session Locking & Queuing)**:
     * Если политика безопасности требует входа в VPN под личной учётной записью инженера: Runner захватывает эксклюзивный lock, авторизуется с личными кредами (`inline_once`), выполняет задачу, завершает сессию и освобождает очередь.
  3. **Мульти-стек инстанцирование (Multi-Tenant Stacks)**:
     * Для параллельной работы нескольких независимых VPN-туннелей запускаются параллельные Compose-проекты (`univpn-alice`, `univpn-bob`), каждый со своим выделенным `/dev/net/tun` и отдельным портом TCP-прокси (`18081`, `18082`).

### 8.2. Разделение персональных учётных данных и SSH-ключей
* **Учётные данные VPN:**
  * Передача одноразовых / сессионных токенов через защищённый TLS-канал в теле запроса (`inline_once`), без сохранения на диск контейнера.
  * Либо маппинг пользователей на защищённые файлы секретов (`/opt/univpn/secrets/<engineer_id>.env`).
* **SSH-ключи инженеров:**
  * Хранение персональных публичных/приватных ключей в `/opt/univpn/keys/<engineer_id>.key`.
  * Передача имени ключа или пользователя в параметрах задачи (`ssh_key_name`, `ssh_user`).
  * Runner проверяет принадлежность ключа вызывающему инженеру и использует соответствующий файл.

### 8.3. Сохранение идентичности пользователя в аудите и задачах
* Каждая операция в Worker API обязательно содержит контекст инициатора:
  * `telegram_user_id` или `user_id` (строковый идентификатор инженера / сервис-аккаунта);
  * `user_role` (`engineer`, `admin`, `auditor`);
  * `client_ip` и заголовок `X-Request-Id`.
* Неизменяемый структурированный лог аудита (`audit.jsonl`) фиксирует каждую попытку подключения, команду, целевой IP борта и результат.
* Журнал аудита сохраняется на хостовой файловой системе (`/app/logs/audit.jsonl` -> `./logs/audit.jsonl`) и защищён от модификации извне.

### 8.4. Контроль доступа и ограничения (Rate Limiting & RBAC)
* Лимиты создания задач разделены на:
  * глобальный пул (`BO_VPN_GLOBAL_CREATE_LIMIT_PER_MINUTE`, по умолчанию 60);
  * личный лимит на пользователя (`BO_VPN_USER_CREATE_LIMIT_PER_MINUTE`, по умолчанию 10).
* Разграничение прав на опасные операции:
  * Просмотр статуса и доступности (`vehicle_reachability`, `basic_status`) доступен всем авторизованным инженерам.
  * Операции с модификацией состояния или запуском сторонних бинарников требуют роли `admin`.

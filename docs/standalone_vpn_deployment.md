# Standalone UniVPN Deployment Guide

Инструкция по первичному развёртыванию и проверке изолированного VPN-сервиса UniVPN без Telegram-ботов для обслуживания нескольких независимых приложений.

---

## 1. Сборка Docker-образа UniVPN

> [!NOTE]
> Образ `local/univpn:10781.16.0.0730` собирается воспроизводимо из официального пакета разработчика (Shenzhen Leagsoft Technology Co., Ltd. / 联软科技) через [Dockerfile.univpn](file:///Users/w6itec6apel/Github/bo-vpn-agent/Dockerfile.univpn) с автоматической валидацией криптографических контрольных сумм:
> * Пакет: `https://download.leagsoft.com/download/UniVPN/linux/univpn-linux-64-10781.16.0.0730.zip`
> * SHA-256 zip: `baa5c5f94119a44c068b531465be62eebf5c5f167eab103706ec6391d547aa68`
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

## 3. Подготовка директорий и секретов на хосте

По умолчанию стек ожидает структуру файлов в `/opt/univpn` (можно переопределить через переменные окружения):

```bash
sudo mkdir -p /opt/univpn/{profile,logs}
sudo chmod 755 /opt/univpn
sudo chmod 700 /opt/univpn/profile /opt/univpn/logs
```

### 3.1. Секрет учетных данных VPN
Создайте файл `/opt/univpn/secret.env` (на основе [examples/univpn.secret.env.example](file:///Users/w6itec6apel/Github/bo-vpn-agent/examples/univpn.secret.env.example)):
```bash
sudo tee /opt/univpn/secret.env << 'EOF'
VPN_USERNAME=your_username_here
VPN_PASSWORD=your_password_here
EOF
sudo chmod 600 /opt/univpn/secret.env
```

### 3.2. SSH-ключ для доступа к бортам (при необходимости SSH-диагностики)
Поместите закрытый ключ в `/opt/univpn/rsa.key`:
```bash
sudo chmod 600 /opt/univpn/rsa.key
```

---

## 4. Настройка конфигурации стека

В корне репозитория скопируйте файл переменных окружения:
```bash
cp vpn.env.example .env
```
Отредактируйте `.env`:
* Установите стойкий `BO_VPN_WORKER_AUTH_TOKEN` (токен сервисной авторизации к Worker API).
* При необходимости скорректируйте пути к директориям, если они отличаются от `/opt/univpn`.

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

from __future__ import annotations

import csv
import ipaddress
import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("telegram-audio-bot")
TERMINAL_TASK_STATES = {"finished", "failed", "timeout"}
CHANNELS = {"salon": "Салон", "street": "Улица", "tangenta": "Тангента"}
BRANCH_NAMES = {"pat": "ПАТ", "piteravto": "Питеравто"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac"}
MAX_AUDIO_BYTES = 20 * 1024 * 1024
KEYBOARD = {
    "keyboard": [
        [{"text": "🎯 Выбрать ТС"}, {"text": "🏢 Филиал"}],
        [{"text": "📋 Статус"}],
        [{"text": "⏹ Стоп"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}
BUTTON_COMMANDS = {
    "🎯 Выбрать ТС": "/target",
    "🏢 Филиал": "/branch",
    "📋 Статус": "/status",
    "⏹ Стоп": "/stop",
}
DEFAULT_BRANCH_ALIASES = {
    "piteravto": "piteravto",
    "питеравто": "piteravto",
    "питер": "piteravto",
    "pa": "piteravto",
    "па": "piteravto",
    "pat": "pat",
    "пат": "pat",
    "ap6": "pat",
    "ап6": "pat",
    "ап-6": "pat",
    "passazhiravtotrans": "pat",
}


def _marker_int(line: str, key: str) -> int | None:
    prefix = f"{key}="
    for token in line.split():
        if token.startswith(prefix):
            try:
                return int(token.removeprefix(prefix))
            except ValueError:
                return None
    return None


def _playback_error_message(returncode: int, output: str) -> str:
    if "TRANSFER_FAILED" in output or "not responding" in output or "timed out" in output:
        return "Не удалось передать трек: SSH ТС перестал отвечать. Попробуй ещё раз позже."
    if "PLAYBACK_FAILED" in output:
        return "Трек передан, но аудиоканал на ТС не запустился."
    return f"Ошибка воспроизведения (код {returncode})."


@dataclass(slots=True)
class Target:
    branch: str
    number: str
    ip: str
    volume: int = 25
    output: str = "salon"


@dataclass(slots=True)
class PlaybackJob:
    chat_id: int
    cancelled: bool = False
    process: subprocess.Popen[str] | None = None


class TelegramApi:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"

    def call(self, method: str, data: dict[str, Any] | None = None, timeout: int = 40) -> dict[str, Any]:
        body = urlencode(data or {}).encode("utf-8")
        request = Request(f"{self.base_url}/{method}", data=body, method="POST")
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Telegram API URL
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {payload}")
        return payload["result"]

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        self.call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "reply_markup": json.dumps(reply_markup or KEYBOARD, ensure_ascii=False)},
        )

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            data["offset"] = offset
        return self.call("getUpdates", data, timeout=35)

    def download(self, file_id: str, destination: Path) -> None:
        file_info = self.call("getFile", {"file_id": file_id})
        if int(file_info.get("file_size") or 0) > MAX_AUDIO_BYTES:
            raise RuntimeError("Файл слишком большой. Отправь аудио размером до 20 МБ.")
        file_path = str(file_info["file_path"])
        with urlopen(f"{self.file_base_url}/{file_path}", timeout=120) as response:  # noqa: S310
            with destination.open("wb") as output:
                total = 0
                while chunk := response.read(65536):
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise RuntimeError("Файл слишком большой. Отправь аудио размером до 20 МБ.")
                    output.write(chunk)


class AudioBot:
    def __init__(self) -> None:
        token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
        allowed_raw = os.environ["TELEGRAM_ALLOWED_USER_IDS"]
        self.allowed_users = {int(value.strip()) for value in allowed_raw.split(",") if value.strip()}
        self.api = TelegramApi(token)
        self.worker_url = os.getenv("BO_VPN_WORKER_URL", "http://bo-vpn-worker:8000").rstrip("/")
        self.worker_token = os.getenv("BO_VPN_WORKER_AUTH_TOKEN", "dev-token")
        self.audio_script = Path(os.getenv("AUDIO_SCRIPT", "/app/scripts/play_vehicle_audio.sh"))
        self.vehicle_map_path = Path(os.getenv("VEHICLE_MAP_PATH", "/app/config/vehicle_ip_map.csv"))
        self.vehicle_maps = self._load_vehicle_maps()
        self.branch_aliases = self._load_branch_aliases()
        self.default_branch = self._normalize_branch(os.getenv("VEHICLE_DEFAULT_BRANCH", "piteravto"))
        if self.default_branch is None:
            self.default_branch = sorted(self.vehicle_maps)[0]
        self.temp_dir = Path(os.getenv("AUDIO_TMP_DIR", "/tmp/audio-bot"))
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.targets: dict[int, Target] = {}
        self.awaiting_target: set[int] = set()
        self.awaiting_branch: set[int] = set()
        self.user_branches: dict[int, str] = {}
        self.user_audio: dict[int, dict[str, Any]] = {}
        self.awaiting_volume: set[int] = set()
        self.settings_path = Path(os.getenv("AUDIO_SETTINGS_PATH", "/data/audio-settings.json"))
        self.jobs: dict[int, PlaybackJob] = {}
        self.lock = threading.RLock()
        self._load_settings()

    def run(self) -> None:
        LOGGER.info(
            "audio bot started; allowed_users=%s vehicles=%s",
            sorted(self.allowed_users),
            sum(len(mapping) for mapping in self.vehicle_maps.values()),
        )
        offset: int | None = None
        while True:
            try:
                for update in self.api.get_updates(offset):
                    offset = int(update["update_id"]) + 1
                    callback = update.get("callback_query")
                    if isinstance(callback, dict):
                        self.handle_callback(callback)
                    message = update.get("message")
                    if isinstance(message, dict):
                        self.handle_message(message)
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                LOGGER.warning("telegram polling error: %s", type(exc).__name__)
                time.sleep(3)

    def handle_message(self, message: dict[str, Any]) -> None:
        sender = message.get("from") or {}
        user_id = int(sender.get("id", 0))
        chat_id = int((message.get("chat") or {}).get("id", 0))
        if user_id not in self.allowed_users:
            LOGGER.warning("access denied for telegram_user_id=%s", user_id)
            return

        text = str(message.get("text") or "").strip()
        text = BUTTON_COMMANDS.get(text, text)
        if text.startswith("/"):
            self.handle_command(user_id, chat_id, text)
            return

        if text and user_id in self.awaiting_volume:
            self.handle_command(user_id, chat_id, f"/volume {text}")
            return

        if text and user_id in self.awaiting_target:
            self.handle_command(user_id, chat_id, f"/target {text}")
            return

        if text and user_id in self.awaiting_branch:
            self.awaiting_branch.discard(user_id)
            self.handle_command(user_id, chat_id, f"/branch {text}")
            return

        media = self._media(message)
        if media is None:
            self.api.send_message(chat_id, "Пришли трек Telegram, голосовое или аудиофайл: MP3, M4A, AAC, OGG, WAV, FLAC (до 20 МБ).")
            return

        if any(int((message.get(kind) or {}).get("file_size") or 0) > MAX_AUDIO_BYTES
               for kind in ("voice", "audio", "document")):
            self.api.send_message(chat_id, "Файл слишком большой. Отправь аудио размером до 20 МБ.")
            return

        with self.lock:
            target = self.targets.get(user_id)
            active = self.jobs.get(user_id)
        if target is None:
            self.handle_command(user_id, chat_id, "/target")
            return
        if active is not None:
            self.api.send_message(chat_id, "У тебя уже идёт задача. Используй /stop.")
            return

        file_id, suffix = media
        job = PlaybackJob(chat_id=chat_id)
        with self.lock:
            self.jobs[user_id] = job
        threading.Thread(
            target=self._play,
            args=(user_id, file_id, suffix, target, job),
            daemon=True,
        ).start()

    def handle_command(self, user_id: int, chat_id: int, text: str) -> None:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            self._clear_input(user_id)
            self.api.send_message(
                chat_id,
                "Команды:\n"
                "/branch ФИЛИАЛ — выбрать филиал\n"
                "/target НОМЕР_ТС [громкость] [канал]\n"
                "/target ФИЛИАЛ НОМЕР_ТС [громкость] [канал]\n"
                "/status — текущая цель\n"
                "/stop — остановить звук\n\n"
                "/volume 15 — громкость для следующего аудио\n"
                "/channel — выбрать канал\n\n"
                "Пришли или перешли трек Telegram, голосовое, MP3, M4A, AAC, OGG, WAV или FLAC (до 20 МБ).\n"
                "Филиалы: piteravto, pat.\n"
                "Каналы: salon, street, tangenta.",
            )
            self._panel(user_id, chat_id)
            return
        if command == "/channel":
            self._clear_input(user_id)
            self._panel(user_id, chat_id, mode="channel")
            return
        if command == "/volume":
            self._clear_input(user_id)
            if len(parts) == 1:
                self.awaiting_volume.add(user_id)
                self.api.send_message(chat_id, "Введи громкость от 1 до 100 для следующего аудио.")
                return
            try:
                volume = int(parts[1])
                if not 1 <= volume <= 100:
                    raise ValueError
            except ValueError:
                self.awaiting_volume.add(user_id)
                self.api.send_message(chat_id, "Нужно целое число от 1 до 100.")
                return
            self._set_audio(user_id, volume=volume)
            self._panel(user_id, chat_id)
            return
        if command == "/branch":
            self._handle_branch(user_id, chat_id, parts[1:])
            return
        if command == "/target":
            if len(parts) == 1:
                self._clear_input(user_id)
                if user_id not in self.user_branches:
                    self._handle_branch(user_id, chat_id, [])
                    return
                self.awaiting_target.add(user_id)
                self.api.send_message(
                    chat_id,
                    f"Филиал: {BRANCH_NAMES.get(self._branch_for_user(user_id), self._branch_for_user(user_id))}. Введи номер ТС.",
                )
                return
            try:
                target = self._parse_target(user_id, parts[1:])
            except (ValueError, IndexError) as exc:
                self.api.send_message(chat_id, f"Ошибка: {exc}\nПример: /target pat 6317 25 salon")
                return
            with self.lock:
                self._clear_input(user_id)
                self.targets[user_id] = target
                self.user_branches[user_id] = target.branch
                self.user_audio[user_id] = {"volume": target.volume, "output": target.output}
                self._save_settings()
            self._panel(user_id, chat_id)
            return
        if command == "/status":
            self._clear_input(user_id)
            self._panel(user_id, chat_id)
            return
        if command == "/stop":
            self._clear_input(user_id)
            self._stop(user_id, chat_id)
            return
        self.api.send_message(chat_id, "Неизвестная команда. Используй /help.")

    def _handle_branch(self, user_id: int, chat_id: int, args: list[str]) -> None:
        if not args:
            self._clear_input(user_id)
            self.awaiting_branch.add(user_id)
            self._panel(user_id, chat_id, mode="branch")
            return
        branch = self._normalize_branch(args[0])
        if branch is None:
            raise_message = f"неизвестный филиал {args[0]}. Доступны: {', '.join(sorted(self.vehicle_maps))}"
            self.api.send_message(chat_id, f"Ошибка: {raise_message}")
            return
        with self.lock:
            self._clear_input(user_id)
            self.user_branches[user_id] = branch
            self.targets.pop(user_id, None)
            self._save_settings()
        self.awaiting_target.add(user_id)
        self.api.send_message(chat_id, f"Филиал сохранён: {branch}\nВведи номер ТС.")

    def _parse_target(self, user_id: int, args: list[str]) -> Target:
        if not args:
            raise ValueError("не указан номер ТС")
        branch = self._normalize_branch(args[0])
        explicit_branch = branch is not None
        if explicit_branch:
            args = args[1:]
        else:
            branch = self.user_branches.get(user_id)
        if not args:
            raise ValueError("не указан номер ТС")
        number = args[0].strip()
        matches = [name for name, mapping in self.vehicle_maps.items() if number in mapping]
        if branch is None:
            if len(matches) > 1:
                raise ValueError(
                    f"номер ТС {number} есть в нескольких филиалах: {', '.join(sorted(matches))}. "
                    f"Укажи филиал: /target ФИЛИАЛ {number}"
                )
            branch = matches[0] if matches else self.default_branch
        ip = self.vehicle_maps.get(branch, {}).get(number)
        if ip is None:
            if matches:
                raise ValueError(
                    f"ТС {number} найдена в филиале {matches[0]}, но выбран филиал {branch}. "
                    f"Выбери: /branch {matches[0]}"
                )
            raise ValueError(f"ТС {number} нет в справочнике филиала {branch}")
        address = ipaddress.ip_address(ip)
        if address.version != 4 or address not in ipaddress.ip_network("172.26.0.0/15"):
            raise ValueError("нужен адрес МНТ из сети 172.26.0.0/15")
        settings = self.user_audio.get(user_id, {})
        volume = int(args[1]) if len(args) > 1 else settings.get("volume", 25)
        output = args[2] if len(args) > 2 else settings.get("output", "salon")
        if not 1 <= volume <= 100:
            raise ValueError("громкость должна быть от 1 до 100")
        if output not in {"salon", "street", "tangenta"}:
            raise ValueError("канал: salon, street или tangenta")
        if explicit_branch:
            self.user_branches[user_id] = branch
        return Target(branch=branch, number=number, ip=str(address), volume=volume, output=output)

    def _branch_for_user(self, user_id: int) -> str:
        return self.user_branches.get(user_id, self.default_branch)

    def _clear_input(self, user_id: int) -> None:
        self.awaiting_target.discard(user_id)
        self.awaiting_branch.discard(user_id)
        self.awaiting_volume.discard(user_id)

    def _set_audio(self, user_id: int, **changes: Any) -> None:
        with self.lock:
            settings = {"volume": 25, "output": "salon", **self.user_audio.get(user_id, {}), **changes}
            self.user_audio[user_id] = settings
            if user_id in self.targets:
                # A running playback keeps the settings captured when its file arrived.
                self.targets[user_id] = replace(self.targets[user_id], **settings)
            self._save_settings()

    def _panel(self, user_id: int, chat_id: int, mode: str = "main", message_id: int | None = None) -> None:
        target = self.targets.get(user_id)
        settings = self.user_audio.get(user_id, {})
        branch = self._branch_for_user(user_id)
        volume = settings.get("volume", 25)
        output = settings.get("output", "salon")
        text = (
            f"Филиал: {BRANCH_NAMES.get(branch, branch)}\n"
            + (f"ТС: {target.number}\nIP: {target.ip}\n" if target else "ТС не выбрана\n")
            + f"Громкость: {volume}%\nКанал: {CHANNELS[output]}\n"
            + ("Задача выполняется. Настройки ниже — для следующего аудио."
               if user_id in self.jobs else "Готов к аудио" if target else "Выбери филиал и ТС")
        )

        def button(label: str, action: str) -> dict[str, str]:
            return {"text": label, "callback_data": f"audio:{user_id}:{action}"}

        rows = []
        if mode == "branch":
            rows.append([button(BRANCH_NAMES.get(name, name), f"branch={name}") for name in sorted(self.vehicle_maps)])
        elif mode == "channel":
            rows.append([button(("✓ " if name == output else "") + label, f"channel={name}")
                         for name, label in CHANNELS.items()])
        rows.extend([
            [button("Филиал", "branch"), button("Выбрать ТС", "target")],
            [button(f"Канал: {CHANNELS[output]}", "channel")],
            [button("−5%", "quieter"), button(f"{volume}%", "volume"), button("+5%", "louder")],
            [button("Стоп", "stop"), button("Обновить", "status")],
        ])
        markup = {"inline_keyboard": rows}
        if message_id is None:
            self.api.send_message(chat_id, text, markup)
        else:
            try:
                self.api.call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                                  "text": text, "reply_markup": json.dumps(markup, ensure_ascii=False)})
            except HTTPError as exc:
                if exc.code != 400:
                    raise
                details = exc.read().decode("utf-8", errors="replace")
                if "message is not modified" not in details:
                    self.api.send_message(chat_id, text, markup)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        user_id = int((callback.get("from") or {}).get("id", 0))
        parts = str(callback.get("data") or "").split(":", 2)
        if user_id not in self.allowed_users or len(parts) != 3 or parts[:2] != ["audio", str(user_id)]:
            self.api.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Это не твоя панель. Открой /start."})
            return
        self.api.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
        message = callback.get("message") or {}
        if not message.get("chat") or not message.get("message_id"):
            return
        chat_id = int(message["chat"]["id"])
        message_id = int(message["message_id"])
        action = parts[2]
        self._clear_input(user_id)
        if action.startswith("branch="):
            self._handle_branch(user_id, chat_id, [action.partition("=")[2]])
        elif action.startswith("channel="):
            output = action.partition("=")[2]
            if output in CHANNELS:
                self._set_audio(user_id, output=output)
                self._panel(user_id, chat_id, message_id=message_id)
        elif action in {"quieter", "louder"}:
            current = self.user_audio.get(user_id, {}).get("volume", 25)
            self._set_audio(user_id, volume=max(1, min(100, current + (5 if action == "louder" else -5))))
            self._panel(user_id, chat_id, message_id=message_id)
        elif action in {"branch", "channel", "status"}:
            if action == "branch":
                self.awaiting_branch.add(user_id)
            self._panel(user_id, chat_id, mode=action, message_id=message_id)
        elif action in {"target", "volume", "stop"}:
            self.handle_command(user_id, chat_id, f"/{action}")

    def _save_settings(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        users = {}
        for user_id in self.user_branches.keys() | self.user_audio.keys():
            target = self.targets.get(user_id)
            users[str(user_id)] = {**self.user_audio.get(user_id, {}),
                                   "branch": self.user_branches.get(user_id),
                                   "number": target.number if target else None}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.settings_path.parent,
                                             prefix=".audio-settings-", delete=False) as destination:
                temporary = Path(destination.name)
                json.dump(users, destination, ensure_ascii=False)
            temporary.replace(self.settings_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _load_settings(self) -> None:
        try:
            users = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            LOGGER.warning("Cannot read audio settings; using defaults")
            return
        if not isinstance(users, dict):
            return
        for key, settings in users.items():
            try:
                user_id = int(key)
                if user_id not in self.allowed_users or not isinstance(settings, dict):
                    continue
                volume = int(settings.get("volume", 25))
                output = settings.get("output", "salon")
                if not 1 <= volume <= 100 or output not in CHANNELS:
                    continue
                self.user_audio[user_id] = {"volume": volume, "output": output}
                branch = self._normalize_branch(settings.get("branch"))
                if branch is not None:
                    self.user_branches[user_id] = branch
                    if settings.get("number"):
                        # Resolve again from the current inventory, never persist a stale IP.
                        self.targets[user_id] = self._parse_target(user_id, [branch, str(settings["number"])])
            except (ValueError, TypeError, AttributeError):
                continue

    def _normalize_branch(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip().lower().replace("_", "-")
        branch = self.branch_aliases.get(normalized) if hasattr(self, "branch_aliases") else DEFAULT_BRANCH_ALIASES.get(normalized)
        if branch in getattr(self, "vehicle_maps", {}):
            return branch
        if normalized in getattr(self, "vehicle_maps", {}):
            return normalized
        return None

    @staticmethod
    def _load_branch_aliases() -> dict[str, str]:
        aliases = dict(DEFAULT_BRANCH_ALIASES)
        raw = os.getenv("VEHICLE_BRANCH_ALIASES", "")
        for item in raw.split(","):
            if not item.strip() or "=" not in item:
                continue
            alias, branch = item.split("=", 1)
            aliases[alias.strip().lower().replace("_", "-")] = branch.strip().lower()
        return aliases

    def _load_vehicle_maps(self) -> dict[str, dict[str, str]]:
        raw = os.getenv("VEHICLE_MAP_PATHS", "").strip()
        if not raw:
            return {"piteravto": self._load_vehicle_map(self.vehicle_map_path)}
        maps: dict[str, dict[str, str]] = {}
        for item in raw.split(","):
            if not item.strip():
                continue
            if "=" not in item:
                raise RuntimeError("VEHICLE_MAP_PATHS must use branch=/path entries")
            branch, path = item.split("=", 1)
            branch = branch.strip().lower()
            maps[branch] = self._load_vehicle_map(Path(path.strip()))
        if not maps:
            raise RuntimeError("справочники ТС пусты")
        return maps

    @staticmethod
    def _load_vehicle_map(path: Path) -> dict[str, str]:
        mapping: dict[str, str] = {}
        with path.open(newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                number = str(row.get("vehicle_number") or "").strip()
                ip = str(row.get("ip") or "").strip()
                if not number or not ip:
                    continue
                address = ipaddress.ip_address(ip)
                if address.version == 4 and address in ipaddress.ip_network("172.26.0.0/15"):
                    mapping[number] = str(address)
        if not mapping:
            raise RuntimeError(f"справочник ТС пуст: {path}")
        return mapping

    @staticmethod
    def _media(message: dict[str, Any]) -> tuple[str, str] | None:
        voice = message.get("voice")
        if isinstance(voice, dict) and voice.get("file_id"):
            return str(voice["file_id"]), ".ogg"
        audio = message.get("audio")
        if isinstance(audio, dict) and audio.get("file_id"):
            suffix = Path(str(audio.get("file_name") or "")).suffix.lower()
            return str(audio["file_id"]), suffix if suffix in AUDIO_SUFFIXES else ".media"
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            name = str(document.get("file_name") or "")
            suffix = Path(name).suffix.lower()
            if suffix in AUDIO_SUFFIXES or str(document.get("mime_type") or "").startswith("audio/"):
                return str(document["file_id"]), suffix if suffix in AUDIO_SUFFIXES else ".media"
        return None

    @staticmethod
    def _prepare_audio(source: Path, destination: Path, job: PlaybackJob) -> None:
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
                   "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:a:0",
                   "-vn", "-map_metadata", "-1", "-ac", "2", "-ar", "48000",
                   "-c:a", "libmp3lame", "-b:a", "192k", "-y", str(destination)]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        job.process = process
        try:
            if job.cancelled:
                process.terminate()
            try:
                result = process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError("Подготовка аудио заняла слишком много времени.") from None
            if result != 0 and not job.cancelled:
                raise RuntimeError("Не удалось прочитать аудио. Файл повреждён или формат не поддерживается.")
        finally:
            job.process = None

    def _play(self, user_id: int, file_id: str, suffix: str, target: Target, job: PlaybackJob) -> None:
        local_file = self.temp_dir / f"{uuid.uuid4().hex}{suffix}"
        prepared_file = self.temp_dir / f"{uuid.uuid4().hex}.mp3"
        try:
            self.api.send_message(job.chat_id, "Скачиваю файл из Telegram…")
            self.api.download(file_id, local_file)
            if job.cancelled:
                self.api.send_message(job.chat_id, "Задача отменена.")
                return

            self.api.send_message(job.chat_id, "Подготавливаю аудио…")
            self._prepare_audio(local_file, prepared_file, job)
            if job.cancelled:
                self.api.send_message(job.chat_id, "Задача отменена.")
                return

            self.api.send_message(job.chat_id, "Проверяю VPN и доступность ТС…")
            self._ensure_vehicle_ready(user_id, target.number, target.ip)
            if job.cancelled:
                self.api.send_message(job.chat_id, "Задача отменена.")
                return

            command = [
                str(self.audio_script),
                target.ip,
                str(prepared_file),
                str(target.volume),
                target.output,
            ]
            self.api.send_message(
                job.chat_id,
                f"Передаю трек на ТС {target.number} ({target.ip}). "
                "После загрузки воспроизведение начнётся автоматически.\nДля отмены: /stop",
            )
            with self.lock:
                if job.cancelled:
                    self.api.send_message(job.chat_id, "Задача отменена.")
                    return
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                job.process = process
            output = ""
            playback_announced = False
            for line in process.stdout or ():
                output = (output + line)[-4000:]
                if line.startswith("TRANSFER_WAIT "):
                    seconds = _marker_int(line, "seconds")
                    if seconds:
                        self.api.send_message(
                            job.chat_id,
                            f"Трек всё ещё передаётся на ТС: {seconds} сек. Ожидаю медленный SSH…",
                        )
                elif line.startswith("TRANSFER_RETRY "):
                    self.api.send_message(
                        job.chat_id,
                        "Связь с ТС прервалась во время передачи. Повторяю загрузку…",
                    )
                elif line.startswith("TRANSFER_COMPLETE "):
                    self.api.send_message(job.chat_id, "Трек передан. Запускаю аудиоканал…")
                elif not playback_announced and line.startswith("Playback started:"):
                    playback_announced = True
                    self.api.send_message(
                        job.chat_id,
                        f"Воспроизведение началось на ТС {target.number} ({target.ip}): "
                        f"{target.output}, {target.volume}%.\nДля остановки: /stop",
                    )
            process.wait()
            LOGGER.info("playback ip=%s rc=%s output=%s", target.ip, process.returncode, output[-4000:])
            if process.returncode == 0:
                self.api.send_message(job.chat_id, "Готово: воспроизведение завершено, временные файлы удалены.")
            elif job.cancelled or process.returncode in {130, -signal.SIGINT}:
                self.api.send_message(job.chat_id, "Остановлено. Временные файлы удалены.")
            else:
                self.api.send_message(job.chat_id, _playback_error_message(process.returncode, output))
        except Exception as exc:  # noqa: BLE001 - report experimental job failures to operator
            LOGGER.exception("audio job failed")
            self.api.send_message(job.chat_id, f"Ошибка: {exc}")
        finally:
            local_file.unlink(missing_ok=True)
            prepared_file.unlink(missing_ok=True)
            with self.lock:
                if self.jobs.get(user_id) is job:
                    self.jobs.pop(user_id, None)

    def _ensure_vehicle_ready(self, user_id: int, number: str, ip: str) -> None:
        attempts = 3
        for attempt in range(1, attempts + 1):
            result = self._check_vehicle_once(user_id, number, ip)
            if result is None:
                return
            error_code, message = result
            if error_code != "vehicle_unreachable" or attempt == attempts:
                raise RuntimeError(message)
            LOGGER.warning(
                "vehicle reachability transient failure ip=%s attempt=%s/%s",
                ip,
                attempt,
                attempts,
            )
            time.sleep(2)

    def _check_vehicle_once(self, user_id: int, number: str, ip: str) -> tuple[str, str] | None:
        request_id = f"audio-{uuid.uuid4()}"
        payload = {
            "request_id": request_id,
            "telegram_user_id": user_id,
            "user_role": "engineer",
            "vehicle": {"number": number, "ip": ip},
            "vpn": {"mode": "container_secret"},
            "operation": "vehicle_reachability",
            "params": {},
            "runner_mode": "container_namespace",
            "timeout_sec": 90,
        }
        task = self._worker_request("POST", "/tasks", request_id, payload)
        task_id = str(task["task_id"])
        deadline = time.monotonic() + 95
        while time.monotonic() < deadline:
            status = self._worker_request("GET", f"/tasks/{task_id}", request_id)
            state = str(status.get("state"))
            if state == "finished":
                return None
            if state in TERMINAL_TASK_STATES:
                return (
                    str(status.get("error_code") or state),
                    str(status.get("message") or status.get("phase_message") or state),
                )
            time.sleep(2)
        raise RuntimeError("проверка VPN/ТС превысила timeout")

    def _worker_request(
        self,
        method: str,
        path: str,
        request_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.worker_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.worker_token}",
                "X-Request-Id": request_id,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - configured internal worker URL
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"worker HTTP {exc.code}: {details}") from exc

    def _stop(self, user_id: int, chat_id: int) -> None:
        with self.lock:
            job = self.jobs.get(user_id)
            if job is None:
                self.api.send_message(chat_id, "Активного воспроизведения нет.")
                return
            job.cancelled = True
            process = job.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            self.api.send_message(chat_id, "Останавливаю воспроизведение…")
        else:
            self.api.send_message(chat_id, "Отменяю задачу до начала воспроизведения…")

    def stop_all(self) -> None:
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            job.cancelled = True
            if job.process is not None and job.process.poll() is None:
                try:
                    os.killpg(job.process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    bot = AudioBot()

    def stop_handler(_signum: int, _frame: object) -> None:
        bot.stop_all()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    bot.run()


if __name__ == "__main__":
    main()

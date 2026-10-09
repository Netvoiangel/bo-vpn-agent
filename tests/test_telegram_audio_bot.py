from __future__ import annotations

import os
import json
import subprocess
import shutil
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock

from bo_vpn_agent.telegram_audio_bot import AudioBot, DEFAULT_BRANCH_ALIASES, PlaybackJob, TelegramApi


class FakeApi:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.markups = []
        self.calls = []

    def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        self.messages.append((chat_id, text))
        self.markups.append(reply_markup)

    def call(self, method, data):
        self.calls.append((method, data))
        return True


def write_map(path: Path, rows: list[tuple[str, str]]) -> None:
    lines = ["vehicle_number,ip"]
    lines.extend(f"{number},{ip}" for number, ip in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_bot(vehicle_maps: dict[str, dict[str, str]], default_branch: str = "piteravto") -> AudioBot:
    bot = AudioBot.__new__(AudioBot)
    bot.vehicle_maps = vehicle_maps
    bot.branch_aliases = dict(DEFAULT_BRANCH_ALIASES)
    bot.default_branch = default_branch
    bot.allowed_users = {100}
    bot.user_branches = {}
    bot.targets = {}
    bot.awaiting_target = set()
    bot.awaiting_branch = set()
    bot.awaiting_volume = set()
    bot.user_audio = {}
    bot._test_tmp = TemporaryDirectory()
    bot.settings_path = Path(bot._test_tmp.name) / "settings.json"
    bot.jobs = {}
    bot.lock = threading.RLock()
    bot.api = FakeApi()
    return bot


class TelegramAudioBotTargetTests(unittest.TestCase):
    def setUp(self):
        original = make_bot

        def tracked_bot(*args, **kwargs):
            bot = original(*args, **kwargs)
            self.addCleanup(bot._test_tmp.cleanup)
            return bot

        patcher = patch(__name__ + ".make_bot", side_effect=tracked_bot)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loads_multiple_vehicle_maps_from_env(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            piteravto = tmp / "piteravto.csv"
            pat = tmp / "pat.csv"
            write_map(piteravto, [("6217", "172.26.129.119")])
            write_map(pat, [("6217", "172.27.133.20")])

            with patch.dict(
                os.environ,
                {"VEHICLE_MAP_PATHS": f"piteravto={piteravto},pat={pat}"},
                clear=False,
            ):
                bot = AudioBot.__new__(AudioBot)
                bot.vehicle_map_path = piteravto
                maps = bot._load_vehicle_maps()

        self.assertEqual(maps["piteravto"]["6217"], "172.26.129.119")
        self.assertEqual(maps["pat"]["6217"], "172.27.133.20")

    def test_explicit_pat_target_uses_pat_address_for_overlapping_number(self) -> None:
        bot = make_bot(
            {
                "piteravto": {"6217": "172.26.129.119"},
                "pat": {"6217": "172.27.133.20"},
            },
        )

        target = bot._parse_target(100, ["pat", "6217", "15", "salon"])

        self.assertEqual(target.branch, "pat")
        self.assertEqual(target.number, "6217")
        self.assertEqual(target.ip, "172.27.133.20")
        self.assertEqual(target.volume, 15)
        self.assertEqual(target.output, "salon")

    def test_ambiguous_number_requires_branch_when_user_has_no_branch(self) -> None:
        bot = make_bot(
            {
                "piteravto": {"6217": "172.26.129.119"},
                "pat": {"6217": "172.27.133.20"},
            },
        )

        with self.assertRaises(ValueError) as raised:
            bot._parse_target(100, ["6217"])

        self.assertIn("есть в нескольких филиалах", str(raised.exception))

    def test_selected_branch_resolves_number_without_repeating_branch(self) -> None:
        bot = make_bot(
            {
                "piteravto": {"6217": "172.26.129.119"},
                "pat": {"6217": "172.27.133.20"},
            },
        )
        bot.user_branches[100] = "pat"

        target = bot._parse_target(100, ["6217"])

        self.assertEqual(target.branch, "pat")
        self.assertEqual(target.ip, "172.27.133.20")

    def test_legacy_single_map_mode_keeps_piteravto_branch(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "vehicles.csv"
            write_map(path, [("10192", "172.26.128.16")])
            with patch.dict(os.environ, {"VEHICLE_MAP_PATH": str(path)}, clear=False):
                bot = AudioBot.__new__(AudioBot)
                bot.vehicle_map_path = path
                maps = bot._load_vehicle_maps()

        self.assertEqual(maps, {"piteravto": {"10192": "172.26.128.16"}})

    def test_branch_button_clears_pending_target_input(self) -> None:
        bot = make_bot(
            {
                "piteravto": {"6217": "172.26.129.119"},
                "pat": {"6217": "172.27.133.20"},
            },
        )

        bot.handle_command(100, 200, "/target")
        bot.handle_command(100, 200, "/branch")
        bot.handle_message({"from": {"id": 100}, "chat": {"id": 200}, "text": "pat"})

        self.assertIn(100, bot.awaiting_target)
        self.assertNotIn(100, bot.awaiting_branch)
        self.assertEqual(bot.user_branches[100], "pat")
        self.assertIn("Филиал сохранён: pat", bot.api.messages[-1][1])

    def test_stop_clears_pending_input_modes(self) -> None:
        bot = make_bot({"piteravto": {"10192": "172.26.128.16"}})
        bot.awaiting_target.add(100)
        bot.awaiting_branch.add(100)

        bot.handle_command(100, 200, "/stop")

        self.assertNotIn(100, bot.awaiting_target)
        self.assertNotIn(100, bot.awaiting_branch)
        self.assertIn("Активного воспроизведения нет", bot.api.messages[-1][1])


class TelegramAudioControlsTests(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot({"pat": {"1001": "172.26.0.1"}, "piteravto": {"1001": "172.27.0.1"}})
        self.addCleanup(self.bot._test_tmp.cleanup)

    def click(self, action, user_id=100, owner=100):
        self.bot.handle_callback({"id": "cb", "from": {"id": user_id},
                                  "data": f"audio:{owner}:{action}",
                                  "message": {"message_id": 20, "chat": {"id": 200}}})

    def message(self, text):
        self.bot.handle_message({"from": {"id": 100}, "chat": {"id": 200}, "text": text})

    def test_branch_buttons_then_number_default_salon(self):
        self.message("/branch")
        buttons = self.bot.api.markups[-1]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in buttons], ["ПАТ", "Питеравто"])
        self.click("branch=pat")
        self.message("missing")
        self.assertIn(100, self.bot.awaiting_target)
        self.message("1001")
        self.assertEqual(self.bot.targets[100].ip, "172.26.0.1")
        self.assertEqual(self.bot.targets[100].output, "salon")
        self.assertNotIn(100, self.bot.awaiting_target)
        self.assertIsNotNone(self.bot.api.markups[-1])

    def test_callbacks_require_authorized_owner(self):
        self.click("branch=pat", user_id=999)
        self.click("branch=pat", owner=999)
        self.assertEqual(self.bot.user_branches, {})
        self.assertEqual(self.bot.api.calls[-1][0], "answerCallbackQuery")

    def test_channel_volume_preserve_running_target(self):
        self.message("/target pat 1001")
        running_target = self.bot.targets[100]
        self.click("channel=street")
        self.click("quieter")
        self.assertEqual(running_target.output, "salon")
        self.assertEqual(running_target.volume, 25)
        self.assertEqual(self.bot.targets[100].output, "street")
        self.assertEqual(self.bot.targets[100].volume, 20)
        self.assertEqual(self.bot.api.calls[-1][0], "editMessageText")
        self.click("channel=bad")
        self.assertEqual(self.bot.targets[100].output, "street")

    def test_volume_input_validation_and_stop(self):
        self.click("volume")
        self.message("101")
        self.assertIn(100, self.bot.awaiting_volume)
        self.message("15")
        self.assertEqual(self.bot.user_audio[100]["volume"], 15)
        self.assertNotIn(100, self.bot.awaiting_volume)
        self.message("/volume 1")
        self.click("quieter")
        self.assertEqual(self.bot.user_audio[100]["volume"], 1)
        self.message("/volume 100")
        self.click("louder")
        self.assertEqual(self.bot.user_audio[100]["volume"], 100)
        self.click("volume")
        self.click("stop")
        self.assertNotIn(100, self.bot.awaiting_volume)

    def test_switch_branch_clears_target_retains_audio(self):
        self.message("/target pat 1001 15 tangenta")
        self.click("branch=piteravto")
        self.assertNotIn(100, self.bot.targets)
        self.message("1001")
        self.assertEqual(self.bot.targets[100].ip, "172.27.0.1")
        self.assertEqual(self.bot.targets[100].output, "tangenta")
        self.assertEqual(self.bot.targets[100].volume, 15)

    def test_settings_reload_resolves_current_inventory_ip(self):
        self.message("/target pat 1001 15 street")
        self.bot.targets.clear()
        self.bot.user_branches.clear()
        self.bot.user_audio.clear()
        self.bot.vehicle_maps["pat"]["1001"] = "172.26.0.2"
        self.bot._load_settings()
        self.assertEqual(self.bot.targets[100].ip, "172.26.0.2")
        self.assertEqual(self.bot.targets[100].volume, 15)
        self.assertEqual(self.bot.targets[100].output, "street")
        self.assertNotIn("172.26.", self.bot.settings_path.read_text())
        self.assertEqual(self.bot.settings_path.stat().st_mode & 0o777, 0o600)

    def test_bad_settings_do_not_break_startup(self):
        self.bot.settings_path.write_text('{broken')
        self.bot._load_settings()
        self.assertEqual(self.bot.targets, {})
        self.bot.settings_path.write_text(json.dumps({"100": {"volume": 999}, "999": {"volume": 10}}))
        self.bot._load_settings()
        self.assertEqual(self.bot.user_audio, {})

    def test_media_types_and_unknown_documents(self):
        for suffix in ["mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"]:
            for kind in ["audio", "document"]:
                with self.subTest(suffix=suffix, kind=kind):
                    self.assertEqual(AudioBot._media({kind: {"file_id": "f", "file_name": f"track.{suffix}"}}), ("f", f".{suffix}"))
        self.assertEqual(AudioBot._media({"audio": {"file_id": "f"}}), ("f", ".media"))
        self.assertEqual(AudioBot._media({"voice": {"file_id": "f"}}), ("f", ".ogg"))
        self.assertIsNone(AudioBot._media({"document": {"file_id": "f", "file_name": "x.pdf"}}))

    def test_large_media_rejected_before_job(self):
        self.bot.handle_message({"from": {"id": 100}, "chat": {"id": 200},
                                 "audio": {"file_id": "f", "file_size": 30 * 1024 * 1024}})
        self.assertFalse(self.bot.jobs)
        self.assertIn("слишком большой", self.bot.api.messages[-1][1])

    def test_conversion_failure_and_timeout(self):
        job = PlaybackJob(chat_id=200)
        with patch("bo_vpn_agent.telegram_audio_bot.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "Не удалось прочитать"):
                AudioBot._prepare_audio(Path("in.wav"), Path("out.mp3"), job)
            self.assertIsNone(job.process)
            popen.return_value.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 120), 0]
            with self.assertRaisesRegex(RuntimeError, "слишком много времени"):
                AudioBot._prepare_audio(Path("in.wav"), Path("out.mp3"), job)
            popen.return_value.kill.assert_called_once()

    def test_invalid_audio_never_contacts_worker_and_cleans_files(self):
        self.message("/target pat 1001")
        self.bot.temp_dir = Path(self.bot._test_tmp.name)
        self.bot.api.download = lambda file_id, path: path.write_bytes(b"broken")
        self.bot._ensure_vehicle_ready = Mock()
        job = PlaybackJob(chat_id=200)
        self.bot.jobs[100] = job
        with patch.object(self.bot, "_prepare_audio", side_effect=RuntimeError("bad audio")), self.assertLogs("telegram-audio-bot", level="ERROR"):
            self.bot._play(100, "file", ".wav", self.bot.targets[100], job)
        self.bot._ensure_vehicle_ready.assert_not_called()
        self.assertFalse(self.bot.jobs)
        self.assertEqual(list(self.bot.temp_dir.glob("*.wav")), [])

    def test_polling_includes_callbacks(self):
        api = TelegramApi("synthetic-token")
        with patch.object(api, "call", return_value=[]) as call:
            api.get_updates(50)
        self.assertEqual(json.loads(call.call_args.args[1]["allowed_updates"]), ["message", "callback_query"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg integration runs in bot image")
    def test_real_audio_conversion_for_supported_formats(self):
        codecs = {"mp3": "libmp3lame", "m4a": "aac", "aac": "aac", "ogg": "libvorbis",
                  "opus": "libopus", "wav": "pcm_s16le", "flac": "flac"}
        with TemporaryDirectory() as directory:
            for suffix, codec in codecs.items():
                with self.subTest(suffix=suffix):
                    source = Path(directory) / f"source.{suffix}"
                    target = Path(directory) / "prepared.mp3"
                    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                                    "anullsrc=r=48000:cl=stereo", "-t", "0.1", "-c:a", codec, str(source)],
                                   check=True, timeout=15, capture_output=True)
                    AudioBot._prepare_audio(source, target, PlaybackJob(chat_id=200))
                    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(target)],
                                            check=True, capture_output=True, text=True, timeout=10)
                    stream = json.loads(result.stdout)["streams"][0]
                    self.assertEqual(stream["codec_name"], "mp3")
                    self.assertEqual(stream["sample_rate"], "48000")
                    self.assertEqual(stream["channels"], 2)
            invalid = Path(directory) / "invalid.mp3"
            invalid.write_bytes(b"not an audio file")
            with self.assertRaisesRegex(RuntimeError, "Не удалось прочитать"):
                AudioBot._prepare_audio(invalid, Path(directory) / "invalid-out.mp3", PlaybackJob(chat_id=200))


if __name__ == "__main__":
    unittest.main()

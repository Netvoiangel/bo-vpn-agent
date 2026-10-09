# Telegram audio bot

Open `/start` to show the inline control panel:

1. Choose **PAT** or **Piteravto** with the branch buttons.
2. Press **Choose vehicle** and enter its garage number. After choosing a branch,
   the bot asks for the number immediately. Overlapping numbers are resolved only
   within the selected branch.
3. The default channel is **Salon**. The channel button also offers **Street** and
   **Tangenta**. The names correspond to `salon`, `street`, `tangenta` in the player.
4. Adjust volume with the minus/plus buttons, or press the percentage and enter
   an integer from 1 to 100. The initial volume is 25%.
5. Send or forward a Telegram music message, voice message, or audio document.
   Use **Stop** to cancel preparation or stop playback.

Settings apply to the next audio file; changing them does not change an already
running playback. Each user has separate settings. Buttons on another user's
panel cannot control their session. `/status` opens a fresh panel. Existing text
commands and reply-keyboard buttons remain supported:

```text
/branch pat
/target VEHICLE_NUMBER
/target pat VEHICLE_NUMBER 15 salon
/volume 15
/channel
/status
/stop
```

## Audio formats

Supported inputs include MP3, M4A/AAC, OGG/Opus, WAV, FLAC and Telegram voice
messages. Telegram music messages without filenames are accepted too. The bot
decodes the audio and prepares a stereo 48 kHz MP3 locally before contacting the
worker or uploading anything to a vehicle. Invalid or unsupported input is
rejected before the vehicle check. Preparation is limited to 120 seconds and
can be cancelled with **Stop**. Both downloaded and prepared files are removed
after completion, cancellation or failure.

The input limit is 20 MB for the hosted Telegram Bot API
([getFile documentation](https://core.telegram.org/bots/api#getfile)). Links to
streaming services are not audio files and are not downloaded.

## Saved settings and deployment

The bot stores each user's branch, volume, channel and selected vehicle number
in `/data/audio-settings.json` (`AUDIO_SETTINGS_PATH` overrides the location).
The full-compose bot already mounts `/data` persistently. Settings are written
atomically with owner-only permissions. IP addresses are not persisted: after a
restart the selected vehicle is resolved against the current branch inventory.
If it was removed from inventory, a new vehicle must be selected.

Only the bot image needs rebuilding for these changes. The image includes
FFmpeg for input validation and conversion:

```bash
docker compose -f docker-compose.full.yml --profile audio build audio-bot
docker compose -f docker-compose.full.yml --profile audio up -d --no-deps audio-bot
```

These commands do not restart worker, runner, TCP proxy or UniVPN. Tests use
mock Telegram/worker APIs and synthetic media; they do not play audio on vehicles.

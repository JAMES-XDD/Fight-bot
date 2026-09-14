# Telegram VC Audio Bot — Render Ready

This version is prepared for a **Render Background Worker** and uses a Render Persistent Disk for the Telegram session and downloaded audio.

## Render architecture

```text
Render
└── Background Worker
    ├── Telegram Bot account (commands/buttons)
    ├── Telegram User account (VC playback)
    ├── Telethon
    ├── PyTgCalls
    ├── FFmpeg
    └── /var/data  <-- persistent disk
         ├── sessions/
         └── downloads/
```

Render background workers run continuously and do not expose an incoming URL. A persistent disk is therefore used for the Telethon `.session` and local audio files.

## Deploy

1. Put this project in a GitHub repository.
2. In Render, choose **New → Blueprint** and select the repository.
3. Render will read `render.yaml`.
4. Add the secret environment variables requested by the Blueprint:
   - `API_ID`
   - `API_HASH`
   - `BOT_TOKEN`
   - `USER_PHONE`
   - `ADMIN_IDS`
5. Deploy.

The Blueprint creates a Background Worker with a 1 GB persistent disk mounted at `/var/data`.

### Important: first Telegram login

The first user-account login is interactive because Telegram may ask for the login code and 2FA password.

If your first deployment cannot complete login non-interactively, use Render's service Shell and run:

```bash
python main.py
```

Complete the Telegram login when prompted. The resulting session is stored under:

```text
/var/data/sessions/user.session
```

After that, normal worker restarts use the stored session.

**Never commit or share this `.session` file.**

## Environment variables

```env
API_ID=...
API_HASH=...
BOT_TOKEN=...
USER_PHONE=+91...
ADMIN_IDS=123456789

AUDIO_VOLUME=5000
AUDIO_GAIN=500
KEEP_FILES=false

DATA_DIR=/var/data

MONITOR_URL=
MONITOR_TOKEN=
HEARTBEAT_SECONDS=30
```

`ADMIN_IDS` is recommended. If it is empty, commands are not restricted by user ID.

## Commands

Reply to an audio/document:

```text
.play
```

Play in another chat:

```text
.play -1001234567890
```

Controls:

```text
.stop
.pause
.resume
.skip
.loop
.loop on
.loop off
.volume 100
.id
.help
```

While a track is playing, the status message contains:

- `🔁 Loop: ON/OFF`
- `⏸ Pause`
- `▶️ Resume`
- `⏭ Skip`
- `⏹ Stop`

Pressing Loop toggles the state immediately.

## Volume and gain

The requested values are retained:

```env
AUDIO_VOLUME=5000
AUDIO_GAIN=500
```

These are interpreted as FFmpeg processing multipliers:

- 5000 = 50×
- 500 = 5×

That is extremely aggressive and can produce clipping. An FFmpeg limiter is applied after the gain. For normal audio, start with:

```env
AUDIO_VOLUME=100
AUDIO_GAIN=100
```

The separate `.volume N` command controls the Telegram voice-chat participant volume and is clamped to `0..200`.

## Monitoring

The worker can optionally send a heartbeat to a monitoring service:

```env
MONITOR_URL=https://your-monitor.example.com/api/heartbeat
MONITOR_TOKEN=your-secret
HEARTBEAT_SECONDS=30
```

If `MONITOR_URL` is blank, heartbeat requests are disabled.

The heartbeat includes only operational state such as:

- online status
- account ID
- active voice chats
- current tracks
- queue lengths
- loop state
- uptime

Do not put Telegram OTPs, API hashes, bot tokens, or session data into heartbeat payloads.

## Render persistent disk

The Blueprint mounts:

```text
/var/data
```

The application stores persistent data there:

```text
/var/data/sessions
/var/data/downloads
```

Only files under the attached disk survive Render restarts/deploys. Render documents that the normal filesystem is ephemeral.

## Render plan

The Blueprint uses:

```yaml
plan: starter
```

You can change the worker plan in `render.yaml` or in the Render Dashboard.

A persistent disk is required for the Telegram user session. The disk is attached to the Background Worker, not the monitoring web service.

## Local testing

```bash
cp .env.example .env
docker compose build
docker compose run --rm telegram-vc-audio-bot python main.py
```

Or without Docker:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Security

Never commit:

- `.env`
- `sessions/*.session`
- Telegram OTPs
- Telegram 2FA passwords
- Bot tokens

If a bot token or user session is exposed, revoke/rotate the affected credentials immediately.

## Notes

A Render Background Worker does not expose an HTTP endpoint. It is the right Render service type for this continuously running process, while a separate Web Service can be used for a dashboard/monitor.

A monitor that polls an endpoint cannot make a crashed worker stay alive by itself; Render should handle worker restarts. The monitor is for visibility and alerting.

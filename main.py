import asyncio
import logging
import os
import time
from pathlib import Path

from aiohttp import ClientSession
from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from pytgcalls import PyTgCalls
from pytgcalls import filters as tgcall_filters
from pytgcalls.types import StreamEnded

from player import PlayerManager

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR))).resolve()
DOWNLOAD_DIR = DATA_DIR / "downloads"
SESSION_DIR = DATA_DIR / "sessions"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
SESSION_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("telegram-vc-player")

def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

API_ID = int(required("API_ID"))
API_HASH = required("API_HASH")
BOT_TOKEN = required("BOT_TOKEN")
PHONE = os.getenv("USER_PHONE", "").strip() or None
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
KEEP_FILES = os.getenv("KEEP_FILES", "false").lower() in {"1", "true", "yes", "on"}
MONITOR_URL = os.getenv("MONITOR_URL", "").strip()
MONITOR_TOKEN = os.getenv("MONITOR_TOKEN", "").strip()
HEARTBEAT_SECONDS = max(10, int(os.getenv("HEARTBEAT_SECONDS", "30")))

bot = TelegramClient(str(SESSION_DIR / "bot"), API_ID, API_HASH)
user = TelegramClient(str(SESSION_DIR / "user"), API_ID, API_HASH)
calls = PyTgCalls(user)
player = PlayerManager(
    bot, calls, DOWNLOAD_DIR,
    int(os.getenv("AUDIO_VOLUME", "5000")),
    int(os.getenv("AUDIO_GAIN", "500")),
    KEEP_FILES,
)
started_at = time.time()

def allowed(event):
    return not ADMIN_IDS or (event.sender_id in ADMIN_IDS if event.sender_id else False)

def controls(chat_id, loop_enabled):
    return [
        [
            Button.inline(f"🔁 Loop: {'ON' if loop_enabled else 'OFF'}",
                          data=f"loop:{chat_id}".encode()),
            Button.inline("⏸ Pause", data=f"pause:{chat_id}".encode()),
        ],
        [
            Button.inline("▶️ Resume", data=f"resume:{chat_id}".encode()),
            Button.inline("⏭ Skip", data=f"skip:{chat_id}".encode()),
        ],
        [Button.inline("⏹ Stop", data=f"stop:{chat_id}".encode())],
    ]

async def heartbeat():
    if not MONITOR_URL:
        return
    while True:
        try:
            active = []
            for chat_id, state in player.states.items():
                if state.current:
                    active.append({
                        "chat_id": chat_id,
                        "track": state.current.title,
                        "queue": len(state.queue),
                        "loop": state.loop,
                    })
            payload = {
                "status": "online",
                "uptime": int(time.time() - started_at),
                "account_id": getattr(getattr(player, "user_me", None), "id", None),
                "active": active,
            }
            headers = {"Content-Type": "application/json"}
            if MONITOR_TOKEN:
                headers["Authorization"] = f"Bearer {MONITOR_TOKEN}"
            async with ClientSession() as session:
                async with session.post(
                    MONITOR_URL, json=payload, headers=headers, timeout=10
                ) as resp:
                    if resp.status >= 400:
                        log.warning("Monitor heartbeat returned HTTP %s", resp.status)
        except Exception:
            log.exception("Heartbeat failed")
        await asyncio.sleep(HEARTBEAT_SECONDS)

@bot.on(events.NewMessage(pattern=r"(?i)^[./]help$"))
async def help_handler(event):
    if not allowed(event): return
    await event.reply(
        "**Telegram VC Audio Player**\n\n"
        "Reply to audio/document with `.play`.\n"
        "Optional target: `.play -1001234567890`\n\n"
        "`.stop` `.pause` `.resume` `.skip`\n"
        "`.loop` `.loop on` `.loop off`\n"
        "`.volume 100` `.id`",
        parse_mode="md",
    )

@bot.on(events.NewMessage(pattern=r"(?i)^[./]id$"))
async def id_handler(event):
    if allowed(event):
        await event.reply(f"`{event.chat_id}`", parse_mode="md")

@bot.on(events.NewMessage(pattern=r"(?i)^[./]play(?:\s+(-?\d+))?\s*$"))
async def play_handler(event):
    if not allowed(event): return
    reply = await event.get_reply_message()
    if not reply or not reply.media:
        await event.reply("Reply to an audio/document with `.play`.")
        return
    target = event.pattern_match.group(1)
    chat_id = int(target) if target else event.chat_id
    await event.reply("⬇️ Downloading audio…")
    try:
        await player.enqueue(reply, chat_id, event.sender_id)
    except Exception as exc:
        log.exception("Play failed")
        await event.reply(f"❌ `{type(exc).__name__}: {exc}`", parse_mode="md")

@bot.on(events.NewMessage(pattern=r"(?i)^[./]stop$"))
async def stop_handler(event):
    if allowed(event):
        await event.reply("⏹ Stopped." if await player.stop(event.chat_id) else "Nothing is playing.")

@bot.on(events.NewMessage(pattern=r"(?i)^[./]pause$"))
async def pause_handler(event):
    if allowed(event):
        await event.reply("⏸ Paused." if await player.pause(event.chat_id) else "Nothing is playing.")

@bot.on(events.NewMessage(pattern=r"(?i)^[./]resume$"))
async def resume_handler(event):
    if allowed(event):
        await event.reply("▶️ Resumed." if await player.resume(event.chat_id) else "Nothing is paused.")

@bot.on(events.NewMessage(pattern=r"(?i)^[./]skip$"))
async def skip_handler(event):
    if allowed(event):
        await event.reply("⏭ Skipped." if await player.skip(event.chat_id) else "Nothing is playing.")

@bot.on(events.NewMessage(pattern=r"(?i)^[./]loop(?:\s+(on|off))?$"))
async def loop_handler(event):
    if not allowed(event): return
    value = event.pattern_match.group(1)
    enabled = await player.set_loop(
        event.chat_id, None if value is None else value.lower() == "on"
    )
    await event.reply(f"🔁 Loop {'ON' if enabled else 'OFF'}.")

@bot.on(events.NewMessage(pattern=r"(?i)^[./]volume\s+(\d+)$"))
async def volume_handler(event):
    if not allowed(event): return
    volume = max(0, min(int(event.pattern_match.group(1)), 200))
    try:
        await calls.change_volume_call(event.chat_id, volume)
        await event.reply(f"🔊 VC volume set to `{volume}`.", parse_mode="md")
    except Exception as exc:
        await event.reply(f"❌ `{type(exc).__name__}: {exc}`", parse_mode="md")

@bot.on(events.CallbackQuery)
async def button_handler(event):
    if not (not ADMIN_IDS or event.sender_id in ADMIN_IDS):
        await event.answer("Not authorized.", alert=True); return
    try:
        action, raw = event.data.decode().split(":", 1)
        chat_id = int(raw)
        if action == "loop":
            enabled = await player.set_loop(chat_id, None)
            await event.answer(f"Loop {'ON' if enabled else 'OFF'}")
        elif action == "pause":
            await player.pause(chat_id); await event.answer("Paused.")
        elif action == "resume":
            await player.resume(chat_id); await event.answer("Resumed.")
        elif action == "skip":
            await player.skip(chat_id); await event.answer("Skipped.")
        elif action == "stop":
            await player.stop(chat_id); await event.answer("Stopped.")
        state = player.states.get(chat_id)
        if state and chat_id in player.status_message_ids:
            await bot.edit_message(
                chat_id, player.status_message_ids[chat_id],
                player.status_text(chat_id),
                buttons=controls(chat_id, state.loop),
            )
    except Exception as exc:
        log.exception("Button action failed")
        await event.answer(f"Error: {type(exc).__name__}", alert=True)

@calls.on_update(tgcall_filters.stream_end())
async def stream_end_handler(_, update: StreamEnded):
    await player.on_stream_end(update.chat_id)

async def main():
    log.info("Starting control bot…")
    await bot.start(bot_token=BOT_TOKEN)

    log.info("Starting Telegram user account…")
    if PHONE:
        await user.start(phone=PHONE)
    else:
        await user.start()

    player.user_me = await user.get_me()
    log.info("Logged in as %s (%s)", player.user_me.first_name, player.user_me.id)

    await calls.start()
    tasks = [bot.run_until_disconnected(), user.run_until_disconnected()]
    if MONITOR_URL:
        tasks.append(heartbeat())
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

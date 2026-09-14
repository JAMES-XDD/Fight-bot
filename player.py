import asyncio
import logging
import math
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from telethon import TelegramClient
from pytgcalls import PyTgCalls

log = logging.getLogger("telegram-vc-player.player")

@dataclass
class QueueItem:
    path: Path
    title: str
    source_message_id: int
    requested_by: int | None = None

@dataclass
class ChatState:
    queue: deque = field(default_factory=deque)
    current: QueueItem | None = None
    loop: bool = False
    ending: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

class PlayerManager:
    def __init__(self, bot: TelegramClient, calls: PyTgCalls, download_dir: Path,
                 volume_value: int, gain_value: int, keep_files: bool):
        self.bot = bot
        self.calls = calls
        self.download_dir = download_dir
        self.volume_value = volume_value
        self.gain_value = gain_value
        self.keep_files = keep_files
        self.states = {}
        self.status_message_ids = {}
        self.user_me = None

    def state(self, chat_id):
        return self.states.setdefault(chat_id, ChatState())

    def status_text(self, chat_id):
        state = self.states.get(chat_id)
        if not state or not state.current:
            return "🎵 Nothing is playing."
        return (
            f"🎵 **Playing:** {state.current.title}\n"
            f"Mode: {'🔁 Loop ON' if state.loop else '➡️ Queue'}\n"
            f"Queue: {len(state.queue)}"
        )

    async def _process_audio(self, input_path):
        output = input_path.with_name(f"{input_path.stem}.processed.m4a")
        vm = max(0.0, self.volume_value / 100.0)
        gm = max(0.0, self.gain_value / 100.0)
        if vm == 0 or gm == 0:
            chain = "volume=0"
        else:
            db = 20.0 * math.log10(vm * gm)
            chain = f"volume={db}dB,alimiter=limit=0.95"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(input_path), "-vn", "-af", chain,
            "-c:a", "aac", "-b:a", "192k", str(output),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError("FFmpeg: " + err.decode(errors="replace").strip())
        return output

    async def enqueue(self, source_message, target_chat_id, requested_by):
        state = self.state(target_chat_id)
        token = uuid.uuid4().hex[:10]
        name = self._safe_name(source_message)
        raw = self.download_dir / f"{token}_{name}"
        downloaded = await source_message.download_media(file=str(raw))
        if not downloaded:
            raise RuntimeError("Telegram returned no downloaded file.")
        raw = Path(downloaded)
        processed = await self._process_audio(raw)
        if processed != raw:
            raw.unlink(missing_ok=True)
        item = QueueItem(processed, name, source_message.id, requested_by)

        async with state.lock:
            if state.current is None:
                state.current = item
                start = True
            else:
                state.queue.append(item)
                start = False

        if start:
            await self._play_current(target_chat_id)
        else:
            await self._status(target_chat_id, f"➕ Added: **{item.title}**\nQueue: {len(state.queue)}")

    def _safe_name(self, message):
        name = None
        if getattr(message, "file", None):
            name = message.file.name or message.file.title
        name = name or f"audio_{message.id}.bin"
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
        return (name[:120] or f"audio_{message.id}.bin")

    async def _play_current(self, chat_id):
        state = self.state(chat_id)
        if not state.current:
            return
        await self.calls.play(chat_id, str(state.current.path))
        await self._status(chat_id, self.status_text(chat_id))

    async def on_stream_end(self, chat_id):
        state = self.states.get(chat_id)
        if not state:
            return
        async with state.lock:
            if state.ending:
                return
            state.ending = True
            try:
                finished = state.current
                if finished is None:
                    return
                if state.loop:
                    nxt = finished
                elif state.queue:
                    nxt = state.queue.popleft()
                else:
                    nxt = None
                if not state.loop:
                    state.current = nxt
                # In loop mode, keep the same item as current.
                else:
                    state.current = finished
            finally:
                state.ending = False

        if not state.loop and finished:
            self._cleanup(finished)

        if state.current:
            try:
                await self._play_current(chat_id)
            except Exception:
                log.exception("Failed to start next track")
                await self._status(chat_id, "❌ Could not start next track.")
        else:
            await self._status(chat_id, "✅ Queue finished.")

    def _cleanup(self, item):
        if self.keep_files:
            return
        try:
            item.path.unlink(missing_ok=True)
        except Exception:
            log.exception("Could not delete %s", item.path)

    async def stop(self, chat_id):
        state = self.states.get(chat_id)
        if not state or (not state.current and not state.queue):
            return False
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            pass
        async with state.lock:
            current, queued = state.current, list(state.queue)
            state.current = None
            state.queue.clear()
        if current: self._cleanup(current)
        for item in queued: self._cleanup(item)
        await self._status(chat_id, "⏹ Stopped.")
        return True

    async def pause(self, chat_id):
        state = self.states.get(chat_id)
        if not state or not state.current: return False
        await self.calls.pause(chat_id)
        return True

    async def resume(self, chat_id):
        state = self.states.get(chat_id)
        if not state or not state.current: return False
        await self.calls.resume(chat_id)
        return True

    async def skip(self, chat_id):
        state = self.states.get(chat_id)
        if not state or not state.current: return False
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            pass
        # Advance immediately; leave_call may or may not emit stream_end.
        await self.on_stream_end(chat_id)
        return True

    async def set_loop(self, chat_id, value):
        state = self.state(chat_id)
        async with state.lock:
            state.loop = (not state.loop) if value is None else value
        await self._status(chat_id, self.status_text(chat_id))
        return state.loop

    async def _status(self, chat_id, text):
        try:
            from main import controls
            state = self.state(chat_id)
            buttons = controls(chat_id, state.loop)
            status_id = self.status_message_ids.get(chat_id)
            if status_id:
                try:
                    await self.bot.edit_message(chat_id, status_id, text, buttons=buttons)
                    return
                except Exception:
                    self.status_message_ids.pop(chat_id, None)
            msg = await self.bot.send_message(chat_id, text, buttons=buttons)
            self.status_message_ids[chat_id] = msg.id
        except Exception:
            log.exception("Status update failed")

"""Telegram: a short status, the panel link, important warnings, and a state backup.

Trade-by-trade detail lives on the web panel so the chat stays readable. Commands match exactly, so
an unrelated message cannot trigger a report. Only the owner's chat is served. Queued messages
older than two minutes are ignored, so an old /sifirla cannot fire after a restart.
"""

import asyncio
import gzip
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

from . import reports, settings

logger = logging.getLogger("tradebot.telegram")

STALE_MESSAGE_SECONDS = 120
BACKUP_FILENAME = "bot_durum_yedegi.json.gz"
BACKUP_CAPTION = "Bot durum yedeği. Lütfen silmeyin."
RESTORE_ATTEMPTS = 3
RESTORE_RETRY_SECONDS = 5.0

COMMANDS = {
    "/start": "help", "/help": "help", "yardım": "help",
    "/durum": "status", "durum": "status", "📊 durum": "status",
    "/panel": "panel", "panel": "panel", "🌐 panel": "panel",
    "/sifirla": "reset_ask", "sıfırla": "reset_ask",
    "/sifirla onay": "reset_do",
    # These moved to the panel. Old habits get a pointer instead of "unknown command".
    "/pozisyon": "panel", "/islemler": "panel", "/masa": "panel", "/haber": "panel",
    "/arbitraj": "panel", "/maliyet": "panel", "/test": "panel", "/kanit": "panel",
}

KEYBOARD = {
    "keyboard": [[{"text": "📊 Durum"}, {"text": "🌐 Panel"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}


def normalize_command(text: str) -> str:
    """Lowercases by Turkish rules. Plain lower() turns "İ" into two characters and breaks matching."""
    return text.strip().replace("İ", "i").replace("I", "ı").lower()


class Telegram:
    def __init__(self, session: aiohttp.ClientSession, bot=None, token: str = settings.TELEGRAM_BOT_TOKEN,
                 chat_id: str = settings.TELEGRAM_CHAT_ID):
        self.session = session
        self.bot = bot
        self.token = token
        self.chat_id = str(chat_id)
        self.offset = 0
        self.backup_message_id: Optional[int] = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def _api(self, method: str, timeout: float = 15.0, **fields) -> Optional[dict]:
        try:
            async with self.session.post(self._url(method), json=fields,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.warning("Telegram %s failed: %s", method, type(e).__name__)
            return None
        if not data.get("ok"):
            logger.warning("Telegram %s rejected: %s", method, str(data.get("description"))[:120])
            return None
        return data.get("result")

    async def send(self, text: str, keyboard: bool = False) -> None:
        if not self.enabled:
            return
        for chunk in reports.split_message(text):
            fields: Dict[str, Any] = {"chat_id": self.chat_id, "text": chunk, "disable_web_page_preview": True}
            if keyboard:
                fields["reply_markup"] = KEYBOARD
            await self._api("sendMessage", **fields)

    # ---------------------------------------------------------------- commands

    def build_reply(self, text: str) -> str:
        command = COMMANDS.get(normalize_command(text))
        bot = self.bot
        if command == "help":
            return reports.format_help()
        if command == "status":
            return reports.format_status(bot)
        if command == "panel":
            return reports.panel_line()
        if command == "reset_ask":
            return ("⚠️ Bu komut tüm sahte kasaları ve işlem geçmişini siler. Yapay zeka harcama "
                    "kaydı silinmez. Onay için yazın: /sifirla onay")
        if command == "reset_do":
            bot.reset_paper()
            return "🔄 Sahte kasalar ve ölçümler sıfırlandı."
        return "Bu komutu tanımıyorum. Komutlar için /start yazın."

    async def handle_update(self, update: Dict[str, Any], now: Optional[float] = None) -> Optional[str]:
        msg = update.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = msg.get("text")
        if not chat_id or not text:
            return None
        if str(chat_id) != self.chat_id:
            logger.warning("Rejected Telegram chat %s", chat_id)
            return None
        now = now if now is not None else time.time()
        if now - msg.get("date", now) > STALE_MESSAGE_SECONDS:
            return None
        try:
            reply = self.build_reply(text)
        except Exception as e:  # The reply must never be partial or wrong because of a bug.
            logger.exception("Telegram command failed: %r", text)
            reply = f"⚠️ Komut çalıştırılamadı ({type(e).__name__}). Yanlış bilgi vermemek için sonuç gösterilmedi."
        await self.send(reply, keyboard=True)
        return reply

    async def poll_forever(self) -> None:
        if not self.enabled:
            logger.warning("Telegram token or chat id missing: commands are off")
            return
        while True:
            result = await self._get_updates()
            if result is None:
                await asyncio.sleep(3.0)
                continue
            for update in result:
                self.offset = update["update_id"]
                await self.handle_update(update)

    async def _get_updates(self) -> Optional[List[dict]]:
        try:
            async with self.session.post(self._url("getUpdates"), json={"offset": self.offset + 1, "timeout": 25},
                                         timeout=aiohttp.ClientTimeout(total=40)) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.warning("Telegram getUpdates failed: %s", type(e).__name__)
            return None
        if not data.get("ok"):
            logger.warning("Telegram getUpdates rejected: %s", str(data.get("description"))[:120])
            return None
        return data.get("result", [])

    # ---------------------------------------------------------------- state backup

    async def save_backup(self, state: dict) -> bool:
        """Keeps one pinned file in the chat, edited silently, so a redeploy loses nothing."""
        if not self.enabled:
            return False
        payload = gzip.compress(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        form = aiohttp.FormData()
        form.add_field("chat_id", self.chat_id)
        if self.backup_message_id:
            form.add_field("message_id", str(self.backup_message_id))
            form.add_field("media", json.dumps({"type": "document", "media": "attach://backup",
                                                "caption": BACKUP_CAPTION}))
            form.add_field("backup", payload, filename=BACKUP_FILENAME, content_type="application/gzip")
            method = "editMessageMedia"
        else:
            form.add_field("document", payload, filename=BACKUP_FILENAME, content_type="application/gzip")
            form.add_field("caption", BACKUP_CAPTION)
            form.add_field("disable_notification", "true")
            method = "sendDocument"
        try:
            async with self.session.post(self._url(method), data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.warning("Backup failed: %s", type(e).__name__)
            return False
        if not data.get("ok"):
            description = str(data.get("description", ""))
            if "message is not modified" in description:
                return True
            logger.warning("Backup rejected: %s", description[:120])
            # The pinned message may have been deleted. Next attempt creates a new one.
            self.backup_message_id = None
            return False
        if method == "sendDocument":
            self.backup_message_id = data["result"]["message_id"]
            await self._api("pinChatMessage", chat_id=self.chat_id, message_id=self.backup_message_id,
                            disable_notification=True)
        return True

    async def load_backup(self) -> Optional[dict]:
        """Reads the pinned backup file.

        A backup that exists but cannot be read is never overwritten: the message id stays unset,
        so the next save creates a new file and the old one stays in the chat.
        """
        if not self.enabled:
            return None
        self.backup_message_id = None
        for attempt in range(RESTORE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(RESTORE_RETRY_SECONDS)
            chat = await self._api("getChat", chat_id=self.chat_id)
            if chat is None:
                continue
            pinned = chat.get("pinned_message") or {}
            document = pinned.get("document") or {}
            if document.get("file_name") != BACKUP_FILENAME:
                return None
            state = await self._download(document["file_id"])
            if state is not None:
                self.backup_message_id = pinned.get("message_id")
                return state
        logger.error("Backup could not be restored; it will be kept and a new one started")
        return None

    async def _download(self, file_id: str) -> Optional[dict]:
        info = await self._api("getFile", file_id=file_id)
        if not info:
            return None
        url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                raw = await r.read()
            return json.loads(gzip.decompress(raw).decode("utf-8"))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            logger.warning("Backup download failed: %s", type(e).__name__)
            return None

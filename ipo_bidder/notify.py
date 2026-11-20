"""Telegram notifications."""
import logging

import requests

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.warning("telegram not configured; message:\n%s", text)
            return False
        ok = True
        for chunk in _chunks(text, 3900):
            try:
                r = requests.post(API.format(token=self.token, method="sendMessage"),
                                  json={"chat_id": self.chat_id, "text": chunk, "disable_web_page_preview": True},
                                  timeout=20)
                if r.status_code != 200:
                    log.error("telegram send failed: %s %s", r.status_code, r.text[:200])
                    ok = False
            except requests.RequestException as e:
                log.error("telegram send error: %s", e)
                ok = False
        return ok

    def get_chat_ids(self) -> list:
        """Chat ids of everyone who has messaged the bot (for first-time setup)."""
        r = requests.get(API.format(token=self.token, method="getUpdates"), timeout=20)
        r.raise_for_status()
        seen = {}
        for u in r.json().get("result", []):
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                seen[chat["id"]] = f"{chat.get('first_name', '')} {chat.get('last_name', '')} @{chat.get('username', '')}".strip()
        return [(k, v) for k, v in seen.items()]


def _chunks(text: str, n: int):
    while text:
        if len(text) <= n:
            yield text
            return
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        yield text[:cut]
        text = text[cut:].lstrip("\n")

"""Soulseek availability: pause downloads while slskd is logged out and tell the owner on Telegram.

On 2026-10-08 the VPN lost UDP for ~90 minutes and slskd was logged out of the server; every
queued Soulseek job failed within minutes. Jobs now wait while slskd is logged out or
unreachable. Anything else (a slow slskd, an uploader that does not answer) is not an
outage: it fails that one download and the next copy is tried.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger("acquisition")

CHECK_SECONDS = 30
ALERT_AFTER = 600


class SoulseekHealth:
    def __init__(self, connected, clock=time.time):
        # connected() is True when logged in, False when logged out or unreachable, None when
        # slskd is too busy to say: then the last answer stands.
        self._connected, self._clock = connected, clock
        self._lock = threading.Lock()
        self._checked_at, self._logged_in = 0.0, True
        self.down_since, self.alerted = None, False

    def refresh(self, force=False):
        now = self._clock()
        with self._lock:
            if not force and now - self._checked_at < CHECK_SECONDS:
                return self._logged_in
            self._checked_at = now
        logged_in = self._connected()
        with self._lock:
            if logged_in is not None:
                self._logged_in = bool(logged_in)
            return self._logged_in

    def ready(self):
        """Whether a Soulseek download may start now."""
        return self.refresh()

    def status(self):
        logged_in = self.refresh()
        return {"connected": logged_in, "logged_in": logged_in, "paused": not logged_in,
                "paused_until": None, "down_since": self.down_since}

    def watch(self, notify, waiting=lambda: 0):
        """One watchdog tick: track the outage and send one alert after ALERT_AFTER, one on recovery."""
        now = self._clock()
        healthy = self.refresh(force=True)
        if not healthy:
            if self.down_since is None:
                self.down_since = now
            elif not self.alerted and now - self.down_since >= ALERT_AFTER:
                minutes = round((now - self.down_since) / 60)
                self.alerted = notify(f"⚠️ Soulseek has been disconnected for {minutes} min. "
                                      f"{waiting()} downloads are waiting; they resume on their own when it reconnects.")
            return
        if self.down_since is not None and self.alerted:
            minutes = round((now - self.down_since) / 60)
            notify(f"✅ Soulseek reconnected after {minutes} min. Downloads are resuming.")
        self.down_since, self.alerted = None, False


def telegram(config, text):
    """Send text to the configured chat. Never logs the token (it is part of the URL)."""
    token, chat = config.get("telegram_bot_token"), config.get("telegram_chat_id")
    if not token or not chat:
        return False
    try:
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=20).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        log.warning("Telegram notification failed%s", f" (HTTP {status})" if status else "")
        return False

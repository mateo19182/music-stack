"""Soulseek availability: pause downloads during an outage and tell the owner on Telegram.

On 2026-10-08 the VPN lost UDP for ~90 minutes; every queued Soulseek job failed within
minutes with the same slskd error. Jobs now wait while slskd is logged out or failing.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger("acquisition")

CHECK_SECONDS = 30
SHORT_PAUSE = 60          # after one connection error: maybe just that uploader
LONG_PAUSE = 600          # after several in a row: the network is down
BURST, BURST_WINDOW = 3, 300
ALERT_AFTER = 600


class SoulseekHealth:
    def __init__(self, connected, clock=time.time):
        self._connected, self._clock = connected, clock
        self._lock = threading.Lock()
        self._checked_at, self._logged_in = 0.0, True
        self.paused_until, self.down_since, self.alerted = 0.0, None, False
        self._errors = []

    def refresh(self, force=False):
        now = self._clock()
        with self._lock:
            if not force and now - self._checked_at < CHECK_SECONDS:
                return self._logged_in
            self._checked_at = now
        logged_in = self._connected()
        with self._lock:
            self._logged_in = logged_in
            return logged_in

    def ready(self):
        """Whether a Soulseek download may start now."""
        if self._clock() < self.paused_until:
            return False
        return self.refresh()

    def failed(self):
        """A connection error during a download: pause briefly, or longer when they pile up."""
        now = self._clock()
        with self._lock:
            self._errors = [t for t in self._errors if now - t < BURST_WINDOW] + [now]
            pause = LONG_PAUSE if len(self._errors) >= BURST else SHORT_PAUSE
            self.paused_until = max(self.paused_until, now + pause)
            self._checked_at = 0.0   # check the server again before the next start

    def status(self):
        now = self._clock()
        logged_in = self.refresh()
        paused = now < self.paused_until
        return {"connected": logged_in and not paused, "logged_in": logged_in,
                "paused_until": self.paused_until if paused else None, "down_since": self.down_since}

    def watch(self, notify, waiting=lambda: 0):
        """One watchdog tick: track the outage and send one alert after ALERT_AFTER, one on recovery."""
        now = self._clock()
        healthy = self.refresh(force=True) and now >= self.paused_until
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
        with self._lock:
            self._errors = []


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

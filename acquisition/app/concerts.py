"""Read-only view of the concert worker's report (concerts/); never contacts event providers."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Madrid")
CHECK_HOUR = 18
# One missed daily run plus slack before the report is called stale.
STALE_AFTER = timedelta(hours=26)
MAX_REPORT_BYTES = 20 * 1024 * 1024
DEFAULT_SETTINGS = {"lookahead_days": 365, "max_nationwide_dates": 3}


def current_time():
    return datetime.now(timezone.utc)


def name_key(value):
    # Same identity as concert_alerts.library.name_key.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value)).casefold()).strip()


def search_key(value):
    value = unicodedata.normalize("NFKD", str(value).casefold())
    return " ".join(re.findall(r"\w+", "".join(c for c in value if not unicodedata.combining(c))))


def safe_url(value):
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else ""


def text(value, limit=300):
    return str(value)[:limit] if isinstance(value, (str, int, float)) else ""


def next_check(now):
    local = now.astimezone(ZONE)
    due = local.replace(hour=CHECK_HOUR, minute=0, second=0, microsecond=0)
    if due <= local:
        due = (local + timedelta(days=1)).replace(hour=CHECK_HOUR, minute=0, second=0, microsecond=0)
    return due


def load_report(path):
    """``(report, None)`` or ``(None, (status, message))``. Only this one file is ever read."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None, ("invalid", "The concert report is too large to show.")
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, ("missing", "No concert report yet. It appears after the concert check runs.")
    except PermissionError:
        return None, ("unavailable", "The concert report cannot be read by the web app.")
    except (OSError, UnicodeDecodeError, ValueError):
        return None, ("invalid", "The concert report could not be read; the next check will replace it.")
    if not isinstance(report, dict) or not isinstance(report.get("events"), list):
        return None, ("invalid", "The concert report has an unexpected format.")
    return report, None


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def checked_time(report):
    return parse_time(report.get("checked_at"))


def load_run(path):
    """The worker's last run outcome (run-status.json), or None when absent or unreadable."""
    try:
        run = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    finished = parse_time(run.get("finished_at")) if isinstance(run, dict) else None
    if not finished:
        return None
    return {"ok": run.get("ok") is True, "finished_at": finished.isoformat(),
            "mode": text(run.get("mode"), 20), "error": text(run.get("error"), 300)}


def run_notice(run, checked):
    if not run or run["ok"] or (checked and parse_time(run["finished_at"]) < checked):
        return ""
    when = parse_time(run["finished_at"]).astimezone(ZONE).strftime("%a %d %b %H:%M")
    shown = " Showing the previous report." if checked else ""
    return f"The last concert check failed ({when}): {run['error'] or 'unknown error'}.{shown}"


def clean_event(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("artists"), dict) or not raw.get("id"):
        return None
    when = text(raw.get("date"), 10)
    try:
        date.fromisoformat(when)
    except ValueError:
        when = ""
    time = text(raw.get("time"), 8)
    return {
        "id": text(raw["id"], 120), "name": text(raw.get("name")) or "Untitled event",
        "date": when, "date_unknown": bool(raw.get("date_unknown")) or not when,
        "time": time[:5] if re.fullmatch(r"\d\d:\d\d(:\d\d)?", time) and not raw.get("time_unknown") else "",
        "status": text(raw.get("status"), 40) or "unknown",
        "venue": text(raw.get("venue"), 200), "city": text(raw.get("city"), 120),
        "highlight": text(raw.get("highlight"), 80),
        "artists": {text(k, 120): text(v, 300) for k, v in raw["artists"].items() if k and v},
        "url": safe_url(raw.get("url")), "source": text(raw.get("source"), 80),
    }


def source_name(source_id):
    prefix = str(source_id).split(":", 1)[0] if ":" in str(source_id) else ""
    return {"ra": "Resident Advisor", "dice": "DICE"}.get(prefix, "Ticketmaster")


def restricted_ids(events, max_dates):
    """Mirrors notify.select_digest_dates: long tours keep only preferred-area dates; a bill
    stays nationwide when any matched artist has few dates."""
    if not max_dates:
        return set()
    dates = defaultdict(set)
    for event in events:
        for artist in event["artists"]:
            dates[artist].add(event["id"])
    return {event["id"] for event in events
            if event["artists"] and not event["highlight"]
            and all(len(dates[artist]) > max_dates for artist in event["artists"])}


def group_key(event):
    artists = tuple(sorted(event["artists"]))
    return artists, name_key(event["name"]) if len(artists) > 1 else None


def sources_view(report):
    sources = report.get("sources") if isinstance(report.get("sources"), dict) else {}
    view = []
    for name, info in sorted(sources.items()):
        info = info if isinstance(info, dict) else {}
        error = text(info.get("error"), 200)
        view.append({"name": text(name, 80), "status": "ok" if info.get("status") == "ok" else "unavailable",
                     "events": info.get("events") if isinstance(info.get("events"), int) else None,
                     "cached": bool(info.get("cached")),
                     "pending": info.get("pending") if isinstance(info.get("pending"), int) else 0,
                     "error": "" if re.search(r"https?:|apikey|token|key=", error, re.I) else error})
    return view


def group_order(group):
    # Groups with shown dates first, chronologically; fully hidden long tours last.
    first = (group["dates"] or group["hidden"])[0]
    return not group["dates"], first["date_unknown"], first["date"], first["time"], group["artists"].casefold()


def build_view(report, *, now=None, area="all", q="", days=0, run=None):
    now = now or current_time()
    settings = {**DEFAULT_SETTINGS}
    for key in settings:
        value = (report.get("settings") or {}).get(key) if isinstance(report.get("settings"), dict) else None
        if isinstance(value, int) and value >= 0:
            settings[key] = value
    events = [e for e in map(clean_event, report["events"]) if e]
    # Restriction counts every known date, as the digest does, before any view filter.
    restricted = restricted_ids(events, settings["max_nationwide_dates"])
    alternates = defaultdict(list)
    for item in report.get("duplicates") or []:
        if isinstance(item, dict) and safe_url(item.get("url")):
            alternates[text(item.get("canonical_id"), 120)].append(
                {"source": source_name(item.get("source_id", "")), "url": item["url"]})

    today = now.astimezone(ZONE).date()
    until = today + timedelta(days=days) if days else None
    needle = search_key(q)
    groups = {}
    for event in events:
        if event["date"] and date.fromisoformat(event["date"]) < today:
            continue
        if until and event["date"] and date.fromisoformat(event["date"]) > until:
            continue
        if area == "preferred" and not event["highlight"]:
            continue
        if needle and needle not in search_key(" ".join([*event["artists"].values(), event["name"]])):
            continue
        event = {**event, "restricted": event["id"] in restricted,
                 "alternates": [a for a in alternates.get(event["id"], []) if a["url"] != event["url"]][:4]}
        groups.setdefault(group_key(event), []).append(event)

    view, shown = [], 0
    for key, dates in groups.items():
        dates.sort(key=lambda e: (e["date_unknown"], e["date"], e["time"], e["id"]))
        artists = ", ".join(dates[0]["artists"].values())
        titles = {name_key(e["name"]) for e in dates}
        common = len(titles) == 1 and name_key(artists) not in titles
        for event in dates:
            event["show_title"] = not common and name_key(event["name"]) != name_key(artists)
        visible = [e for e in dates if not e["restricted"]]
        shown += len(visible)
        view.append({"key": "|".join(key[0]) + "|" + (key[1] or ""), "artists": artists,
                     "title": dates[0]["name"] if common else "", "multi": len(key[0]) > 1,
                     "dates": visible, "hidden": [e for e in dates if e["restricted"]]})
    view.sort(key=group_order)

    checked = checked_time(report)
    stale = checked is None or now - checked > STALE_AFTER
    sources = sources_view(report)
    down = [s["name"] for s in sources if s["status"] != "ok"]
    notices = [run_notice(run, checked),
               "The last concert check is more than a day old; these listings may be out of date." if stale else "",
               f"{' and '.join(down)} {'was' if len(down) == 1 else 'were'} unavailable at the last check; "
               "listings from it may be missing." if down else ""]
    return {
        "status": "stale" if stale else "ok",
        "notices": [n for n in notices if n], "last_run": run,
        "checked_at": checked.isoformat() if checked else None,
        "next_check": next_check(now).isoformat(),
        "schedule": f"Daily at {CHECK_HOUR}:00 Europe/Madrid",
        "sources": sources, "settings": settings,
        "coverage": text(report.get("coverage"), 400),
        "counts": {"matching": len(events), "listings": sum(len(g["dates"]) + len(g["hidden"]) for g in view),
                   "shown": shown, "hidden": sum(len(g["hidden"]) for g in view), "groups": len(view)},
        "groups": view,
    }


def empty_view(status, message, now=None, run=None):
    now = now or current_time()
    notices = [n for n in (run_notice(run, None), message) if n]
    return {"status": status, "notices": notices, "last_run": run, "checked_at": None,
            "next_check": next_check(now).isoformat(), "schedule": f"Daily at {CHECK_HOUR}:00 Europe/Madrid",
            "sources": [], "settings": dict(DEFAULT_SETTINGS), "coverage": "",
            "counts": {"matching": 0, "listings": 0, "shown": 0, "hidden": 0, "groups": 0}, "groups": []}

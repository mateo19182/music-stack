import hashlib
import json
import re
import unicodedata
from html import escape
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import quote, urlsplit

from .library import name_key


def safe_url(value):
    parsed = urlsplit(value or "")
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username else ""


def highlights(venue):
    city = name_key(venue.get("city", {}).get("name", ""))
    state = name_key(venue.get("state", {}).get("name", ""))
    state_code = venue.get("state", {}).get("stateCode", "").upper().removeprefix("ES-")
    if city in {"a coruña", "la coruña", "a coruna", "la coruna"}:
        return "A Coruña · Galicia"
    if city in {"santiago de compostela", "vigo", "ferrol", "lugo", "ourense", "orense", "pontevedra"}:
        return "Galicia"
    if state in {"galicia", "a coruña", "la coruña", "a coruna", "la coruna", "lugo", "ourense", "orense", "pontevedra"}:
        return "Galicia"
    if state_code in {"GA", "C", "LU", "OU", "PO"}:
        return "Galicia"
    if state_code in {"M", "MD", "B"}:
        return "Barcelona" if state_code == "B" else "Madrid"
    if city in {"madrid", "barcelona"} or state in {"madrid", "comunidad de madrid", "barcelona"}:
        return "Madrid" if "madrid" in (city + state) else "Barcelona"
    return ""


def select_events(raw_events, index):
    selected, unresolved, unknown_locations = {}, {}, []
    for raw in raw_events:
        venues = raw.get("_embedded", {}).get("venues", [])
        if not venues:
            unknown_locations.append(str(raw.get("id", "")))
            continue
        venue = venues[0]
        country = venue.get("country", {}).get("countryCode", "").upper()
        if country != "ES":
            if not country:
                unknown_locations.append(str(raw.get("id", "")))
            continue
        artists = {}
        for attraction in raw.get("_embedded", {}).get("attractions", []):
            artist, issue = index.match(attraction)
            if artist:
                artists[artist.id] = artist.name
            elif issue:
                unresolved[(issue["provider_id"], issue["name"])] = issue
        if not artists:
            continue
        dates = raw.get("dates", {})
        start = dates.get("start", {})
        event = {
            "id": str(raw["id"]), "name": raw.get("name", "Untitled event"),
            "date": start.get("localDate", ""), "time": start.get("localTime", ""),
            "timezone": dates.get("timezone", ""),
            "date_unknown": bool(start.get("dateTBA") or start.get("dateTBD")),
            "time_unknown": bool(start.get("timeTBA") or start.get("noSpecificTime")),
            "status": dates.get("status", {}).get("code", "unknown"),
            "venue": venue.get("name", ""), "venue_id": str(venue.get("id", "")),
            "city": venue.get("city", {}).get("name", ""), "country": country,
            "highlight": highlights(venue), "artists": dict(sorted(artists.items())),
            "url": safe_url(raw.get("url", "")), "source": "Ticketmaster",
            "lineup_complete": False,
        }
        # One provider ID groups every matched artist on that event, including festivals.
        selected[event["id"]] = event
    events = sorted(selected.values(), key=lambda e: (not bool(e["highlight"]), e["date"], e["name"], e["id"]))
    return events, list(unresolved.values()), unknown_locations


def fingerprint(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def place_key(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in value if not unicodedata.combining(c))))


def same_show(left, right):
    return (left["source"] != right["source"]
            and not any(e["date_unknown"] or e["time_unknown"] or not e["time"] for e in (left, right))
            and left["date"] == right["date"] and left["time"][:5] == right["time"][:5]
            and bool(place_key(left["venue"])) and not place_key(left["venue"]).startswith(("tba", "tbc", "venue tba"))
            and place_key(left["venue"]) == place_key(right["venue"])
            and place_key(left["city"]) == place_key(right["city"])
            and bool(set(left["artists"]) & set(right["artists"])))


def notification_signature(event):
    # Source attribution, title wording and URL changes alone do not warrant another alert.
    # Nor does a sale-window change (not on sale -> on sale): the date was already announced.
    status = "listed" if event["status"] in {"onsale", "listed", "offsale"} else event["status"]
    return (event["date"], event["time"][:5], event["date_unknown"], event["time_unknown"],
            place_key(event["venue"]), place_key(event["city"]), sorted(event["artists"]), status)


def select_ra_events(raw_events, index, start=None, end=None):
    selected, unresolved, unknown_locations = [], [], []
    provinces = {"15": "C", "27": "LU", "32": "OU", "36": "PO", "28": "M", "08": "B"}
    for raw in raw_events:
        start_time = raw.get("startTime")
        zone = (raw.get("area") or {}).get("ianaTimeZone")
        if start_time and zone and start is not None:
            try:
                actual_start = datetime.fromisoformat(start_time)
                if actual_start.tzinfo is None:
                    actual_start = actual_start.replace(tzinfo=ZoneInfo(zone))
                if actual_start < start or (end is not None and actual_start > end):
                    continue
            except (ValueError, ZoneInfoNotFoundError):
                unknown_locations.append("ra:" + str(raw["id"]))
                continue
        venue = raw.get("venue") or {}
        area = venue.get("area") or {}
        if (area.get("country") or {}).get("urlCode", "").upper() != "ES":
            unknown_locations.append("ra:" + str(raw["id"]))
            continue
        artists = {}
        for artist in raw.get("artists") or []:
            match, issue = index.match({"id": "ra:" + str(artist["id"]), "name": artist["name"]})
            if match:
                artists[match.id] = match.name
            elif issue:
                unresolved.append(issue)
        if not raw.get("artists"):
            unresolved.append({"provider_id": "ra:" + str(raw["id"]), "name": raw["title"], "reason": "no linked RA artists"})
        if not artists:
            continue
        if re.search(r"listening session|sesión de escucha|sessió d'escolta", name_key(raw["title"])):
            unresolved.append({"provider_id": "ra:" + str(raw["id"]), "name": raw["title"],
                               "reason": "listening session; artist performance unconfirmed"})
            continue
        address = venue.get("address") or ""
        if not address and place_key(venue.get("name") or "") in {"", "tba", "tbc", "venue tba"}:
            unknown_locations.append("ra:" + str(raw["id"]))
            continue
        postal = re.search(r"\b(\d{5})\s+([^,;\n]+)", address)
        city = postal.group(2).strip() if postal else area.get("name", "")
        if not postal and city in {"North", "South", "East"}:
            city += " (RA area)"
        province = provinces.get(postal.group(1)[:2], "") if postal else ""
        local_start = raw.get("startTime") or ""
        selected.append({
            "id": "ra:" + str(raw["id"]), "name": raw["title"],
            "date": (raw.get("date") or "")[:10], "time": local_start[11:19],
            "date_unknown": not bool(raw.get("date")), "time_unknown": not bool(local_start),
            "timezone": (raw.get("area") or {}).get("ianaTimeZone") or "",
            "status": "listed", "venue": venue.get("name") or "",
            "venue_id": "ra:" + str(venue.get("id", "")), "city": city, "country": "ES",
            "highlight": highlights({"city": {"name": city}, "state": {"stateCode": province}}),
            "artists": dict(sorted(artists.items())),
            "url": safe_url("https://ra.co/events/" + str(raw["id"])),
            "source": "Resident Advisor", "lineup_complete": False,
        })
    return selected, unresolved, unknown_locations


def select_dice_events(raw_events, index, start=None, end=None):
    selected, unresolved, unknown_locations = [], [], []
    provinces = {"15": "C", "27": "LU", "32": "OU", "36": "PO", "28": "M", "08": "B"}
    for raw in raw_events:
        id = "dice:" + raw["url"].rstrip("/").rsplit("/", 1)[-1].split("-", 1)[0]
        try:
            actual_start = datetime.fromisoformat(raw["start"])
            if actual_start.tzinfo is None:
                actual_start = actual_start.replace(tzinfo=ZoneInfo("Europe/Madrid"))
        except (ValueError, TypeError):
            unknown_locations.append(id)
            continue
        if start is not None and (actual_start < start or (end is not None and actual_start > end)):
            continue
        if raw.get("country") != "ES":
            if not raw.get("country"):
                unknown_locations.append(id)
            continue
        artists = {}
        for artist in raw.get("artists") or []:
            match, issue = index.match({"id": "dice:" + (artist.get("id") or artist["name"]), "name": artist["name"]})
            if match:
                artists[match.id] = match.name
            elif issue:
                unresolved.append(issue)
        if not artists:
            continue
        postal = re.search(r"\b(\d{5})\b", raw.get("postal") or raw.get("address") or "")
        province = provinces.get(postal.group(1)[:2], "") if postal else ""
        city = raw.get("locality") or ""
        # startDate is the venue's wall-clock time with its offset.
        selected.append({
            "id": id, "name": raw["name"], "date": raw["start"][:10], "time": raw["start"][11:19], "date_unknown": False, "time_unknown": not raw["start"][11:19],
            "timezone": "Europe/Madrid", "status": raw.get("status") or "listed",
            "venue": raw.get("venue") or "", "venue_id": "", "city": city, "country": "ES",
            "highlight": highlights({"city": {"name": city}, "state": {"stateCode": province}}),
            "artists": dict(sorted(artists.items())), "url": safe_url(raw["url"]),
            "source": "DICE", "lineup_complete": False,
        })
    return selected, unresolved, unknown_locations


def telegram_text(text, units=800):
    # Hermes interprets MEDIA: as a local attachment, even inside message text.
    text = re.sub(r"MEDIA:", "MEDIA∶", text, flags=re.IGNORECASE)
    return text.encode("utf-16-le")[:units * 2].decode("utf-16-le", errors="ignore")


def telegram_html(text, units=800):
    escaped, length = [], 0
    for character in telegram_text(text, units):
        entity = escape(character)
        size = len(entity.encode("utf-16-le")) // 2
        if length + size > units:
            break
        escaped.append(entity)
        length += size
    return "".join(escaped)


def notification(event, changed=False, show_title=False):
    when = "Date TBA" if event["date_unknown"] else (event["date"] or "Date unknown")
    if not event["date_unknown"] and event["date"]:
        try:
            when = date.fromisoformat(event["date"]).strftime("%d %b %Y")
        except ValueError:
            pass
    city = ("★ " if event["highlight"] else "") + telegram_html(event["city"], 120)
    parts = [telegram_html(when, 80), city, telegram_html(event["venue"], 200)]
    if event["time"] and not event["time_unknown"]:
        parts.append(telegram_html(event["time"][:5], 20))
    if changed:
        parts.append("Updated")
    if event["status"] not in {"onsale", "listed"}:
        status = {"offsale": "Not on sale", "cancelled": "CANCELLED",
                  "postponed": "POSTPONED", "rescheduled": "RESCHEDULED"}
        parts.append(status.get(event["status"], telegram_html(event["status"], 80)))
    text = "• " + " · ".join(part for part in parts if part)
    if show_title:
        text += "\n  " + telegram_html(event["name"], 200)
    url = safe_url(event["url"])
    # URL-encode Hermes' attachment marker without changing its decoded destination.
    url = re.sub(r"MEDIA:", "MEDIA%3A", url, flags=re.IGNORECASE)
    href = escape(quote(url, safe=":/?&=#%+@,;"), quote=True) if url else ""
    if href and len(href.encode("utf-16-le")) // 2 <= 1000:
        text += f' · <a href="{href}">Event / tickets</a>'
    return text

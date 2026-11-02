"""DICE reader: published sitemaps for discovery, schema.org JSON-LD on event pages for details.

Used with DICE's permission; requests are throttled, budgeted and only for new or changed pages.
"""

import json
import re
import time
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .ticketmaster import ProviderError

INDEX = "https://dice.fm/sitemaps/sitemap.xml"
USER_AGENT = "music-stack-concerts/0.1 (personal concert alerts; permitted by DICE)"
# Slugs end in "<venue>-<city>-tickets"; the page's country is still checked before use.
PLACES = (
    "a-coruna coruna la-coruna santiago-de-compostela santiago vigo ferrol lugo ourense pontevedra "
    "madrid barcelona valencia sevilla seville malaga granada cordoba cadiz jerez almeria huelva jaen "
    "bilbao donostia san-sebastian vitoria gasteiz pamplona iruna logrono santander oviedo gijon aviles "
    "leon burgos valladolid salamanca segovia zaragoza huesca teruel girona tarragona reus lleida "
    "sabadell terrassa badalona hospitalet mataro castellon alicante elche benidorm murcia cartagena "
    "albacete toledo ciudad-real cuenca guadalajara caceres badajoz merida palma mallorca ibiza eivissa "
    "menorca tenerife las-palmas gran-canaria lanzarote fuerteventura marbella torremolinos benicassim "
    "alcala-de-henares mostoles getafe leganes fuenlabrada alcobendas cantabria asturias galicia spain espana"
).split()
PLACE_PATTERN = re.compile(r"-(?:" + "|".join(re.escape(p) for p in PLACES) + r")-(?:[a-z0-9-]*-)?tickets?$")
STATUS = {"EventCancelled": "cancelled", "EventPostponed": "postponed", "EventRescheduled": "rescheduled"}


def spanish_slug(url):
    return bool(PLACE_PATTERN.search(url.rstrip("/").rsplit("/", 1)[-1]))


def sitemap_entries(xml):
    entries = {}
    for block in re.findall(r"<(?:url|sitemap)>(.*?)</(?:url|sitemap)>", xml, re.S):
        loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block)
        if loc:
            lastmod = re.search(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", block)
            entries[unescape(loc.group(1))] = lastmod.group(1) if lastmod else ""
    return entries


def dice_url(value):
    parsed = urlsplit(value or "")
    return parsed.scheme == "https" and parsed.hostname == "dice.fm" and not parsed.username


def _text(value):
    return unescape(value).strip() if isinstance(value, str) else ""


def parse_event_page(html, url):
    """The page's schema.org event as a compact record, or None when it has none."""
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict) or "Event" not in str(item.get("@type", "")):
                continue
            location = item.get("location") if isinstance(item.get("location"), dict) else {}
            address = location.get("address") if isinstance(location.get("address"), dict) else {}
            country = address.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name") or country.get("identifier")
            performers = item.get("performer") or []
            performers = performers if isinstance(performers, list) else [performers]
            artists = []
            for performer in performers:
                if not isinstance(performer, dict) or not _text(performer.get("name")):
                    continue
                link = performer.get("url") if dice_url(performer.get("url")) else ""
                slug = link.rstrip("/").rsplit("/", 1)[-1] if "/artist/" in link else ""
                artists.append({"id": slug, "name": _text(performer["name"])})
            canonical = item.get("url") if dice_url(item.get("url")) else url
            status = STATUS.get(str(item.get("eventStatus", "")).rsplit("/", 1)[-1], "listed")
            # DICE marks ordinary events EventRescheduled; only a previous date confirms it.
            if status == "rescheduled" and not item.get("previousStartDate"):
                status = "listed"
            return {
                "url": canonical, "name": _text(item.get("name")) or "Untitled event",
                "start": _text(item.get("startDate")),
                "status": status,
                "venue": _text(location.get("name")), "address": _text(address.get("streetAddress")),
                "locality": _text(address.get("addressLocality")), "postal": _text(address.get("postalCode")),
                "country": _text(country).upper()[:2] if _text(country) else "", "artists": artists,
            }
    return None


class Dice:
    def __init__(self, cache_path=None, client=None, sleep=time.sleep, page_budget=150, delay=2.0):
        self.client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self.owns_client = client is None
        self.cache_path = Path(cache_path) if cache_path else None
        self.sleep = sleep
        self.page_budget = page_budget
        self.delay = delay
        self.requests = 0
        self.pages_fetched = 0
        self.pending = 0

    def close(self):
        if self.owns_client:
            self.client.close()

    def _get(self, url, allow_missing=False):
        for attempt in range(3):
            self.sleep(self.delay)
            self.requests += 1
            try:
                response = self.client.get(url)
            except httpx.HTTPError:
                if attempt < 2:
                    self.sleep(5 * 2 ** attempt)
                    continue
                raise ProviderError("DICE network failure; source unavailable") from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    self.sleep(5 * 2 ** attempt)
                    continue
            if allow_missing and response.status_code in {404, 410}:
                return None
            if response.status_code != 200:
                raise ProviderError(f"DICE HTTP {response.status_code}; source unavailable")
            return response.text

    def _load_cache(self):
        if self.cache_path and self.cache_path.exists():
            try:
                cache = json.loads(self.cache_path.read_text())
                if isinstance(cache.get("pages"), dict):
                    return cache["pages"]
            except (OSError, ValueError, AttributeError):
                pass
        return {}

    def _save_cache(self, pages):
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"saved_at": time.time(), "pages": pages}, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(self.cache_path)

    def listed(self):
        """Spanish event pages in DICE's sitemaps with their lastmod; fails closed."""
        index = sitemap_entries(self._get(INDEX))
        shards = [url for url in index if dice_url(url) and urlsplit(url).path.startswith("/sitemaps/")]
        if not shards:
            raise ProviderError("DICE sitemap index was empty; source incomplete")
        listed = {}
        for shard in shards:
            for url, lastmod in sitemap_entries(self._get(shard)).items():
                if dice_url(url) and urlsplit(url).path.startswith("/event/") and spanish_slug(url):
                    listed[url] = lastmod
        if not listed:
            raise ProviderError("DICE sitemaps listed no Spanish events; source incomplete")
        return listed

    def events(self, start=None, end=None):
        listed = self.listed()
        cached = self._load_cache()
        pages = {url: cached[url] for url in listed if url in cached}
        todo = [url for url in listed if url not in pages]
        todo += sorted((url for url in listed if url in pages and pages[url].get("lastmod") != listed[url]),
                       key=lambda url: pages[url].get("fetched_at", 0))
        failures = 0
        try:
            for url in todo:
                if self.pages_fetched >= self.page_budget:
                    break
                try:
                    html = self._get(url, allow_missing=True)
                except ProviderError:
                    failures += 1
                    if failures >= 5:
                        raise ProviderError("DICE event pages keep failing; source incomplete") from None
                    continue
                self.pages_fetched += 1
                event = parse_event_page(html, url) if html else None
                pages[url] = {"lastmod": listed[url], "fetched_at": time.time(), "event": event}
                if self.pages_fetched % 50 == 0:
                    self._save_cache(pages)  # keep progress if a long run is interrupted
        finally:
            self.pending = sum(url not in pages or pages[url].get("lastmod") != listed[url] for url in listed)
            self._save_cache(pages)
        return [page["event"] for page in pages.values() if page.get("event")]


def fill_cache(cache_path, page_budget):
    """Warm the cache outside the timed service run; sends nothing."""
    reader = Dice(cache_path=cache_path, page_budget=page_budget)
    try:
        events = reader.events()
        return {"events": len(events), "pages_fetched": reader.pages_fetched, "pending": reader.pending,
                "requests": reader.requests}
    finally:
        reader.close()

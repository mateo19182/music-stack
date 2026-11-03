"""Resident Advisor's unofficial GraphQL reader; query approach adapted from Cuepoint (MIT)."""

import json
import time
from pathlib import Path

import httpx

from .ticketmaster import ProviderError


AREAS_QUERY = "{ countries { urlCode areas { id name } } }"
EVENTS_QUERY = """
query GET_EVENT_LISTINGS($filters: FilterInputDtoInput, $page: Int, $pageSize: Int) {
  eventListings(filters: $filters, page: $page, pageSize: $pageSize) {
    totalResults
    data { listingDate event {
      id title date startTime contentUrl isFestival
      artists { id name }
      venue { id name address area { id name country { urlCode } } }
      area { ianaTimeZone }
    } }
  }
}
"""


class ResidentAdvisor:
    def __init__(self, cache_path=None, client=None, sleep=time.sleep, request_budget=300):
        self.client = client or httpx.Client(timeout=30, headers={
            "User-Agent": "music-stack-concerts/0.1", "Referer": "https://ra.co/events"})
        self.owns_client = client is None
        self.cache_path = Path(cache_path) if cache_path else None
        self.sleep = sleep
        self.request_budget = request_budget
        self.requests = 0
        self.cache_used = False
        self.areas = []
        self.duplicate_rows = 0

    def close(self):
        if self.owns_client:
            self.client.close()

    def _query(self, query, variables=None):
        for attempt in range(3):
            if self.requests >= self.request_budget:
                raise ProviderError("RA request budget exhausted; source incomplete")
            self.sleep(0.5)
            self.requests += 1
            try:
                response = self.client.post("https://ra.co/graphql", json={"query": query, "variables": variables or {}})
            except httpx.HTTPError:
                if attempt < 2:
                    self.sleep(2 ** attempt)
                    continue
                raise ProviderError("RA network failure; source unavailable") from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    self.sleep(2 ** attempt)
                    continue
            if response.status_code != 200:
                raise ProviderError(f"RA HTTP {response.status_code}; source unavailable")
            try:
                result = response.json()
                if result.get("errors") or not isinstance(result.get("data"), dict):
                    raise ValueError
                return result["data"]
            except (ValueError, AttributeError):
                raise ProviderError("RA returned invalid data or GraphQL errors; source incomplete") from None

    def events(self, start, end):
        dates = [start.date().isoformat(), end.date().isoformat()]
        if self.cache_path and self.cache_path.exists():
            try:
                cache = json.loads(self.cache_path.read_text())
                age = time.time() - cache["saved_at"]
                if cache["dates"] == dates and 0 <= age < 6 * 3600 and isinstance(cache["events"], list):
                    self.cache_used = True
                    self.areas = cache["areas"]
                    self.duplicate_rows = cache.get("duplicate_rows", 0)
                    return cache["events"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        try:
            countries = self._query(AREAS_QUERY)["countries"]
            self.areas = next(country["areas"] for country in countries if country["urlCode"] == "ES")
            area_ids = [int(area["id"]) for area in self.areas]
            if not area_ids:
                raise ValueError
            found, listings, page, total, received = {}, set(), 1, None, 0
            while True:
                data = self._query(EVENTS_QUERY, {
                    "filters": {"areas": {"any": area_ids}, "listingDate": {"gte": dates[0], "lte": dates[1]}},
                    "page": page, "pageSize": 100,
                })["eventListings"]
                count = int(data["totalResults"])
                if count < 0 or (total is not None and count != total):
                    raise ProviderError("RA result count changed; source incomplete")
                total = count
                rows = data["data"]
                if not isinstance(rows, list) or (received < total and not rows):
                    raise ValueError
                received += len(rows)
                previous = len(listings)
                for row in rows:
                    event = row["event"]
                    if (not event.get("id") or not isinstance(event.get("title"), str)
                            or not isinstance(event.get("date"), str)
                            or not isinstance(event.get("artists"), list)):
                        raise ValueError
                    # Multi-day listings can repeat the same event on different days.
                    listings.add((event["id"], row.get("listingDate", event["date"])))
                    found[str(event["id"])] = event
                if len(listings) == previous and len(listings) < total:
                    raise ProviderError("RA pagination made no progress; source incomplete")
                # RA's page order is not stable: a listing can appear on two pages, so another one
                # may have been skipped. Stop on rows received, and record the repeats.
                if received >= total:
                    self.duplicate_rows = received - len(listings)
                    if self.duplicate_rows > max(5, total // 50):
                        raise ProviderError("RA repeated too many listings across pages; source incomplete")
                    break
                page += 1
        except (KeyError, TypeError, ValueError, StopIteration):
            raise ProviderError("RA returned incomplete area or event data") from None
        events = list(found.values())
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"saved_at": time.time(), "dates": dates, "areas": self.areas,
                                             "duplicate_rows": self.duplicate_rows, "events": events}))
            temporary.chmod(0o600)
            temporary.replace(self.cache_path)
        return events

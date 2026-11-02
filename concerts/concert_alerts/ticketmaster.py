import time
from datetime import timedelta

import httpx


class ProviderError(RuntimeError):
    pass


class Ticketmaster:
    def __init__(self, api_key, client=None, request_budget=1000, sleep=time.sleep):
        if not api_key:
            raise ValueError("Set ticketmaster_api_key in the private concert configuration")
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=30)
        self.owns_client = client is None
        self.request_budget = request_budget
        self.requests = 0
        self.sleep = sleep

    def close(self):
        if self.owns_client:
            self.client.close()

    def _page(self, params):
        for attempt in range(3):
            if self.requests >= self.request_budget:
                raise ProviderError("Ticketmaster request budget exhausted; sync incomplete")
            self.sleep(0.26)
            self.requests += 1
            try:
                response = self.client.get(
                    "https://app.ticketmaster.com/discovery/v2/events.json",
                    params={**params, "apikey": self.api_key},
                )
            except httpx.HTTPError:
                if attempt == 2:
                    raise ProviderError("Ticketmaster network failure; sync incomplete") from None
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise ProviderError(f"Ticketmaster HTTP {response.status_code}; sync incomplete")
                try:
                    delay = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    delay = 2 ** attempt
                self.sleep(min(max(delay, 0), 30))
                continue
            if response.status_code != 200:
                raise ProviderError(f"Ticketmaster HTTP {response.status_code}; check provider access")
            try:
                data = response.json()
                if not isinstance(data.get("page"), dict) or "totalElements" not in data["page"]:
                    raise ValueError
                return data
            except (ValueError, AttributeError):
                raise ProviderError("Invalid Ticketmaster response; sync incomplete") from None

    def events(self, start, end):
        found = {}

        def window(left, right):
            params = {
                "countryCode": "ES", "classificationName": "music", "locale": "*",
                "startDateTime": left.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": right.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": 200, "page": 0, "sort": "date,asc",
                "includeTBA": "no", "includeTBD": "no",
            }
            first = self._page(params)
            count = int(first["page"]["totalElements"])
            if count > 1000:
                seconds = int((right - left).total_seconds())
                if seconds < 1:
                    raise ProviderError("More than 1000 events at one instant; sync incomplete")
                middle = left + timedelta(seconds=seconds // 2)
                window(left, middle)
                window(middle + timedelta(seconds=1), right)
                return
            pages = (count + 199) // 200
            window_ids = set()
            for page in range(max(pages, 1)):
                data = first if page == 0 else self._page({**params, "page": page})
                if int(data["page"]["totalElements"]) != count:
                    raise ProviderError("Ticketmaster result count changed; retry sync")
                events = data.get("_embedded", {}).get("events", [])
                if count and not events:
                    raise ProviderError("Ticketmaster returned an empty result page; sync incomplete")
                for event in events:
                    if not event.get("id"):
                        raise ProviderError("Ticketmaster event has no ID; sync incomplete")
                    window_ids.add(event["id"])
                    found[event["id"]] = event
            if len(window_ids) != count:
                raise ProviderError("Ticketmaster returned incomplete pages; retry sync")

        window(start.replace(microsecond=0), end.replace(microsecond=0))
        return list(found.values())

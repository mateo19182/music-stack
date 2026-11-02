# Concert alerts

Python worker for library artists playing anywhere in Spain, including festival
listings. Highlights Galicia/A Coruña, Madrid and Barcelona. All artists are
enabled; there is no favourites filter. Dry-run is the default and no timer is
installed automatically.

Ticketmaster is enabled by default. Set `ra_enabled` to `true` in the private
configuration to add Resident Advisor. It uses the same library matching,
location rules and Telegram delivery; no RA account, key or browser is needed.
Set `dice_enabled` to add DICE (see below); its dates reach Telegram only once
`dice_notify` is also `true`.

Artists with more than three known upcoming dates are restricted to preferred
areas in Telegram: Galicia, Madrid and Barcelona. Artists with three or fewer
dates remain eligible anywhere in Spain. `max_nationwide_dates` configures this
threshold; zero disables the restriction. Counts use the whole fetched catalogue,
not just newly announced dates, and include appearances under different event
titles. A festival stays eligible nationally when at least one matched artist has
few dates. Hidden dates remain in the report and are not marked as delivered;
visible groups say how many other dates were hidden.

## Setup and use

From the repository root:

```sh
uv sync --project concerts --frozen --group dev
install -d -m 700 config/concerts
# Only on first setup; preserve an existing private configuration.
test -e config/concerts/config.json || install -m 600 examples/concerts.config.json config/concerts/config.json
cd concerts
.venv/bin/python -m concert_alerts library
.venv/bin/python -m concert_alerts sync
```

Set `ticketmaster_api_key` to the app's Consumer Key in the private configuration.
Paths in that file are relative to its directory. Defaults read this checkout's
Navidrome database in read-only mode and write private state/report files under
`state/concerts/`. Library access needs no playback password or acquisition token.
Configuration must be mode 0600. The worker creates private state with umask 077.

`sync` queries the next 365 days (configurable up to 730), matches events and
writes `state/concerts/latest.json`. Review its matches, unmatched/ambiguous
provider artists, unknown locations and URL-like library artist tags. The library
command also reports invalid tags; neither command changes music or library tags.
The report's `sources` section distinguishes a successful empty result from an
unavailable provider. If one provider fails, the other continues; if both fail,
the run sends nothing. No failure is interpreted as a cancellation.

After reviewing real matches, deliver pending events explicitly:

```sh
.venv/bin/python -m concert_alerts sync --send
```

Delivery uses `hermes send -q --to telegram` with an argument list. Dates are
grouped by artist and ordered chronologically, with venue, city, local time and a
compact clickable "Event / tickets" link per date. Preferred areas get a star.
Solo matches share a block across different event titles; titles remain beside
their dates when they differ. Multi-artist bills stay together as shared listings
rather than being repeated for each matching artist.
The lookahead appears once in the digest header and the source once at the end.
Normal on-sale status and repetitive library/lineup caveats are omitted. Explicit
changed statuses, dates or lineups produce updates; a vanished listing is never
treated as a cancellation. Messages are numbered `1/N`, `2/N`, etc. Whole groups
move to the next message when needed; only a group too large for an empty message
is split, with a continuation heading. Delivery is tracked for each date so failed
messages can be retried independently. HTML payloads are bounded before Hermes
receives them, keeping its own message splitting from cutting a group again.
Telegram link previews are turned off by Hermes' documented
`gateway.platforms.telegram.extra.disable_link_previews: true` in
`~/.hermes/config.yaml`; `hermes send` has no per-message flag. The setting
applies to every Hermes Telegram message: standalone `hermes send` reads it on
each run, the running gateway after its next restart.

SQLite tracks observed and successfully notified event versions separately.
Dry runs leave notifications pending. A failed batch is retried; successfully
sent earlier batches stay recorded. Delivery is at least once: a crash after
Telegram accepts a message but before the local commit can cause a duplicate.
An exclusive lock prevents overlapping syncs.

For offline development, `sync --events-file /path/to/events.json` accepts a JSON
array of Ticketmaster event objects. This mode cannot be combined with `--send`.
Use a separate configuration/state database for synthetic events.

## Failures

Failures are reported, not only logged:

- A failed run (all sources down, delivery failure, crash or timeout) triggers
  `music-concerts-failure.service` through `OnFailure=`, which sends a short
  Telegram alert pointing at the journal.
- A run where one source is unavailable still sends the digest, followed by a
  "check incomplete" alert naming the source and its error.
- Every sync writes `run-status.json` beside the report (time, mode, fixed error
  text, unavailable sources). Acquire's Concerts tab shows a failed last run,
  unavailable sources and stale reports, which also covers Telegram itself
  being down.

## Web view

Acquire's Concerts tab (any signed-in web user; not agent tokens) shows
`latest.json` grouped like the digest, with area, name and date-window filters,
long-tour dates collapsed behind "Show N other dates", sources and check times.
Compose mounts `state/concerts` read-only at `/concerts` (override with
`CONCERTS_STATE_DIR`; `concerts_report` in the acquisition config sets the file).
The app reads only that report: never the private configuration, the database
or provider APIs. Reports older than 26 hours are marked stale. The report's
`settings` section carries the lookahead and long-tour threshold; older reports
fall back to 365 days and 3 dates.

## Scheduling

`deployment/music-concerts.service`, `.timer` and `music-concerts-failure.service`
(install all three) are user-systemd templates for
this checkout location. The timer runs daily at 18:00 Europe/Madrid without a
random delay; daylight-saving changes follow Madrid local time. The
service runs a dry-run. Install them only when ready to schedule the worker.
Review the real report before adding `--send` to `ExecStart`. Hermes must be
available in the user service's PATH, with its Telegram destination configured.

## DICE

Used with DICE's permission (confirmed by DICE support, 2026-11-02). DICE's
official API only covers a promoter's own events, so the reader uses the
sitemaps published in `robots.txt` to discover events, then each event page's
schema.org `MusicEvent` data for performers, venue, postcode, start time and
status. It identifies itself in the User-Agent and waits two seconds between
requests.

Each run reads the sitemap index and its shards (about 8 requests), keeps event
URLs naming a Spanish place, and fetches only new pages or pages whose `lastmod`
changed, at most `dice_page_budget` (default 150) per run to stay inside the
service timeout. Pages left over are fetched on later runs and appear as
"pages still to read" for the source; nothing is silently dropped. Results are
cached in `dice-cache.json` beside the state database, saved even when a run
fails. Fill the cache once, outside the timer, with:

```sh
.venv/bin/python -m concert_alerts dice-cache --pages 1500
```

Performer names are matched exactly like RA's linked artists; overrides use
`dice:<artist-slug>` from the DICE artist URL. IDs are `dice:<short-id>`.
Countries are confirmed from the page, and postcodes identify preferred
provinces. DICE marks many ordinary events `EventRescheduled`, so a
rescheduling counts only when a previous start date is given; cancellations
and postponements are used as given. Coverage limit: events whose URL names no
known Spanish place are not read.

`dice_notify` (default `false`) keeps DICE dates in the report and the Concerts
tab but out of Telegram, so matches can be reviewed first. Held dates are not
marked delivered; enabling it sends them in the next digest.

## Matching and coverage

The RA reader uses its unofficial GraphQL endpoint, following the query approach
in [Cuepoint](https://github.com/danilenzo/cuepoint). Attribution and the MIT license
are in `THIRD_PARTY_NOTICES`. It discovers all Spanish areas through the country
list, including Barcelona and the regional North/South/East buckets, then pages
through the date range. It checks result counts, budgets and repeated pages;
incomplete responses never replace the successful cache. Complete results are
cached for six hours in `ra-cache.json` beside the state database.

Only linked RA artist records are matched, with overrides keyed as
`ra:<artist-id>` in `artist_overrides`. Free-text lineups are reported rather than
guessed. Explicit listening-session listings and unnamed TBA venues without an
address are held out of alerts. This is not a guarantee that every RA listing is
a confirmed performance; inspect the event link and report when a name is
ambiguous. Spanish postcodes identify preferred provinces where supplied; broad
regional locations are labelled as RA areas, rather than presented as cities.

RA events use `ra:<event-id>` IDs. Cross-source duplicates need matching local
date/time, city, venue name and a shared library artist; unknown times or TBA
venues stay separate. SQLite stores aliases so the delivery identity survives a
later source outage. Original Ticketmaster IDs and notification history are
preserved. Additional source attribution, title wording or URL changes alone do
not trigger a repeat; real date, time, venue, artist or status changes do. RA's
normal listed status cannot undo a known Ticketmaster cancellation. Aliases and
alternative URLs appear in the report's `duplicates` section.

[Ticketmaster Discovery](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/)
is the initial source. It supports Spain, with a documented default quota of
5,000 calls/day and 5 calls/second. The worker throttles requests, retries bounded
transient failures, budgets requests, splits large date windows to avoid the
1,000-result paging ceiling, and aborts incomplete fetches before notification.

Artist matching uses MusicBrainz IDs when supplied, otherwise unique normalised
names. Ambiguous names need an `artist_overrides` entry mapping a Ticketmaster
attraction ID to a Navidrome artist ID. Names are not fuzzily matched, split on
commas or inferred from event titles. Combined credits remain intact. This can
miss genuine matches; same-name artists without external IDs still need a human
coverage check. Override an incorrect match before enabling delivery.

Only confirmed Spanish venue countries are included; missing province data does
not exclude them. Preferred areas are highlighted from supplied city/province
names and Spanish province/region codes. No coordinate-to-region lookup is
implemented yet. A venue outside the known cities with no usable province data
may lack a highlight and be hidden by the long-tour filter; its event remains in
the full report for inspection.

Coverage is incomplete: other ticket platforms, missing artist lineups, undated
announcements and ongoing events that started before the search window may be
missed. A festival listing identifies only its supplied artists; it does not
prove a complete lineup or an artist's exact performance day. Duplicate provider
IDs are removed, but separately sold ticket variants with different IDs can
still appear separately. Multi-day appearances are preserved.

Before relying on alerts, compare a representative artist/event sample with
official dates. Add permitted venue/promoter/festival feeds to fill actual gaps,
especially in Galicia and electronic music. Missing results mean unknown
coverage, not no concerts.

[Bandsintown](https://help.artists.bandsintown.com/en/articles/7053475-what-is-the-bandsintown-api)
restricts keys to artists/their authorised teams; broader use requires specific
approval. [Songkick](https://www.songkick.com/developer) requires paid licensing
and excludes hobbyist applications. Neither is a launch dependency.

Additional sources evaluated on 2026-11-02; none is connected yet:

- [Mardi Gras RSS](https://www.salamardigras.com/feed/) returned a valid RSS feed
  with six posts. Combine it with the venue's [upcoming-ticket page](https://www.salamardigras.com/entradas-para-conciertos-proximos-meses/)
  and linked event pages: post publication dates are not performance dates, and
  a feed alone is not a complete future calendar. Start with this small Galicia
  adapter before adding more venue calendars.
- [Ataquilla](https://entradas.ataquilla.com/es/ventaentradas/agenda) is a Galicia
  candidate. Search indexed concerts, but the fetched agenda returned no results
  and an older category URL redirected to a 404. Its listing retrieval still
  needs investigation; do not treat an empty response as an empty calendar.
- [Official festival lineups](https://www.resurrectionfest.es/en/tag/line-up/)
  can fill gaps in ticket-provider artist lists. Keep festival dates distinct
  from unconfirmed individual performance days.
- Entradium event-page fetches returned 403 in this check. Keep it as a
  candidate rather than assuming an available public API or reliable scraper.

Prefer RSS, JSON-LD or public page data, with caching and low request rates. Each
adapter should produce the same event/artist/venue records and retain its source
URL. Add conservative cross-source duplicate handling before allowing a new
provider to send notifications. Source failures must not turn existing events
into cancellations.

## Verification

```sh
uv run --project concerts --frozen pytest -q concerts/tests
```

Tests use isolated libraries, HTTP fixtures and fake notification senders. They
cover identity ambiguity, country filtering, festival and tour grouping, message
splitting, paging limits, incomplete provider responses, dry-run state and
notification retries.

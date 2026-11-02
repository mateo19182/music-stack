import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import select_events, select_ra_events
from .library import ArtistIndex, library_catalogue
from .notify import deliver, select_digest_dates
from .state import State, run_lock
from .ticketmaster import ProviderError, Ticketmaster
from .ra import ResidentAdvisor


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config/concerts/config.json"


def read_config(path):
    if path.stat().st_mode & 0o077:
        raise ValueError("Concert configuration must be private (chmod 600)")
    data = json.loads(path.read_text())
    data["max_nationwide_dates"] = int(data.get("max_nationwide_dates", 3))
    if not isinstance(data.get("ra_enabled", False), bool):
        raise ValueError("ra_enabled must be a JSON boolean")
    if data["max_nationwide_dates"] < 0:
        raise ValueError("max_nationwide_dates must be non-negative")
    base = path.parent
    for key, default in (("navidrome_db", "../navidrome/navidrome.db"),
                         ("state_db", "../../state/concerts/events.db"),
                         ("report", "../../state/concerts/latest.json")):
        value = Path(data.get(key, default))
        data[key] = value if value.is_absolute() else (base / value).resolve()
    return data


def sync(config, raw_events=None, send=False, provider_factory=Ticketmaster, ra_factory=ResidentAdvisor):
    artists, library_issues = library_catalogue(config["navidrome_db"])
    index = ArtistIndex(artists, config.get("artist_overrides"))
    if not index.artists:
        raise ValueError("No published library artists found; sync stopped")
    with run_lock(config["state_db"]):
        requests = 0
        ra_events, sources = [], {}
        start = datetime.now(timezone.utc).replace(microsecond=0)
        end = start + timedelta(days=int(config.get("lookahead_days", 365)))
        if raw_events is None:
            raw_events = []
            provider = None
            try:
                provider = provider_factory(config.get("ticketmaster_api_key", ""))
                raw_events = provider.events(start, end)
                sources["Ticketmaster"] = {"status": "ok", "events": len(raw_events)}
            except (ProviderError, ValueError) as error:
                sources["Ticketmaster"] = {"status": "unavailable", "error": str(error)}
            finally:
                if provider:
                    requests += getattr(provider, "requests", 0)
                    provider.close()
            if config.get("ra_enabled", False):
                provider = ra_factory(cache_path=Path(config["state_db"]).parent / "ra-cache.json")
                try:
                    ra_events = provider.events(start, end)
                    sources["Resident Advisor"] = {"status": "ok", "events": len(ra_events),
                                                    "cached": provider.cache_used, "areas": provider.areas}
                except (ProviderError, OSError) as error:
                    message = str(error) if isinstance(error, ProviderError) else "RA cache unavailable"
                    sources["Resident Advisor"] = {"status": "unavailable", "error": message}
                finally:
                    requests += provider.requests
                    provider.close()
            if not any(source["status"] == "ok" for source in sources.values()):
                raise ProviderError("All configured event sources are unavailable; no notifications sent")
        events, unresolved, unknown_locations = select_events(raw_events, index)
        ra_matches, ra_unresolved, ra_locations = select_ra_events(ra_events, index, start, end)
        events.extend(ra_matches)
        unresolved.extend(ra_unresolved)
        unknown_locations.extend(ra_locations)
        state = State(config["state_db"])
        try:
            events, duplicates = state.reconcile(events)
            events.sort(key=lambda e: (not bool(e["highlight"]), e["date"], e["name"], e["id"]))
            pending = state.observe(events)
            visible, hidden = select_digest_dates(pending, events, config.get("max_nationwide_dates", 3))
            report = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "mode": "send" if send else "dry-run", "library_artists": len(index.artists),
                "provider_events": len(raw_events) + len(ra_events), "provider_requests": requests,
                "matching_events": len(events), "pending_notifications": len(visible),
                "hidden_notifications": len(hidden),
                "events": events, "unresolved_artists": unresolved,
                "library_issues": library_issues,
                "unknown_locations": unknown_locations,
                "sources": sources, "duplicates": duplicates,
                # Non-secret settings readers need to reproduce the digest view.
                "settings": {"lookahead_days": int(config.get("lookahead_days", 365)),
                             "max_nationwide_dates": int(config.get("max_nationwide_dates", 3))},
                "coverage": "Only successful sources are included; unlinked artists and other event platforms may be missing.",
                "sent_events": 0,
            }
            # Save the reviewable report before any external delivery.
            write_report(config["report"], report)
            if send:
                report["sent_events"] = deliver(
                    visible, state, lookahead_days=int(config.get("lookahead_days", 365)),
                    catalogue=events, max_nationwide_dates=config.get("max_nationwide_dates", 3))
                write_report(config["report"], report)
            return report
        finally:
            state.close()


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Library concert alerts throughout Spain; dry-run by default")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("library", help="Check read-only library access")
    run = sub.add_parser("sync", help="Find matching events and write a private report")
    run.add_argument("--send", action="store_true", help="Deliver pending events through Hermes to Telegram")
    run.add_argument("--events-file", type=Path, help="Offline Ticketmaster event array; cannot be used with --send")
    args = parser.parse_args(argv)
    if args.command == "sync" and args.send and args.events_file:
        parser.error("--events-file is dry-run only")
    try:
        config = read_config(args.config)
        if args.command == "library":
            artists, issues = library_catalogue(config["navidrome_db"])
            print(json.dumps({"library_artists": len(artists), "library_issues": issues,
                              "sample": [a.name for a in artists[:10]]}, ensure_ascii=False))
            return 0
        if not 1 <= int(config.get("lookahead_days", 365)) <= 730:
            raise ValueError("lookahead_days must be between 1 and 730")
        raw = json.loads(args.events_file.read_text()) if args.events_file else None
        if raw is not None and not isinstance(raw, list):
            raise ValueError("Event fixture must be a JSON array")
        report = sync(config, raw, args.send)
        print(json.dumps({k: report[k] for k in ("mode", "library_artists", "provider_events", "matching_events", "pending_notifications", "sent_events")}))
        print(f"Report: {config['report']}")
        return 0
    except ProviderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error, RuntimeError, KeyError, TypeError):
        # HTTP exceptions and malformed configuration can contain credential-bearing URLs.
        print("Concert sync failed. Check private configuration, library access and provider availability; no incomplete fetch is notified.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

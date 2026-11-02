import re
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


def name_key(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class Artist:
    id: str
    name: str
    mbid: str = ""


def library_catalogue(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute("SELECT id, name, mbz_artist_id FROM artist WHERE missing=0").fetchall()
    excluded = {"various artists", "various", "va", "unknown artist", "[unknown]"}
    artists, issues = [], []
    for id, name, mbid in rows:
        if not name or name_key(name) in excluded:
            continue
        if re.match(r"^(?:https?:|www\.|(?:t|telegram)\.me(?:/|$))", name_key(name)):
            issues.append({"id": str(id), "name": name, "reason": "URL-like artist tag"})
        else:
            artists.append(Artist(str(id), name, mbid or ""))
    return artists, issues


def read_library(path):
    return library_catalogue(path)[0]


class ArtistIndex:
    def __init__(self, artists, overrides=None):
        self.artists = {a.id: a for a in artists}
        self.names = defaultdict(set)
        self.mbids = defaultdict(set)
        self.overrides = overrides or {}
        for artist in artists:
            self.names[name_key(artist.name)].add(artist.id)
            if artist.mbid:
                self.mbids[artist.mbid.casefold()].add(artist.id)
        if any(id not in self.artists for id in self.overrides.values()):
            raise ValueError("An artist override refers to an artist outside the library")

    def match(self, attraction):
        provider_id = str(attraction.get("id", ""))
        if provider_id in self.overrides:
            return self.artists[self.overrides[provider_id]], None
        # Use IDs when provided; a conflicting ID must never fall back to a name.
        mbids = attraction.get("externalLinks", {}).get("musicbrainz", [])
        ids = set()
        for link in mbids:
            mbid = str(link.get("id") or link.get("url", "").rstrip("/").rsplit("/", 1)[-1])
            ids.update(self.mbids.get(mbid.casefold(), ()))
        if not mbids:
            ids = self.names.get(name_key(attraction.get("name", "")), set())
        if len(ids) == 1:
            return self.artists[next(iter(ids))], None
        reason = "ambiguous" if len(ids) > 1 else "unmatched"
        return None, {"provider_id": provider_id, "name": attraction.get("name", ""), "reason": reason}

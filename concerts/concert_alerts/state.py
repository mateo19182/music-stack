import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .events import fingerprint, notification_signature, same_show


@contextmanager
def run_lock(database):
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another concert sync is running") from None
        yield


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
            notified TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS aliases (source_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def reconcile(self, events):
        known = {id: json.loads(payload) for id, payload in self.db.execute("SELECT id,payload FROM events")}
        aliases = dict(self.db.execute("SELECT source_id,canonical_id FROM aliases"))
        current, duplicates = {}, []
        with self.db:
            for event in events:
                source_id = event["id"]
                canonical_id = aliases.get(source_id, source_id)
                if canonical_id == source_id and source_id not in known:
                    candidates = {**known, **current}
                    matches = [candidate for candidate in candidates.values() if same_show(event, candidate)]
                    if len(matches) == 1:
                        canonical_id = matches[0]["id"]
                        self.db.execute("INSERT OR REPLACE INTO aliases VALUES(?,?)", (source_id, canonical_id))
                        aliases[source_id] = canonical_id
                event = {**event, "id": canonical_id}
                if event["status"] == "listed" and canonical_id in known:
                    event["status"] = known[canonical_id]["status"]
                if canonical_id != source_id:
                    duplicates.append({"source_id": source_id, "canonical_id": canonical_id, "url": event["url"]})
                if canonical_id in current:
                    current[canonical_id]["artists"] = dict(sorted({**current[canonical_id]["artists"], **event["artists"]}.items()))
                else:
                    current[canonical_id] = event
        return list(current.values()), duplicates

    def observe(self, events):
        pending = []
        with self.db:
            for event in events:
                digest = fingerprint(event)
                previous = self.db.execute("SELECT notified,fingerprint,payload FROM events WHERE id=?", (event["id"],)).fetchone()
                notified = previous[0] if previous else None
                if (previous and notified == previous[1]
                        and notification_signature(json.loads(previous[2])) == notification_signature(event)):
                    notified = digest
                if notified != digest:
                    pending.append((event, bool(notified), digest))
                self.db.execute("""INSERT INTO events(id,payload,fingerprint) VALUES(?,?,?)
                    ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                    fingerprint=excluded.fingerprint, updated_at=CURRENT_TIMESTAMP""",
                    (event["id"], json.dumps(event, ensure_ascii=False), digest))
                if notified is not None:
                    self.db.execute("UPDATE events SET notified=? WHERE id=?", (notified, event["id"]))
        return pending

    def delivered(self, entries):
        with self.db:
            for event, _, digest in entries:
                self.db.execute("UPDATE events SET notified=? WHERE id=?", (digest, event["id"]))

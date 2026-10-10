"""Durable job and file records; workers claim work atomically."""

import contextlib, json, sqlite3, time, uuid
from pathlib import Path


def uid():
    return uuid.uuid4().hex


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("pragma journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS searches (id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, stage TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, job_id TEXT, owner TEXT, published INTEGER NOT NULL DEFAULT 0, path TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_stage ON jobs(stage,created_at);
            CREATE TABLE IF NOT EXISTS wishlist (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, table, id, data, **columns):
        if table not in {"sessions", "searches", "candidates", "jobs", "files", "wishlist"}:
            raise ValueError(table)
        values = {"id": id, **columns, "data": json.dumps(data, ensure_ascii=False)}
        with self.db() as db:
            db.execute(
                f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)}) ON CONFLICT(id) DO UPDATE SET "
                + ",".join(f"{k}=excluded.{k}" for k in values if k != "id"),
                list(values.values()),
            )

    def get(self, table, id):
        if table not in {"sessions", "searches", "candidates", "jobs", "files", "wishlist"}:
            raise ValueError(table)
        with self.db() as db:
            r = db.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone()
        return self.unpack(r)

    @staticmethod
    def unpack(row):
        if row is None:
            return None
        row = dict(row)
        return {**json.loads(row.pop("data")), **row}

    def list(self, table, where="1", params=(), order="id"):
        if table not in {"sessions", "searches", "candidates", "jobs", "files", "wishlist"}:
            raise ValueError(table)
        with self.db() as db:
            rows = db.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", params
            ).fetchall()
        return [self.unpack(r) for r in rows]

    def update_job(self, id, **updates):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            stage = updates.pop("stage", row["stage"])
            data.update(updates)
            db.execute(
                "UPDATE jobs SET stage=?,data=? WHERE id=?",
                (stage, json.dumps(data), id),
            )

    def claim(self, stages=("queued", "process_queued", "publish_queued"), paused=()):
        """Take the oldest job in these stages; downloads from a paused source wait."""
        paused = list(paused)
        skip = (" AND NOT (stage='queued' AND json_extract(data, '$.candidate.source') IN ("
                + ",".join("?" for _ in paused) + "))") if paused else ""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE stage IN ("
                + ",".join("?" for _ in stages)
                + ")" + skip + " ORDER BY created_at LIMIT 1",
                list(stages) + paused,
            ).fetchone()
            if not row:
                return None
            stage = {
                "queued": "downloading",
                "process_queued": "processing",
                "publish_queued": "publishing",
            }[row["stage"]]
            db.execute("UPDATE jobs SET stage=? WHERE id=?", (stage, row["id"]))
            job = self.unpack(row)
            job["stage"] = stage
            return job

    def recover(self):
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET stage=CASE stage WHEN 'downloading' THEN 'queued' WHEN 'processing' THEN 'process_queued' WHEN 'publishing' THEN 'publish_queued' END WHERE stage IN ('downloading','processing','publishing')"
            )
            db.execute(
                "UPDATE searches SET status='failed',data=json_set(data,'$.error','Search interrupted by restart; search again') WHERE status='searching'"
            )
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))

    def transition_job(self, id, expected, stage, **updates):
        """Reject stale UI actions rather than racing an active worker."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
            if not row or row["stage"] not in expected:
                return False
            data = json.loads(row["data"])
            data.update(updates)
            db.execute(
                "UPDATE jobs SET stage=?,data=? WHERE id=?",
                (stage, json.dumps(data), id),
            )
            return True

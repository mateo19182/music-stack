"""Authenticated acquisition with durable download/ingestion workers and review."""

from __future__ import annotations
import contextlib, hashlib, json, logging, os, secrets, sqlite3, threading, time, zipfile
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from mediafile import MediaFile

from .store import Store, uid, now
from .sources import Sources, SourceError, DownloadCancelled, youtube_url
from .ingestion import Ingestor, IngestionCancelled
from .sharing import Sharing
from .llm_review import Reviewer, ReviewError
from .review_summary import summarize

log = logging.getLogger("acquisition")
CONFIG_PATH = Path(os.environ.get("ACQUISITION_CONFIG", "/state/config.json"))
config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
STATE = Path(config.get("state_root", "/state"))
LIBRARY = Path(config.get("library_root", "/library")).resolve()
STAGING = Path(config.get("staging_root", "/staging")).resolve()
STATE.mkdir(parents=True, exist_ok=True)
LIBRARY.mkdir(parents=True, exist_ok=True)
STAGING.mkdir(parents=True, exist_ok=True)
store = Store(STATE / "acquisition.db")
stop = threading.Event()
worker_threads = []
reviewer = Reviewer(config)
advice_lock = threading.Lock()


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=300)


class Search(BaseModel):
    query: str = Field(default="", max_length=300)
    source: str = "all"
    kind: str = "track"
    artist: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    album: str = Field(default="", max_length=200)


class Enqueue(BaseModel):
    candidate_id: str
    artist: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    album: str = Field(default="", max_length=200)


class URLInput(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    kind: str = "track"
    artist: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    album: str = Field(default="", max_length=200)


class Retry(BaseModel):
    stage: str = "download"


class Approval(BaseModel):
    files: list[dict] = Field(default_factory=list, max_length=500)
    keep_existing: bool = False
    selected_file_ids: list[str] | None = Field(default=None, max_length=500)


class SharingConfig(BaseModel):
    enabled: bool


class FileSharing(BaseModel):
    shared: bool


class Import(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)


class AgentCredential(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def scan_shares():
    response = httpx.put(
        config.get("slskd_url", "http://slskd:5030").rstrip("/") + "/api/v0/shares",
        headers={"X-API-KEY": config.get("slskd_api_key", "")}, timeout=5,
    )
    if response.status_code == 409:
        return False
    response.raise_for_status()
    return True


sharing = Sharing(LIBRARY, config.get("share_root", str(STATE / "shared")), STATE,
                  scan=scan_shares if config.get("slskd_api_key") else None,
                  link_library=config.get("sharing_source_root"))


def sharing_worker():
    while not stop.is_set():
        try:
            sharing.refresh()
        except Exception:
            log.exception("Could not synchronize Soulseek sharing")
        stop.wait(15)


def visible(user, record):
    return record.get("owner") == user["username"] or user.get("isAdmin", False)


def check_user(request: Request, bearer_credentials=Depends(HTTPBearer(auto_error=False))):
    authorization = request.headers.get("authorization", "")
    bearer = authorization.lower().startswith("bearer ")
    token = authorization[7:].strip() if bearer else request.cookies.get("acquire_session", "")
    s = (
        store.get("sessions", hashlib.sha256(token.encode()).hexdigest())
        if token
        else None
    )
    if not s or s["expires"] < time.time():
        raise HTTPException(401, "Sign in with your Navidrome account")
    if bearer and s.get("kind") != "agent":
        raise HTTPException(401, "Use a dedicated agent token")
    if not bearer and s.get("kind") == "agent":
        raise HTTPException(401, "Agent tokens require Bearer authentication")
    return s


def require_manual(user):
    if user.get("kind") == "agent":
        raise HTTPException(403, "This action requires manual approval in the web app")


def owned_job(id, user):
    job = store.get("jobs", id)
    if not job or not visible(user, job):
        raise HTTPException(404, "Job not found")
    return job


def album_id(path):
    return hashlib.sha256(
        str(Path(path).parent.relative_to(LIBRARY)).encode()
    ).hexdigest()[:24]


navidrome_links = {}
navidrome_links_checked = 0.0
navidrome_links_lock = threading.Lock()


def navidrome_public_url():
    return config.get("navidrome_public_url", "http://localhost:4533")


def navidrome_link(path):
    global navidrome_links, navidrome_links_checked
    with navidrome_links_lock:
        if time.monotonic() - navidrome_links_checked > 10:
            try:
                db = sqlite3.connect(
                    f"file:{config.get('navidrome_db', '/navidrome/navidrome.db')}?mode=ro",
                    uri=True,
                )
                navidrome_links = dict(
                    db.execute(
                        "SELECT path,album_id FROM media_file WHERE missing=0 AND library_id=1"
                    )
                )
                db.close()
            except sqlite3.Error:
                navidrome_links = {}
            navidrome_links_checked = time.monotonic()
    root = navidrome_public_url()
    try:
        album = navidrome_links.get(str(Path(path).relative_to(LIBRARY)))
    except ValueError:
        album = None
    return root + "/app/#/album/" + album + "/show" if album else root


def file_view(record):
    omit = {
        "path",
        "prepared_path",
        "duplicate_path",
        "source_path",
        "candidate",
        "owner",
        "job_id",
        "original_sha256",
        "sha256",
    }
    result = {k: v for k, v in record.items() if k not in omit}
    result["shared"] = bool(record.get("published") and Path(record["path"]).resolve().is_relative_to(LIBRARY) and sharing.selected(record["path"]))
    result["bitrate"] = round((record.get("bitrate") or 0) / 1000)
    if isinstance(result.get("analysis_source"), dict):
        result["analysis_source"] = (
            ", ".join(f"{k}: estimated" for k in result["analysis_source"])
            or "existing tags"
        )
    result["possible_duplicates"] = [
        {
            **{k: v for k, v in duplicate.items() if k != "path"},
            "id": hashlib.sha256(str(duplicate["path"]).encode()).hexdigest()[:32],
        }
        if duplicate.get("path")
        else duplicate
        for duplicate in record.get("possible_duplicates", [])
    ]
    if record.get("duplicate_path"):
        duplicate_id = hashlib.sha256(
            str(record["duplicate_path"]).encode()
        ).hexdigest()[:32]
        existing_file = store.get("files", duplicate_id)
        if existing_file:
            result["duplicate_of"] = (
                (existing_file.get("title") or "Existing track")
                + " · "
                + (existing_file.get("album") or "Unknown album")
            )
            result["duplicate_file_id"] = duplicate_id
    result["existing"] = record.get("existing_tags", {})
    result["proposed"] = record.get("proposed_tags", {})
    result["navidrome_url"] = (
        navidrome_link(record["path"]) if record.get("published") else None
    )
    return result


def job_view(job):
    result = {
        k: v
        for k, v in job.items()
        if k
        not in {"candidate", "paths", "prepared", "edits", "owner", "cancel_requested", "selected_paths"}
    }
    file_ids = (
        job.get("published_file_ids")
        if job["stage"] == "published"
        else job.get("prepared_file_ids")
    )
    files = (
        [store.get("files", id) for id in file_ids]
        if file_ids
        else store.list("files", "job_id=?", (job["id"],))
    )
    files = [f for f in files if f]
    result["files"] = [file_view(f) for f in files]
    candidate = job.get("candidate", {})
    result["request"] = {
        k: candidate.get(k)
        for k in [
            "requested_artist",
            "requested_title",
            "requested_album",
            "source_title",
            "source_metadata",
            "source_files",
            "provider",
        ]
        if candidate.get(k)
    }
    if job.get("stage") == "review":
        result["review_summary"] = summarize(job, result["files"])
    return result


def register_files(records, job, published):
    ids = []
    for record in records:
        path = Path(record["path"]).resolve()
        id = hashlib.sha256(str(path).encode()).hexdigest()[:32]
        ids.append(id)
        data = {**record, "id": id, "filename": path.name}
        if published:
            data["album_id"] = album_id(path)
        store.put(
            "files",
            id,
            data,
            job_id=job["id"],
            owner=job["owner"],
            published=int(published),
            path=str(path),
        )

    store.update_job(
        job["id"], **{("published_file_ids" if published else "prepared_file_ids"): ids}
    )


def index_library():
    """Use Navidrome's measured tags for existing music; never rewrite audio."""
    database = Path(config.get("navidrome_db", "/navidrome/navidrome.db"))
    if not database.exists():
        return
    try:
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM media_file WHERE missing=0 AND library_id=1"
        ).fetchall()
        db.close()
        for row in rows:
            row = dict(row)
            path = (LIBRARY / row["path"]).resolve()
            if not path.is_relative_to(LIBRARY) or not path.is_file():
                continue
            id = hashlib.sha256(str(path).encode()).hexdigest()[:32]
            existing = store.get("files", id)
            if existing:
                precise_bpm = MediaFile(str(path)).bpm_precise or None
                if precise_bpm != existing.get("bpm"):
                    data = {
                        k: v
                        for k, v in existing.items()
                        if k not in {"id", "path", "job_id", "owner", "published"}
                    }
                    data["bpm"] = precise_bpm
                    store.put(
                        "files",
                        id,
                        data,
                        path=str(path),
                        job_id=existing.get("job_id"),
                        owner=existing.get("owner"),
                        published=existing["published"],
                    )
                continue
            tags = json.loads(row.get("tags") or "{}")
            keys = tags.get("key", [])
            record = {
                "id": id,
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "genre": row["genre"],
                "bpm": MediaFile(str(path)).bpm_precise or None,
                "key": keys[0]["value"] if keys else None,
                "format": row.get("codec") or row["suffix"],
                "bitrate": row["bit_rate"] * 1000,
                "duration": row["duration"],
                "size": row["size"],
                "filename": path.name,
                "album_id": album_id(path),
                "analysis_source": {},
                "navidrome_url": navidrome_public_url()
                + "/app/#/album/"
                + row["album_id"]
                + "/show",
            }
            store.put(
                "files",
                id,
                record,
                path=str(path),
                job_id=None,
                owner=None,
                published=1,
            )
    except sqlite3.Error:
        log.exception("Could not index Navidrome library")


def request_scan():
    creds = config.get("navidrome_scan", {})
    if not creds:
        return "Published; Navidrome will pick up changes on its scheduled scan"
    salt = secrets.token_hex(8)
    params = {
        "u": creds["username"],
        "t": hashlib.md5((creds["password"] + salt).encode()).hexdigest(),
        "s": salt,
        "v": "1.16.1",
        "c": "acquire",
        "f": "json",
    }
    try:
        r = httpx.get(
            config.get("navidrome_url", "http://navidrome:4533")
            + "/rest/startScan.view",
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        if r.json()["subsonic-response"]["status"] != "ok":
            raise ValueError("Scan rejected")
        return "Published; Navidrome scan requested"
    except (httpx.HTTPError, ValueError, KeyError):
        return "Published; scan request failed, scheduled scan will retry"


def do_search(id, user, body):
    sources = Sources(config)
    try:
        query = body.query.strip() or " ".join(
            x for x in [body.artist, body.title, body.album] if x
        )
        results = sources.search(query, body.source, body.kind)
        for candidate in results:
            candidate["id"] = hashlib.sha256(
                (id + candidate["id"]).encode()
            ).hexdigest()[:32]
            candidate.update(
                requested_artist=body.artist,
                requested_title=body.title,
                requested_album=body.album,
            )
            store.put("candidates", candidate["id"], candidate, owner=user["username"])
        record = {
            "results": results,
            "error": " ".join(sources.last_errors.values()) or None,
        }
        store.put("searches", id, record, owner=user["username"], status="done")
    except Exception as exc:
        message = (
            str(exc) if isinstance(exc, SourceError) else "Search failed. Try again."
        )
        store.put(
            "searches",
            id,
            {"results": [], "error": message},
            owner=user["username"],
            status="failed",
        )


def worker(stages=("queued", "process_queued", "publish_queued")):
    sources = Sources(config)
    ingestor = Ingestor(config)
    while not stop.is_set():
        job = store.claim(stages)
        if not job:
            stop.wait(1)
            continue
        id = job["id"]
        failed_stage = {"downloading": "download", "processing": "processing", "publishing": "publishing"}[job["stage"]]

        def cancelled():
            latest = store.get("jobs", id)
            return stop.is_set() or not latest or bool(latest.get("cancel_requested"))

        def progress(value):
            if isinstance(value, dict):
                updates = {
                    "detail": value.get("message"),
                    "progress": value.get("percent"),
                }
                if value.get("candidate"):
                    updates["candidate"] = value["candidate"]
                    if value["candidate"].get("source_title"):
                        updates["label"] = value["candidate"]["source_title"]
            else:
                updates = {"detail": str(value), "progress": None}
            store.update_job(id, **updates)

        try:
            if job["stage"] == "downloading":
                paths = sources.download(
                    job["candidate"], STAGING / id, progress, cancelled
                )
                if cancelled():
                    raise DownloadCancelled("Cancelled")
                store.update_job(
                    id,
                    stage="process_queued",
                    paths=[str(p) for p in paths],
                    detail="Downloaded; waiting for processing",
                    progress=100,
                )
            elif job["stage"] == "processing":
                prepared = ingestor.prepare(
                    job["paths"], job["candidate"], id, progress, cancelled
                )
                if cancelled():
                    raise IngestionCancelled("Cancelled before review")
                register_files(prepared, job, False)
                store.update_job(
                    id,
                    stage="review",
                    prepared=prepared,
                    error=None,
                    detail="Check the audio and metadata before publishing",
                    progress=100,
                )
            else:
                prepared = job["prepared"]
                if job.get("selected_paths") is not None:
                    prepared = [r for r in prepared if r["path"] in job["selected_paths"]]
                    if not prepared:
                        raise ValueError("No selected tracks remain available for publication")
                records = ingestor.publish(
                    prepared, job.get("edits", {}), id, progress, cancelled
                )
                register_files(records, job, True)
                with store.db() as db:
                    db.execute(
                        "DELETE FROM files WHERE job_id=? AND published=0", (id,)
                    )
                store.update_job(
                    id,
                    stage="published",
                    detail=request_scan(),
                    error=None,
                    progress=100,
                )
        except (DownloadCancelled, IngestionCancelled):
            if stop.is_set():
                store.update_job(
                    id,
                    stage={
                        "downloading": "queued",
                        "processing": "process_queued",
                        "publishing": "publish_queued",
                    }[job["stage"]],
                    detail="Interrupted; resumes after restart",
                )
            else:
                store.update_job(
                    id,
                    stage="cancelled",
                    detail="Cancelled; downloaded originals retained",
                )
        except Exception as exc:
            log.exception("Job %s failed at %s", id, failed_stage)
            message = (
                str(exc)[:600]
                if isinstance(exc, (SourceError, ValueError, RuntimeError))
                else "Processing failed; retained files can be retried"
            )
            store.update_job(
                id,
                stage="failed",
                failed_stage=failed_stage,
                resume_stage=job["stage"],
                error=message,
                detail="Files and progress retained",
            )


def advice_payload(job):
    view = job_view(job)
    candidate = job.get("candidate", {})
    return {"label": view.get("label"), "source": view.get("source"),
            **{key: candidate.get(key) for key in ("kind", "file_count", "folder_complete", "requested_artist", "requested_title", "requested_album")},
            "files": [{**file, "duplicate": bool(file.get("duplicate") or file.get("duplicate_file_id") or file.get("duplicate_of"))}
                      for file in view["files"]]}


def queue_advice(job):
    """Cache advice against its evidence, independently of publication."""
    payload = advice_payload(job)
    fingerprint = hashlib.sha256(json.dumps({"model": reviewer.model, "payload": payload}, sort_keys=True, default=str).encode()).hexdigest()
    advice = job.get("advice", {})
    if advice.get("fingerprint") == fingerprint and advice.get("status") in {"queued", "running", "complete"}:
        return advice
    advice = {"status": "queued", "model": reviewer.model, "fingerprint": fingerprint}
    store.update_job(job["id"], advice=advice)
    return advice


def advice_worker():
    while not stop.is_set():
        job = None
        if reviewer.configured:
            with advice_lock:
                for candidate in store.list("jobs", "stage='review'", order="created_at"):
                    if not candidate.get("advice"):
                        queue_advice(candidate)
                        candidate = store.get("jobs", candidate["id"])
                    if candidate.get("advice", {}).get("status") == "queued":
                        job = candidate
                        store.update_job(job["id"], advice={**job["advice"], "status": "running"})
                        break
        if not job:
            stop.wait(3)
            continue
        try:
            result = reviewer.review(advice_payload(job))
            advice = {**job["advice"], "status": "complete", "result": result, "updated_at": now()}
        except Exception as exc:
            advice = {**job["advice"], "status": "failed", "error": str(exc) if isinstance(exc, ReviewError) else "Review advice failed. You can retry or review manually."}
        with advice_lock:
            latest = store.get("jobs", job["id"])
            if latest and latest["stage"] == "review" and latest.get("advice", {}).get("fingerprint") == job["advice"]["fingerprint"]:
                store.update_job(job["id"], advice=advice)


@contextlib.asynccontextmanager
async def lifespan(app):
    global worker_threads
    stop.clear()
    store.recover()
    for job in store.list("jobs", "stage='review'"):
        if job.get("advice", {}).get("status") == "running":
            store.update_job(job["id"], advice={**job["advice"], "status": "queued"})
    Ingestor(config)
    index_library()
    worker_threads = [
        threading.Thread(
            target=worker, args=(("queued",),), name="download-worker", daemon=True
        ),
        threading.Thread(
            target=worker,
            args=(("process_queued", "publish_queued"),),
            name="ingestion-worker",
            daemon=True,
        ),
    ]
    worker_threads.append(threading.Thread(target=sharing_worker, name="sharing-worker", daemon=True))
    worker_threads.append(threading.Thread(target=advice_worker, name="advice-worker", daemon=True))
    for thread in worker_threads:
        thread.start()
    yield
    stop.set()
    for thread in worker_threads:
        thread.join(timeout=10)


app = FastAPI(
    title="Music acquisition", lifespan=lifespan, docs_url=None, redoc_url=None
)
login_attempts = {}
login_lock = threading.Lock()
search_slots = threading.BoundedSemaphore(3)


@app.middleware("http")
async def guards(request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse(
                {"detail": "Use JSON for this request"}, status_code=415
            )
        origin = request.headers.get("origin")
        allowed = {
            config.get("public_url", "http://localhost:4534"),
            "http://127.0.0.1:4534",
            "http://localhost:4534",
            "http://testserver",
        }
        if origin and origin not in allowed:
            return JSONResponse({"detail": "Origin rejected"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith(("/api/", "/static/")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health():
    healthy = bool(worker_threads and all(t.is_alive() for t in worker_threads))
    return JSONResponse(
        {"ok": healthy, "worker_alive": healthy}, status_code=200 if healthy else 503
    )


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    ip = request.headers.get("cf-connecting-ip") or request.client.host
    with login_lock:
        attempts = [t for t in login_attempts.get(ip, []) if t > time.time() - 900]
        if len(attempts) >= 10:
            raise HTTPException(
                429, "Too many sign-in attempts; try again in 15 minutes"
            )
        attempts.append(time.time())
        login_attempts[ip] = attempts
    try:
        r = httpx.post(
            config.get("navidrome_url", "http://navidrome:4533") + "/auth/login",
            json=body.model_dump(),
            timeout=15,
        )
        if r.status_code in {401, 403}:
            raise HTTPException(401, "Invalid username or password")
        r.raise_for_status()
        upstream = r.json()
    except httpx.HTTPError:
        raise HTTPException(
            503, "Navidrome is unavailable; try again shortly"
        ) from None
    data = {"username": upstream["username"], "isAdmin": upstream["isAdmin"]}
    token = secrets.token_urlsafe(32)
    store.put(
        "sessions",
        hashlib.sha256(token.encode()).hexdigest(),
        data,
        expires=time.time() + 86400,
    )
    response.set_cookie(
        "acquire_session",
        token,
        max_age=86400,
        httponly=True,
        samesite="strict",
        secure=config.get("cookie_secure", True),
        path="/",
    )
    return data


@app.get("/api/me")
def me(user=Depends(check_user)):
    return {
        "username": user["username"],
        "isAdmin": user["isAdmin"],
        "navidromeUrl": navidrome_public_url(),
    }


@app.post("/api/agents")
def create_agent(body: AgentCredential, user=Depends(check_user)):
    require_manual(user)
    if not user["isAdmin"]:
        raise HTTPException(403, "Administrator access required")
    token = secrets.token_urlsafe(48)
    id = hashlib.sha256(token.encode()).hexdigest()
    expires = time.time() + 90 * 86400
    store.put("sessions", id, {"username": user["username"], "isAdmin": True,
              "kind": "agent", "name": body.name, "created_at": now()}, expires=expires)
    return {"id": id, "name": body.name, "token": token, "expires": expires,
            "manual_approval_required": True}


@app.get("/api/agents")
def list_agents(user=Depends(check_user)):
    require_manual(user)
    if not user["isAdmin"]:
        raise HTTPException(403, "Administrator access required")
    return {"agents": [{key: agent.get(key) for key in ("id", "name", "username", "expires", "created_at")}
                       for agent in store.list("sessions", "expires>?", (time.time(),)) if agent.get("kind") == "agent"]}


@app.delete("/api/agents/{id}")
def revoke_agent(id: str, user=Depends(check_user)):
    require_manual(user)
    if not user["isAdmin"]:
        raise HTTPException(403, "Administrator access required")
    credential = store.get("sessions", id)
    if not credential or credential.get("kind") != "agent":
        raise HTTPException(404, "Agent credential not found")
    with store.db() as db:
        db.execute("DELETE FROM sessions WHERE id=?", (id,))
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("acquire_session", "")
    with store.db() as db:
        db.execute(
            "DELETE FROM sessions WHERE id=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
    response.delete_cookie("acquire_session", path="/")
    return {"ok": True}


@app.post("/api/search")
def search(body: Search, user=Depends(check_user)):
    if body.source not in {"all", "soulseek", "youtube"} or body.kind not in {
        "track",
        "album",
    }:
        raise HTTPException(400, "Invalid source or kind")
    if not any(x.strip() for x in [body.query, body.artist, body.title, body.album]):
        raise HTTPException(400, "Enter a track, album or search query")
    if not search_slots.acquire(blocking=False):
        raise HTTPException(429, "Search is busy; try again shortly")
    id = uid()
    store.put(
        "searches", id, {"results": []}, owner=user["username"], status="searching"
    )

    def run():
        try:
            do_search(id, user, body)
        finally:
            search_slots.release()

    threading.Thread(target=run, daemon=True).start()
    return {"id": id, "status": "searching", "results": []}


def _search_identity(value):
    """Keep every word, including recording/version qualifiers."""
    import re
    import unicodedata

    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


_SEARCH_VERSION_WORDS = {"live", "remix", "mix", "edit", "instrumental", "acoustic", "demo", "radio", "extended", "dub", "bootleg", "rework", "remaster", "remastered", "version", "karaoke", "cover", "session", "reprise", "vip"}


def _search_library_index(published_files):
    return [(record, _search_identity(record.get("artist")), _search_identity(record.get("title")), _search_identity(record.get("album")), set(_search_identity(record.get("title")).split()) & _SEARCH_VERSION_WORDS) for record in published_files]


def _search_library_matches(candidate, published_files, identity_index=None):
    """Metadata hints only; neither requested identity nor a shared title proves audio identity."""
    identity_index = _search_library_index(published_files) if identity_index is None else identity_index

    def contains(text, phrase):
        return bool(phrase and f" {phrase} " in f" {text} ")

    def versions(value):
        return set(_search_identity(value).split()) & _SEARCH_VERSION_WORDS

    def match_track(track):
        artist, title = _search_identity(track.get("artist")), _search_identity(track.get("title"))
        album = _search_identity(track.get("album"))
        filename = str(track.get("filename") or "").replace("\\", "/")
        basename = Path(filename).stem if filename else ""
        title_evidence = _search_identity(" ".join(str(v or "") for v in [track.get("title"), basename]))
        source_text = _search_identity(" ".join(str(v or "") for v in [track.get("artist"), track.get("title"), filename]))
        source_version = versions(track.get("title") or basename)
        requested_artist = _search_identity(candidate.get("requested_artist"))
        requested_title = _search_identity(candidate.get("requested_title"))
        bare_title = _search_identity(track.get("title") or basename).split()
        while bare_title and bare_title[0].isdigit():
            bare_title.pop(0)
        bare_title = " ".join(bare_title)
        matches = []
        for record, known_artist, known_title, known_album, known_version in identity_index:
            if not known_artist or not known_title:
                continue
            # A remix/live/edit qualifier in the candidate must not match the plain recording.
            if source_version != known_version or (artist and artist != known_artist):
                continue
            exact = bool(artist and title and artist == known_artist and title == known_title)
            album_conflict = bool(album and record.get("album") and album != known_album)
            named = contains(source_text, known_artist) and contains(title_evidence, known_title)
            requested = requested_artist == known_artist and requested_title == known_title and bare_title == requested_title and (not artist or artist == requested_artist)
            if not exact and not named and not requested:
                continue
            matches.append({
                **{key: record.get(key) for key in ("id", "artist", "title", "album", "format")},
                "bitrate": round((record.get("bitrate") or 0) / 1000),
                "confidence": "exact" if exact and not album_conflict else "possible",
            })
        return matches

    tracks = [{"artist": candidate.get("artist"), "album": candidate.get("album"), **file} for file in candidate.get("files", [])] if candidate.get("kind") == "album" else [candidate]
    tracks = tracks or []
    matched_count = confirmed_count = 0
    unique = {}
    for track in tracks:
        matches = match_track(track)
        matched_count += bool(matches)
        confirmed_count += any(match["confidence"] == "exact" for match in matches)
        for match in matches:
            existing = unique.get(match["id"])
            if not existing or match["confidence"] == "exact":
                unique[match["id"]] = match
    matches = list(unique.values())
    return {
        **candidate,
        "library_matches": matches,
        "library_match": ("exact" if candidate.get("kind") != "album" and any(m["confidence"] == "exact" for m in matches) else "possible") if matches else None,
        "matched_track_count": matched_count,
        "confirmed_track_count": confirmed_count,
        "listed_track_count": len(tracks),
    }


@app.get("/api/search/{id}")
def search_result(id: str, user=Depends(check_user)):
    record = store.get("searches", id)
    if not record or not visible(user, record):
        raise HTTPException(404, "Search not found")
    published = [item for item in store.list("files", "published=1") if Path(item["path"]).is_file()]
    identity_index = _search_library_index(published)
    return {**{k: v for k, v in record.items() if k != "owner"}, "results": [_search_library_matches(candidate, published, identity_index) for candidate in record.get("results", [])]}


enqueue_lock = threading.Lock()


def enqueue(candidate, user):
    with enqueue_lock:
        return _enqueue(candidate, user)


def _enqueue(candidate, user):
    active = store.list(
        "jobs",
        "owner=? AND stage NOT IN ('failed','cancelled','rejected','published')",
        (user["username"],),
    )
    if len(active) >= 100:
        raise HTTPException(409, "Queue limit reached; finish or cancel existing jobs")
    for job in active:
        if job["candidate"].get("id") == candidate.get("id"):
            return job_view(job)
    id = uid()
    job = {
        "candidate": candidate,
        "label": candidate.get("title") or candidate.get("filename") or "Download",
        "source": candidate["source"],
        "progress": 0,
        "detail": "Waiting for worker",
        "error": None,
        "cancel_requested": False,
    }
    store.put("jobs", id, job, owner=user["username"], stage="queued", created_at=now())
    return job_view(store.get("jobs", id))


@app.post("/api/jobs")
def create_job(body: Enqueue, user=Depends(check_user)):
    candidate = store.get("candidates", body.candidate_id)
    if not candidate or not visible(user, candidate):
        raise HTTPException(404, "Search candidate not found")
    candidate = {k: v for k, v in candidate.items() if k != "owner"}
    for name in ["artist", "title", "album"]:
        value = getattr(body, name)
        if value:
            candidate["requested_" + name] = value
    return enqueue(candidate, user)


@app.post("/api/url")
def url_job(body: URLInput, user=Depends(check_user)):
    if body.kind not in {"track", "album"}:
        raise HTTPException(400, "Choose track or album / playlist")
    try:
        url = youtube_url(body.url)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from None
    return enqueue(
        {
            "id": hashlib.sha256((body.kind + url).encode()).hexdigest(),
            "source": "youtube",
            "kind": body.kind,
            "url": url,
            "title": url,
            "files": [],
            **{"requested_" + field: getattr(body, field) for field in ("artist", "title", "album") if getattr(body, field)},
        },
        user,
    )


@app.get("/api/jobs")
def jobs(user=Depends(check_user)):
    records = store.list(
        "jobs",
        "1" if user["isAdmin"] else "owner=?",
        () if user["isAdmin"] else (user["username"],),
        order="created_at DESC LIMIT 100",
    )
    return {"jobs": [job_view(j) for j in records]}


@app.get("/api/jobs/{id}")
def get_job(id: str, user=Depends(check_user)):
    return job_view(owned_job(id, user))


@app.post("/api/jobs/{id}/advice")
def request_advice(id: str, user=Depends(check_user)):
    job = owned_job(id, user)
    if job["stage"] != "review":
        raise HTTPException(409, "Job is not awaiting review")
    if not reviewer.configured:
        raise HTTPException(503, "OpenRouter review advice is not configured")
    with advice_lock:
        latest = owned_job(id, user)
        if latest["stage"] != "review":
            raise HTTPException(409, "Job is not awaiting review")
        return {"advice": queue_advice(latest), "manual_approval_required": True}


@app.get("/api/review")
def review(user=Depends(check_user)):
    records = store.list(
        "jobs",
        "stage='review'" + ("" if user["isAdmin"] else " AND owner=?"),
        () if user["isAdmin"] else (user["username"],),
        order="created_at",
    )
    return {"jobs": [job_view(j) for j in records], "reviewer": {"configured": reviewer.configured,
            "model": reviewer.model, "manual_approval_required": True}}


@app.post("/api/jobs/{id}/cancel")
def cancel(id: str, user=Depends(check_user)):
    job = owned_job(id, user)
    if job["stage"] not in {"queued", "downloading", "process_queued", "processing"}:
        raise HTTPException(409, "This job cannot be cancelled at this stage")
    if not store.transition_job(
        id,
        {job["stage"]},
        "cancelled" if job["stage"] in {"queued", "process_queued"} else job["stage"],
        cancel_requested=True,
    ):
        raise HTTPException(409, "Job changed; refresh and try again")
    return {"ok": True}


@app.post("/api/jobs/{id}/retry")
def retry(id: str, body: Retry, user=Depends(check_user)):
    job = owned_job(id, user)
    if job["stage"] not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled jobs can be retried")
    if body.stage not in {"download", "processing", "publishing"}:
        raise HTTPException(400, "Invalid retry stage")
    if body.stage == "processing" and not job.get("paths"):
        raise HTTPException(409, "No completed download to process")
    stage = (
        "publish_queued"
        if job.get("resume_stage") == "publishing"
        else "process_queued"
        if body.stage == "processing" or job.get("source") == "existing"
        else "queued"
    )
    if not store.transition_job(
        id,
        {"failed", "cancelled"},
        stage,
        cancel_requested=False,
        error=None,
        progress=0,
        retry_count=int(job.get("retry_count", 0)) + 1,
        detail="Retry queued",
    ):
        raise HTTPException(409, "Job was already retried")
    return {"ok": True}


@app.post("/api/jobs/{id}/reject")
def reject(id: str, user=Depends(check_user)):
    require_manual(user)
    job = owned_job(id, user)
    if job["stage"] != "review":
        raise HTTPException(409, "Job is not awaiting review")
    if not store.transition_job(
        id, {"review"}, "rejected", detail="Rejected; downloaded source retained"
    ):
        raise HTTPException(409, "Review was already handled")
    return {"ok": True}


@app.post("/api/jobs/{id}/approve")
def approve(id: str, body: Approval, user=Depends(check_user)):
    require_manual(user)
    job = owned_job(id, user)
    if job["stage"] != "review":
        raise HTTPException(409, "Job is not awaiting review")
    records = store.list("files", "job_id=? AND published=0", (id,))
    allowed = {r["id"]: r for r in records}
    selected = set(body.selected_file_ids) if body.selected_file_ids is not None else set(allowed)
    if not selected or selected - set(allowed):
        raise HTTPException(400, "Select at least one track belonging to this review")
    edits = {}
    for entry in body.files:
        if entry.get("id") not in allowed:
            raise HTTPException(400, "File does not belong to this review")
        changes = entry.get("metadata", {})
        if not isinstance(changes, dict) or set(changes) - {
            "artist",
            "title",
            "album",
            "genre",
            "bpm",
            "key",
        }:
            raise HTTPException(400, "Invalid metadata fields")
        clean = {}
        for k, v in changes.items():
            if k == "bpm":
                if v in (None, ""):
                    v = 0
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, "BPM must be numeric")
                if not 0 <= v <= 400:
                    raise HTTPException(400, "BPM must be between 0 and 400")
            elif not isinstance(v, (str, type(None))) or (v and len(v) > 500):
                raise HTTPException(400, "Metadata text is too long")
            else:
                v = v or ""
            clean[k] = v
        edits[allowed[entry["id"]]["path"]] = clean
    if body.keep_existing:
        edits = {
            r["path"]: {
                k: (r.get("existing_tags", {}).get(k) or (0 if k == "bpm" else ""))
                for k in ["artist", "title", "album", "genre", "bpm", "key"]
            }
            for r in records if r["id"] in selected
        }
    if not store.transition_job(
        id,
        {"review"},
        "publish_queued",
        edits=edits,
        selected_paths=[r["path"] for r in records if r["id"] in selected],
        skipped_count=len(allowed) - len(selected),
        detail="Approved; waiting for publication",
        progress=0,
    ):
        raise HTTPException(409, "Review was already handled")
    return {"ok": True}


@app.get("/api/library")
def library(
    q: str = "",
    genre: str = "",
    key: str = "",
    bpm_min: str = "",
    bpm_max: str = "",
    page: int = 1,
    sort: str = "title",
    user=Depends(check_user),
):
    records = store.list("files", "published=1")
    genres = sorted({r["genre"] for r in records if r.get("genre")})
    keys = sorted({r["key"] for r in records if r.get("key")})
    try:
        lo = float(bpm_min) if bpm_min else None
        hi = float(bpm_max) if bpm_max else None
    except ValueError:
        raise HTTPException(400, "BPM filter must be numeric")
    filtered = []
    for r in records:
        if (
            q
            and q.casefold()
            not in " ".join(
                str(r.get(k) or "") for k in ["title", "artist", "album"]
            ).casefold()
        ):
            continue
        if genre and r.get("genre") != genre:
            continue
        if key and r.get("key") != key:
            continue
        bpm = r.get("bpm")
        if lo is not None and (bpm is None or bpm < lo):
            continue
        if hi is not None and (bpm is None or bpm > hi):
            continue
        filtered.append(r)
    if sort not in {"title", "artist", "album", "genre", "bpm", "key"}:
        sort = "title"
    filtered.sort(
        key=lambda r: (
            r.get(sort) is None,
            (
                float(r.get(sort) or 0)
                if sort == "bpm"
                else str(r.get(sort) or "").casefold()
            ),
        )
    )
    page = max(1, page)
    size = 50
    return {
        "files": [file_view(r) for r in filtered[(page - 1) * size : page * size]],
        "total": len(filtered),
        "page_size": size,
        "genres": genres,
        "keys": keys,
    }


def authorized_file(id, user):
    record = store.get("files", id)
    if not record or (not record["published"] and not visible(user, record)):
        raise HTTPException(404, "File not found")
    path = Path(record["path"]).resolve()
    roots = [LIBRARY, STATE.resolve(), STAGING]
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        raise HTTPException(404, "Audio file unavailable")
    return record, path


@app.get("/api/sharing")
def sharing_status(user=Depends(check_user)):
    return sharing.status()


@app.post("/api/sharing")
def configure_sharing(body: SharingConfig, user=Depends(check_user)):
    require_manual(user)
    if not user["isAdmin"]:
        raise HTTPException(403, "Administrator access required")
    try:
        return sharing.configure(enabled=body.enabled)
    except (OSError, ValueError):
        raise HTTPException(503, "Sharing change saved but could not be applied; the app will retry") from None


@app.post("/api/files/{id}/sharing")
def configure_file_sharing(id: str, body: FileSharing, user=Depends(check_user)):
    require_manual(user)
    if not user["isAdmin"]:
        raise HTTPException(403, "Administrator access required")
    record = store.get("files", id)
    if not record or not record["published"]:
        raise HTTPException(404, "Library file not found")
    path = Path(record["path"]).resolve()
    if not path.is_relative_to(LIBRARY) or not path.is_file():
        raise HTTPException(404, "Library file not found")
    try:
        status = sharing.configure(path=path, shared=body.shared)
        return {"shared": sharing.selected(path), **status}
    except (OSError, ValueError):
        raise HTTPException(503, "Sharing change saved but could not be applied; the app will retry") from None


@app.get("/api/files/{id}/download")
def download(id: str, user=Depends(check_user)):
    r, p = authorized_file(id, user)
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


@app.get("/api/files/{id}/preview")
def preview(id: str, user=Depends(check_user)):
    r, p = authorized_file(id, user)
    return FileResponse(
        p,
        media_type={
            "mp3": "audio/mpeg",
            "m4a": "audio/mp4",
            "flac": "audio/flac",
            "ogg": "audio/ogg",
            "opus": "audio/ogg",
            "wav": "audio/wav",
        }.get(p.suffix.lower().lstrip("."), "application/octet-stream"),
    )


@app.get("/api/albums/{id}/download")
def album_download(id: str, user=Depends(check_user)):
    records = [r for r in store.list("files", "published=1") if r.get("album_id") == id]
    if not records:
        raise HTTPException(404, "Album not found")
    if len(records) > 500:
        raise HTTPException(400, "Album exceeds 500 files")
    export = STATE / "exports"
    export.mkdir(exist_ok=True)
    path = export / (uid() + ".zip")
    try:
        with zipfile.ZipFile(
            path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            for r in records:
                _, p = authorized_file(r["id"], user)
                archive.write(p, arcname=str(p.relative_to(LIBRARY)))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(
        path,
        filename=(records[0].get("album") or "Album").replace("/", "_") + ".zip",
        media_type="application/zip",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/inbox")
def inbox(user=Depends(check_user)):
    if not user["isAdmin"]:
        raise HTTPException(403, "Administrator access required")
    root = Path(config.get("slskd_download_root", "/data/downloads/slskd")).resolve()
    from .sources import AUDIO_EXTENSIONS

    files = [
        {"path": str(p.relative_to(root)), "name": p.name, "size": p.stat().st_size}
        for p in root.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and p.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS
    ]
    return {"files": files[:1000], "total": len(files)}


@app.post("/api/import")
def import_files(body: Import, user=Depends(check_user)):
    if not user["isAdmin"]:
        raise HTTPException(403, "Administrator access required")
    root = Path(config.get("slskd_download_root", "/data/downloads/slskd")).resolve()
    paths = []
    for name in body.paths:
        p = (root / name).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise HTTPException(400, "Import must select a completed inbox file")
        paths.append(str(p))
    id = uid()
    candidate = {"source": "existing"}
    label = "Import " + str(len(paths)) + " files"
    store.put(
        "jobs",
        id,
        {
            "candidate": candidate,
            "paths": paths,
            "label": label,
            "source": "existing",
            "progress": 0,
            "detail": "Existing files queued for processing",
            "cancel_requested": False,
        },
        owner=user["username"],
        stage="process_queued",
        created_at=now(),
    )
    return job_view(store.get("jobs", id))


app.mount(
    "/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"
)


@app.get("/")
def root():
    return FileResponse(Path(__file__).parent / "static/index.html")

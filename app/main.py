"""FastAPI app: panel + API + scheduler."""
import json
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, har, scraper
from .config import ADMIN_PASS, ADMIN_USER, OUTPUT_DIR

STATIC = Path(__file__).parent / "static"
scheduler = BackgroundScheduler(timezone="UTC")
_running: set[int] = set()
_running_lock = threading.Lock()


def _job(site_id: int, trigger: str):
    with _running_lock:
        if site_id in _running:
            return
        _running.add(site_id)
    try:
        scraper.run_site(site_id, trigger)
    finally:
        with _running_lock:
            _running.discard(site_id)


def sync_schedule(site: dict):
    job_id = f"site_{site['id']}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if site["enabled"] and site.get("schedule_minutes"):
        scheduler.add_job(_job, "interval", minutes=site["schedule_minutes"], id=job_id,
                          args=[site["id"], "schedule"], max_instances=1, coalesce=True)


@asynccontextmanager
async def lifespan(app):
    db.init()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    scheduler.start()
    with db.conn() as c:
        for r in c.execute("SELECT * FROM sites"):
            sync_schedule(db.row(r))
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="HAR Scraper", lifespan=lifespan)
security = HTTPBasic()


@app.middleware("http")
async def no_cache(request, call_next):
    # Panel is usually behind a CDN (e.g. ArvanCloud); never let it cache API or panel responses
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, private"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


def auth(cred: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(cred.username, ADMIN_USER) and secrets.compare_digest(cred.password, ADMIN_PASS)):
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است", headers={"WWW-Authenticate": "Basic"})


# ---------- Sites ----------
class SiteIn(BaseModel):
    name: str = Field(min_length=1)
    allowed_domains: list[str] = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    contract_expires: str | None = None
    delay_seconds: float = Field(1.0, ge=0)
    schedule_minutes: int | None = Field(None, ge=5)
    enabled: bool = True
    # None = keep current; a header with empty value is removed
    extra_headers: dict[str, str] | None = None


def _clean_domains(ds):
    out = [d.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0] for d in ds]
    return [d for d in out if d]


def _merge_headers(current: dict, patch: dict | None) -> dict:
    out = dict(current or {})
    for name, value in (patch or {}).items():
        name = name.strip()
        if not name:
            continue
        for k in [k for k in out if k.lower() == name.lower()]:
            del out[k]
        if value.strip():
            out[name] = value.strip()
    return out


def _public(site: dict) -> dict:
    # Header values (cookies/tokens) never go back to the browser; only their names
    s = dict(site)
    s["extra_header_names"] = list((s.pop("extra_headers", None) or {}).keys())
    return s


def _get_site(c, site_id):
    s = db.row(c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())
    if not s:
        raise HTTPException(404, "سایت پیدا نشد")
    return s


@app.get("/api/sites", dependencies=[Depends(auth)])
def list_sites():
    with db.conn() as c:
        sites = [_public(db.row(r)) for r in c.execute("SELECT * FROM sites ORDER BY id DESC")]
        for s in sites:
            s["request_count"] = c.execute("SELECT COUNT(*) FROM requests WHERE site_id=? AND selected=1", (s["id"],)).fetchone()[0]
            last = c.execute("SELECT * FROM runs WHERE site_id=? ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
            s["last_run"] = dict(last) if last else None
            job = scheduler.get_job(f"site_{s['id']}")
            s["next_run"] = job.next_run_time.isoformat() if job and job.next_run_time else None
            s["running"] = s["id"] in _running
    return sites


@app.post("/api/sites", dependencies=[Depends(auth)])
def create_site(body: SiteIn):
    domains = _clean_domains(body.allowed_domains)
    if not domains:
        raise HTTPException(400, "حداقل یک دامنه لازم است")
    with db.conn() as c:
        sid = c.execute(
            "INSERT INTO sites(name, allowed_domains, contract_ref, contract_expires, delay_seconds, schedule_minutes, enabled, extra_headers, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (body.name, json.dumps(domains), body.contract_ref, body.contract_expires or None,
             body.delay_seconds, body.schedule_minutes, int(body.enabled),
             json.dumps(_merge_headers({}, body.extra_headers), ensure_ascii=False), db.now())).lastrowid
        site = _get_site(c, sid)
    sync_schedule(site)
    return _public(site)


@app.put("/api/sites/{site_id}", dependencies=[Depends(auth)])
def update_site(site_id: int, body: SiteIn):
    domains = _clean_domains(body.allowed_domains)
    if not domains:
        raise HTTPException(400, "حداقل یک دامنه لازم است")
    with db.conn() as c:
        old = _get_site(c, site_id)
        c.execute(
            "UPDATE sites SET name=?, allowed_domains=?, contract_ref=?, contract_expires=?, delay_seconds=?, schedule_minutes=?, enabled=?, extra_headers=? WHERE id=?",
            (body.name, json.dumps(domains), body.contract_ref,
             body.contract_expires or None, body.delay_seconds, body.schedule_minutes, int(body.enabled),
             json.dumps(_merge_headers(old["extra_headers"], body.extra_headers), ensure_ascii=False), site_id))
        site = _get_site(c, site_id)
    sync_schedule(site)
    return _public(site)


@app.delete("/api/sites/{site_id}", dependencies=[Depends(auth)])
def delete_site(site_id: int):
    with db.conn() as c:
        c.execute("DELETE FROM sites WHERE id=?", (site_id,))
    if scheduler.get_job(f"site_{site_id}"):
        scheduler.remove_job(f"site_{site_id}")
    return {"ok": True}


# ---------- HAR & requests ----------
@app.post("/api/sites/{site_id}/har", dependencies=[Depends(auth)])
async def upload_har(site_id: int, file: UploadFile = File(...), api_only: bool = Form(True), replace: bool = Form(True)):
    raw = await file.read()
    try:
        data = har.load(raw)
    except har.HarError as e:
        raise HTTPException(400, str(e))
    with db.conn() as c:
        site = _get_site(c, site_id)
        templates, stats = har.extract(data, site["allowed_domains"], api_only=api_only)
        if replace:
            c.execute("DELETE FROM requests WHERE site_id=?", (site_id,))
        for t in templates:
            c.execute("INSERT INTO requests(site_id,label,method,url,headers,body,selected,created_at) VALUES (?,?,?,?,?,?,1,?)",
                      (site_id, t["label"], t["method"], t["url"], json.dumps(t["headers"], ensure_ascii=False), t["body"], db.now()))
    return {"stats": stats}


@app.get("/api/sites/{site_id}/requests", dependencies=[Depends(auth)])
def list_requests(site_id: int):
    with db.conn() as c:
        rows = [db.row(r) for r in c.execute("SELECT * FROM requests WHERE site_id=? ORDER BY id", (site_id,))]
    for r in rows:  # never send secrets back to the browser in full
        r["headers"] = {k: (v[:12] + "…" if k.lower() in ("cookie", "authorization") and len(v) > 12 else v)
                        for k, v in r["headers"].items()}
    return rows


class RequestPatch(BaseModel):
    selected: bool | None = None
    label: str | None = None


@app.patch("/api/requests/{req_id}", dependencies=[Depends(auth)])
def patch_request(req_id: int, body: RequestPatch):
    with db.conn() as c:
        if body.selected is not None:
            c.execute("UPDATE requests SET selected=? WHERE id=?", (int(body.selected), req_id))
        if body.label is not None:
            c.execute("UPDATE requests SET label=? WHERE id=?", (body.label, req_id))
    return {"ok": True}


@app.delete("/api/requests/{req_id}", dependencies=[Depends(auth)])
def delete_request(req_id: int):
    with db.conn() as c:
        c.execute("DELETE FROM requests WHERE id=?", (req_id,))
    return {"ok": True}


# ---------- Runs ----------
@app.post("/api/sites/{site_id}/run", dependencies=[Depends(auth)])
def run_now(site_id: int):
    with db.conn() as c:
        _get_site(c, site_id)
    if site_id in _running:
        raise HTTPException(409, "این سایت در حال اجراست")
    threading.Thread(target=_job, args=(site_id, "manual"), daemon=True).start()
    return {"started": True}


@app.get("/api/sites/{site_id}/runs", dependencies=[Depends(auth)])
def list_runs(site_id: int):
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE site_id=? ORDER BY id DESC LIMIT 50", (site_id,))]


def _run_file(run_id: int) -> Path:
    with db.conn() as c:
        r = c.execute("SELECT output_file FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r or not r["output_file"]:
        raise HTTPException(404, "خروجی برای این اجرا وجود ندارد")
    p = Path(r["output_file"]).resolve()
    if OUTPUT_DIR.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "فایل پیدا نشد")
    return p


@app.get("/api/runs/{run_id}/output", dependencies=[Depends(auth)])
def run_output(run_id: int):
    return json.loads(_run_file(run_id).read_text(encoding="utf-8"))


@app.get("/api/runs/{run_id}/download", dependencies=[Depends(auth)])
def run_download(run_id: int):
    p = _run_file(run_id)
    return FileResponse(p, media_type="application/json", filename=p.name)


# ---------- Panel ----------
@app.get("/", dependencies=[Depends(auth)])
def index():
    return FileResponse(STATIC / "index.html")

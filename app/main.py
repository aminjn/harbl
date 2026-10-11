"""FastAPI app: panel + API + scheduler."""
import csv
import io
import json
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import db, har, scraper
from .config import ADMIN_PASS, ADMIN_USER, OUTPUT_DIR

STATIC = Path(__file__).parent / "static"
scheduler = BackgroundScheduler(timezone="UTC")
_running: set[int] = set()
_running_lock = threading.Lock()


def _job(site_id: int, trigger: str, category_id: int | None = None):
    with _running_lock:
        if site_id in _running:
            return
        _running.add(site_id)
    try:
        scraper.run_site(site_id, trigger, category_id)
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
        # Drop tracker/analytics requests imported by older versions
        junk = [r["id"] for r in c.execute("SELECT id, url FROM requests") if har.is_tracker(r["url"])]
        c.executemany("DELETE FROM requests WHERE id=?", [(i,) for i in junk])
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
    out = [d.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].split(":")[0]
           .removeprefix("www.") for d in ds]
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
            s["category_count"] = c.execute("SELECT COUNT(*) FROM categories WHERE site_id=?", (s["id"],)).fetchone()[0]
            s["item_count"] = c.execute("SELECT COUNT(*) FROM items JOIN categories ON categories.id=items.category_id WHERE site_id=?", (s["id"],)).fetchone()[0]
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


# ---------- Sections (categories) ----------
class CategoryIn(BaseModel):
    name: str = Field(min_length=1)
    max_pages: int = Field(1, ge=1, le=500)


def _get_cat(c, cid):
    cat = db.row(c.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone())
    if not cat:
        raise HTTPException(404, "بخش پیدا نشد")
    return cat


@app.get("/api/sites/{site_id}/categories", dependencies=[Depends(auth)])
def list_categories(site_id: int):
    with db.conn() as c:
        cats = [db.row(r) for r in c.execute("SELECT * FROM categories WHERE site_id=? ORDER BY id", (site_id,))]
        for cat in cats:
            cat["request_count"] = c.execute("SELECT COUNT(*) FROM requests WHERE category_id=? AND selected=1", (cat["id"],)).fetchone()[0]
            cat["item_count"] = c.execute("SELECT COUNT(*) FROM items WHERE category_id=?", (cat["id"],)).fetchone()[0]
            last = c.execute("SELECT * FROM runs WHERE category_id=? ORDER BY id DESC LIMIT 1", (cat["id"],)).fetchone()
            cat["last_run"] = dict(last) if last else None
    return cats


@app.post("/api/sites/{site_id}/categories", dependencies=[Depends(auth)])
def create_category(site_id: int, body: CategoryIn):
    with db.conn() as c:
        _get_site(c, site_id)
        cid = c.execute("INSERT INTO categories(site_id, name, max_pages, created_at) VALUES (?,?,?,?)",
                        (site_id, body.name.strip(), body.max_pages, db.now())).lastrowid
        return _get_cat(c, cid)


@app.put("/api/categories/{cid}", dependencies=[Depends(auth)])
def update_category(cid: int, body: CategoryIn):
    with db.conn() as c:
        _get_cat(c, cid)
        c.execute("UPDATE categories SET name=?, max_pages=? WHERE id=?", (body.name.strip(), body.max_pages, cid))
        return _get_cat(c, cid)


@app.delete("/api/categories/{cid}", dependencies=[Depends(auth)])
def delete_category(cid: int):
    with db.conn() as c:
        c.execute("DELETE FROM categories WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/categories/{cid}/har", dependencies=[Depends(auth)])
async def upload_har(cid: int, file: UploadFile = File(...), replace: bool = Form(True)):
    raw = await file.read()
    try:
        data = har.load(raw)
    except har.HarError as e:
        raise HTTPException(400, str(e))
    with db.conn() as c:
        cat = _get_cat(c, cid)
        site = _get_site(c, cat["site_id"])
        templates, stats = har.extract(data, site["allowed_domains"], api_only=True)
        if replace:
            c.execute("DELETE FROM requests WHERE category_id=?", (cid,))
        for t in templates:
            c.execute("INSERT INTO requests(site_id,category_id,label,method,url,headers,body,selected,created_at) VALUES (?,?,?,?,?,?,?,1,?)",
                      (site["id"], cid, t["label"], t["method"], t["url"], json.dumps(t["headers"], ensure_ascii=False), t["body"], db.now()))
    return {"stats": stats}


@app.get("/api/categories/{cid}/requests", dependencies=[Depends(auth)])
def list_requests(cid: int):
    with db.conn() as c:
        rows = [db.row(r) for r in c.execute("SELECT id, label, method, url, selected FROM requests WHERE category_id=? ORDER BY id", (cid,))]
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


# ---------- Collected data ----------
def _columns(rows: list[dict], limit: int | None = None) -> list[str]:
    freq: dict[str, int] = {}
    for r in rows:
        for k, v in r.items():
            if v not in (None, ""):
                freq[k] = freq.get(k, 0) + 1
    cols = sorted(freq, key=lambda k: -freq[k])  # stable: most common first, original order on ties
    return cols[:limit] if limit else cols


@app.get("/api/categories/{cid}/items", dependencies=[Depends(auth)])
def list_items(cid: int, q: str = "", limit: int = 50, offset: int = 0):
    with db.conn() as c:
        _get_cat(c, cid)
        where, args = "category_id=?", [cid]
        if q:
            where += " AND data LIKE ?"
            args.append(f"%{q}%")
        total = c.execute(f"SELECT COUNT(*) FROM items WHERE {where}", args).fetchone()[0]
        rows = [json.loads(r["data"]) for r in c.execute(
            f"SELECT data FROM items WHERE {where} ORDER BY id LIMIT ? OFFSET ?", args + [min(limit, 500), offset])]
    return {"total": total, "columns": _columns(rows, 8), "rows": rows}


@app.delete("/api/categories/{cid}/items", dependencies=[Depends(auth)])
def clear_items(cid: int):
    with db.conn() as c:
        c.execute("DELETE FROM items WHERE category_id=?", (cid,))
    return {"ok": True}


@app.get("/api/categories/{cid}/export", dependencies=[Depends(auth)])
def export_items(cid: int, fmt: str = "csv"):
    with db.conn() as c:
        cat = _get_cat(c, cid)
        site = _get_site(c, cat["site_id"])
        items = [dict(json.loads(r["data"]), _first_seen=r["first_seen"], _last_seen=r["last_seen"])
                 for r in c.execute("SELECT data, first_seen, last_seen FROM items WHERE category_id=? ORDER BY id", (cid,))]
    name = f"{site['name']}-{cat['name']}"
    if fmt == "json":
        body, media, ext = json.dumps(items, ensure_ascii=False, indent=2).encode(), "application/json", "json"
    else:
        buf = io.StringIO()
        cols = [k for k in _columns(items) if not k.startswith("_")] + ["_first_seen", "_last_seen"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(items)
        # BOM so Excel opens Persian text correctly
        body, media, ext = ("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", "csv"
    return Response(body, media_type=media, headers={
        "Content-Disposition": f"attachment; filename=export.{ext}; filename*=UTF-8''{quote(name)}.{ext}"})


# ---------- Runs ----------
def _start(site_id: int, category_id: int | None):
    if site_id in _running:
        raise HTTPException(409, "این سایت در حال اجراست؛ کمی صبر کنید")
    threading.Thread(target=_job, args=(site_id, "manual", category_id), daemon=True).start()
    return {"started": True}


@app.post("/api/sites/{site_id}/run", dependencies=[Depends(auth)])
def run_site_now(site_id: int):
    with db.conn() as c:
        _get_site(c, site_id)
    return _start(site_id, None)


@app.post("/api/categories/{cid}/run", dependencies=[Depends(auth)])
def run_category_now(cid: int):
    with db.conn() as c:
        cat = _get_cat(c, cid)
    return _start(cat["site_id"], cid)


@app.get("/api/categories/{cid}/runs", dependencies=[Depends(auth)])
def list_runs(cid: int):
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs WHERE category_id=? ORDER BY id DESC LIMIT 20", (cid,))]


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

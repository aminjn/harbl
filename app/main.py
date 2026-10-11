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
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import db, har, scraper
from .recorder import RecorderError, recorder
from .config import ADMIN_PASS, ADMIN_USER, DATA_DIR, FEED_KEY, OUTPUT_DIR

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
    await recorder.stop(save=False)
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
    max_pages: int = Field(200, ge=1, le=1000)


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


def _import_har(cid: int, raw: bytes, replace: bool = True) -> dict:
    try:
        data = har.load(raw)
    except har.HarError as e:
        raise HTTPException(400, str(e))
    with db.conn() as c:
        cat = _get_cat(c, cid)
        site = _get_site(c, cat["site_id"])
        templates, stats = har.extract(data, site["allowed_domains"], api_only=True)
        har.detect_details(templates)
        stats["details"] = sum(t["kind"] == "detail" for t in templates)
        if replace:
            c.execute("DELETE FROM requests WHERE category_id=?", (cid,))
        ids = []
        for t in templates:
            ids.append(c.execute("""INSERT INTO requests(site_id,category_id,label,method,url,headers,body,kind,url_template,body_template,selected,created_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
                      (site["id"], cid, t["label"], t["method"], t["url"], json.dumps(t["headers"], ensure_ascii=False), t["body"],
                       t["kind"], t["url_template"], t["body_template"], db.now())).lastrowid)
        for rid, t in zip(ids, templates):
            of = ids[t["detail_of_index"]] if t["detail_of_index"] is not None else None
            c.execute("UPDATE requests SET family=?, detail_of=? WHERE id=?", (rid, of, rid))
    return stats


@app.post("/api/categories/{cid}/har", dependencies=[Depends(auth)])
async def upload_har(cid: int, file: UploadFile = File(...), replace: bool = Form(True)):
    return {"stats": _import_har(cid, await file.read(), replace)}


# ---------- Recording browser (log in and click on the site from the panel) ----------
class RecStart(BaseModel):
    url: str = ""


class RecStop(BaseModel):
    save: bool = True


@app.get("/api/recorder", dependencies=[Depends(auth)])
def recorder_status():
    return recorder.status()


@app.post("/api/categories/{cid}/recorder/start", dependencies=[Depends(auth)])
async def recorder_start(cid: int, body: RecStart):
    with db.conn() as c:
        cat = _get_cat(c, cid)
        site = _get_site(c, cat["site_id"])
    try:
        await recorder.start(cid, body.url.strip() or f"https://{site['allowed_domains'][0]}/")
    except RecorderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # page load timeout etc.: the browser is open anyway
        return {**recorder.status(), "warning": str(e)}
    return recorder.status()


@app.get("/api/recorder/screen", dependencies=[Depends(auth)])
async def recorder_screen():
    try:
        shot = await recorder.screenshot()
        return Response(shot, media_type="image/jpeg", headers={"X-Page-Url": quote(recorder.page.url, safe=":/?&=%#")})
    except RecorderError as e:
        raise HTTPException(409, str(e))


@app.post("/api/recorder/input", dependencies=[Depends(auth)])
async def recorder_input(ev: dict):
    try:
        await recorder.input(ev)
    except RecorderError as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # navigation timeouts etc. are not fatal
        return {"ok": False, "warning": str(e)}
    return {"ok": True, "url": recorder.page.url if recorder.page else None}


@app.post("/api/recorder/stop", dependencies=[Depends(auth)])
async def recorder_stop(body: RecStop):
    cid, raw = await recorder.stop(save=body.save)
    if not body.save or cid is None:
        return {"saved": False}
    if not raw:
        raise HTTPException(500, "فایل ضبط ساخته نشد")
    return {"saved": True, "category_id": cid, "stats": _import_har(cid, raw, replace=True)}


@app.get("/api/categories/{cid}/requests", dependencies=[Depends(auth)])
def list_requests(cid: int):
    with db.conn() as c:
        rows = [db.row(r) for r in c.execute(
            "SELECT id, label, method, url, selected, kind, url_template FROM requests WHERE category_id=? ORDER BY kind DESC, id", (cid,))]
    return rows


class RequestPatch(BaseModel):
    selected: bool | None = None
    label: str | None = None
    url: str | None = None      # e.g. change specialty / city in a list request
    kind: str | None = None     # list | detail


def _get_req(c, req_id):
    r = db.row(c.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone())
    if not r:
        raise HTTPException(404, "درخواست پیدا نشد")
    return r


@app.patch("/api/requests/{req_id}", dependencies=[Depends(auth)])
def patch_request(req_id: int, body: RequestPatch):
    with db.conn() as c:
        r = _get_req(c, req_id)
        if body.selected is not None:
            c.execute("UPDATE requests SET selected=? WHERE id=?", (int(body.selected), req_id))
        if body.label is not None:
            c.execute("UPDATE requests SET label=? WHERE id=?", (body.label, req_id))
        if body.url is not None:
            site = _get_site(c, r["site_id"])
            if not body.url.startswith(("http://", "https://")) or not har.host_allowed(body.url, site["allowed_domains"]):
                raise HTTPException(400, "آدرس باید روی دامنه همین سایت باشد")
            c.execute("UPDATE requests SET url=? WHERE id=?", (body.url, req_id))
        if body.kind is not None:
            if body.kind == "detail" and not r["url_template"]:
                raise HTTPException(400, "در این آدرس شناسه‌ای از رکوردها پیدا نشد؛ نمی‌تواند «جزئیات» باشد")
            c.execute("UPDATE requests SET kind=? WHERE id=?", ("detail" if body.kind == "detail" else "list", req_id))
    return {"ok": True}


@app.post("/api/requests/{req_id}/duplicate", dependencies=[Depends(auth)])
def duplicate_request(req_id: int):
    with db.conn() as c:
        _get_req(c, req_id)
        c.execute("""INSERT INTO requests(site_id,category_id,label,method,url,headers,body,kind,url_template,body_template,family,detail_of,selected,created_at)
                     SELECT site_id,category_id,label,method,url,headers,body,kind,url_template,body_template,COALESCE(family,id),detail_of,selected,?
                     FROM requests WHERE id=?""", (db.now(), req_id))
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


def _fields(cat: dict) -> list[dict]:
    try:
        return [f for f in json.loads(cat.get("fields") or "[]") if f.get("key")]
    except ValueError:
        return []


def _project(row: dict, fields: list[dict]) -> dict:
    """Only the chosen fields, under their chosen names, in the chosen order (all fields when none chosen)."""
    if not fields:
        return row
    return {(f.get("label") or f["key"]): row.get(f["key"]) for f in fields}


class FieldsIn(BaseModel):
    fields: list[dict]


@app.get("/api/categories/{cid}/fields", dependencies=[Depends(auth)])
def list_fields(cid: int):
    """Every field found in this section's records, with how often it is filled and a sample value."""
    with db.conn() as c:
        cat = _get_cat(c, cid)
        rows = [json.loads(r["data"]) for r in c.execute(
            "SELECT data FROM items WHERE category_id=? ORDER BY last_seen DESC LIMIT 1000", (cid,))]
    found: dict[str, dict] = {}
    for r in rows:
        for k, v in r.items():
            f = found.setdefault(k, {"key": k, "count": 0, "sample": None})
            if v not in (None, "", "[]", "{}"):
                f["count"] += 1
                if f["sample"] is None:
                    f["sample"] = str(v)[:120]
    return {"total": len(rows), "selected": _fields(cat),
            "fields": sorted(found.values(), key=lambda f: -f["count"])}


@app.put("/api/categories/{cid}/fields", dependencies=[Depends(auth)])
def save_fields(cid: int, body: FieldsIn):
    clean = [{"key": str(f["key"]), "label": str(f.get("label") or "").strip()} for f in body.fields if f.get("key")]
    with db.conn() as c:
        _get_cat(c, cid)
        c.execute("UPDATE categories SET fields=? WHERE id=?", (json.dumps(clean, ensure_ascii=False), cid))
    return {"ok": True}


@app.get("/api/categories/{cid}/items", dependencies=[Depends(auth)])
def list_items(cid: int, q: str = "", source: str = "", limit: int = 50, offset: int = 0):
    with db.conn() as c:
        cat = _get_cat(c, cid)
        groups = [{"source": r["source"] or "—", "count": r["n"]} for r in c.execute(
            "SELECT source, COUNT(*) n FROM items WHERE category_id=? GROUP BY source ORDER BY n DESC", (cid,))]
        where, args = "category_id=?", [cid]
        if source:
            where += " AND source=?"
            args.append(source)
        if q:
            where += " AND data LIKE ?"
            args.append(f"%{q}%")
        total = c.execute(f"SELECT COUNT(*) FROM items WHERE {where}", args).fetchone()[0]
        rows = [json.loads(r["data"]) for r in c.execute(
            f"SELECT data FROM items WHERE {where} ORDER BY id LIMIT ? OFFSET ?", args + [min(limit, 500), offset])]
    fields = _fields(cat)
    if fields:
        rows = [_project(r, fields) for r in rows]
        return {"total": total, "groups": groups, "columns": list(rows[0]) if rows else [], "rows": rows, "chosen": True}
    return {"total": total, "groups": groups, "columns": _columns(rows, 8), "rows": rows, "chosen": False}


@app.delete("/api/categories/{cid}/items", dependencies=[Depends(auth)])
def clear_items(cid: int):
    with db.conn() as c:
        c.execute("DELETE FROM items WHERE category_id=?", (cid,))
    return {"ok": True}


@app.get("/api/categories/{cid}/export", dependencies=[Depends(auth)])
def export_items(cid: int, fmt: str = "json", source: str = ""):
    with db.conn() as c:
        cat = _get_cat(c, cid)
        site = _get_site(c, cat["site_id"])
        sql, args = "SELECT data, source, first_seen, last_seen FROM items WHERE category_id=?", [cid]
        if source:
            sql += " AND source=?"
            args.append(source)
        rows = c.execute(sql + " ORDER BY id", args).fetchall()
    name = f"{site['name']}-{cat['name']}" + (f"-{source.replace('/', '_')}" if source else "")
    fields = _fields(cat)
    if fmt == "json":
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["source"] or "—", []).append(_project(json.loads(r["data"]), fields))
        doc = {"site": site["name"], "section": cat["name"], "contract_ref": site["contract_ref"],
               "generated_at": db.now(), "total": len(rows),
               "groups": [{"source": k, "count": len(v), "items": v} for k, v in groups.items()]}
        body, media, ext = json.dumps(doc, ensure_ascii=False, indent=2).encode(), "application/json", "json"
    else:
        items = [dict(_project(json.loads(r["data"]), fields), _source=r["source"], _first_seen=r["first_seen"], _last_seen=r["last_seen"])
                 for r in rows]
        buf = io.StringIO()
        chosen = [f.get("label") or f["key"] for f in fields] or [k for k in _columns(items) if not k.startswith("_")]
        cols = chosen + ["_source", "_first_seen", "_last_seen"]
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


# ---------- Feed for your own website (API key, read-only) ----------
_feed_key = FEED_KEY


def feed_key() -> str:
    global _feed_key
    if not _feed_key:
        f = DATA_DIR / "feed_key.txt"
        if not f.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(secrets.token_urlsafe(24))
        _feed_key = f.read_text().strip()
    return _feed_key


def feed_auth(key: str = Query(""), x_api_key: str = Header("")):
    given = x_api_key or key
    if not given or not secrets.compare_digest(given, feed_key()):
        raise HTTPException(401, "کلید API نامعتبر است")


@app.get("/api/feed-info", dependencies=[Depends(auth)])
def feed_info():
    return {"key": feed_key()}


@app.get("/feed/sites", dependencies=[Depends(feed_auth)])
def feed_sites():
    """Every site and its sections, with record counts."""
    with db.conn() as c:
        out = []
        for s in c.execute("SELECT id, name FROM sites ORDER BY id"):
            sections = [{"id": r["id"], "name": r["name"], "count": r["n"], "url": f"/feed/sections/{r['id']}"}
                        for r in c.execute("""SELECT categories.id, categories.name, COUNT(items.id) n FROM categories
                                              LEFT JOIN items ON items.category_id=categories.id
                                              WHERE site_id=? GROUP BY categories.id ORDER BY categories.id""", (s["id"],))]
            out.append({"id": s["id"], "name": s["name"], "sections": sections})
    return out


@app.get("/feed/sections/{cid}", dependencies=[Depends(feed_auth)])
def feed_section(cid: int, since: str = "", source: str = "", limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0)):
    """Records of one section. `since` (ISO time) returns only records added/updated after it, for incremental sync."""
    with db.conn() as c:
        cat = _get_cat(c, cid)
        site = _get_site(c, cat["site_id"])
        where, args = "category_id=?", [cid]
        if since:
            where += " AND last_seen>?"
            args.append(since)
        if source:
            where += " AND source=?"
            args.append(source)
        total = c.execute(f"SELECT COUNT(*) FROM items WHERE {where}", args).fetchone()[0]
        rows = c.execute(f"SELECT item_key, source, first_seen, last_seen, data FROM items WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
                         args + [limit, offset]).fetchall()
    return {
        "site": {"id": site["id"], "name": site["name"]},
        "section": {"id": cid, "name": cat["name"]},
        "total": total, "offset": offset,
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        "items": [{"key": r["item_key"], "source": r["source"], "first_seen": r["first_seen"],
                   "last_seen": r["last_seen"], "data": _project(json.loads(r["data"]), _fields(cat))} for r in rows],
    }


# ---------- Panel ----------
@app.get("/", dependencies=[Depends(auth)])
def index():
    return FileResponse(STATIC / "index.html")

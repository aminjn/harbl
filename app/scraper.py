"""Replay HAR-derived requests and save responses as JSON."""
import json
import time
from datetime import date, datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from . import db, extract
from .config import MAX_RESPONSE_BYTES, OUTPUT_DIR, REQUEST_TIMEOUT, USER_AGENT_SUFFIX
from .har import host_allowed


class ScrapeBlocked(Exception):
    pass


def check_contract(site: dict):
    if not site["enabled"]:
        raise ScrapeBlocked("این سایت غیرفعال است")
    if not site.get("contract_ref"):
        raise ScrapeBlocked("شماره قرارداد ثبت نشده است")
    exp = site.get("contract_expires")
    if exp and date.fromisoformat(exp) < date.today():
        raise ScrapeBlocked(f"قرارداد در تاریخ {exp} منقضی شده است")


def _parse_body(resp: httpx.Response):
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        try:
            return resp.json()
        except ValueError:
            pass
    text = resp.text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _safe_headers(headers: dict) -> dict:
    # httpx only accepts ASCII header values; percent-encode anything else instead of failing the request
    return {k: v if v.isascii() else quote(v, safe="".join(chr(i) for i in range(32, 127)))
            for k, v in headers.items()}


PAGE_PARAMS = ("page", "p", "pageNumber", "pageIndex", "page_number", "pageNo")


def _page_urls(url: str, max_pages: int) -> list[str]:
    """Same URL for pages start..start+max_pages-1 when it has a page parameter."""
    u = urlsplit(url)
    q = parse_qsl(u.query, keep_blank_values=True)
    for i, (k, v) in enumerate(q):
        if k in PAGE_PARAMS and v.isdigit():
            start = int(v)
            out = []
            for n in range(start, start + max(max_pages, 1)):
                q2 = list(q)
                q2[i] = (k, str(n))
                out.append(urlunsplit(u._replace(query=urlencode(q2))))
            return out
    return [url]


def _headers_for(site: dict, r: dict) -> dict:
    headers = dict(r["headers"])
    # Site-level headers (e.g. a fresh Cookie) replace HAR headers with the same name
    for name, value in (site.get("extra_headers") or {}).items():
        for k in [k for k in headers if k.lower() == name.lower()]:
            del headers[k]
        headers[name] = value
    if USER_AGENT_SUFFIX:
        for k in list(headers):
            if k.lower() == "user-agent":
                headers[k] = f"{headers[k]} {USER_AGENT_SUFFIX}"
    return _safe_headers(headers)


def _save_records(category_id: int, records: list[dict], url: str) -> int:
    """Upsert records; returns how many were new."""
    new, ts = 0, db.now()
    with db.conn() as c:
        for rec in records:
            flat = extract.flatten(rec)
            key = extract.record_key(rec)
            cur = c.execute("UPDATE items SET data=?, source_url=?, last_seen=? WHERE category_id=? AND item_key=?",
                            (json.dumps(flat, ensure_ascii=False), url, ts, category_id, key))
            if cur.rowcount == 0:
                c.execute("INSERT INTO items(category_id, item_key, data, source_url, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                          (category_id, key, json.dumps(flat, ensure_ascii=False), url, ts, ts))
                new += 1
    return new


def run_category(category_id: int, trigger: str = "manual") -> dict:
    with db.conn() as c:
        cat = db.row(c.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone())
        if not cat:
            raise ScrapeBlocked("بخش پیدا نشد")
        site = db.row(c.execute("SELECT * FROM sites WHERE id=?", (cat["site_id"],)).fetchone())
        reqs = [db.row(r) for r in c.execute(
            "SELECT * FROM requests WHERE category_id=? AND selected=1 ORDER BY id", (category_id,))]
        run_id = c.execute(
            "INSERT INTO runs(site_id, category_id, started_at, status, trigger) VALUES (?,?,?,?,?)",
            (site["id"], category_id, db.now(), "running", trigger)).lastrowid

    results, ok, fail, message, records_total, records_new = [], 0, 0, None, 0, 0
    try:
        check_contract(site)
        if not reqs:
            raise ScrapeBlocked("هیچ درخواستی در این بخش نیست؛ HAR این بخش را آپلود کنید")
        sent = 0
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            for r in reqs:
                prev_keys = None
                for url in _page_urls(r["url"], cat["max_pages"]):
                    if sent:
                        time.sleep(max(site["delay_seconds"], 0))
                    item = {"request_id": r["id"], "label": r["label"], "method": r["method"], "url": url}
                    results.append(item)
                    # Hard guarantee: never contact a host outside the contract
                    if not host_allowed(url, site["allowed_domains"]):
                        item.update(error="دامنه خارج از قرارداد است؛ ارسال نشد")
                        fail += 1
                        break
                    sent += 1
                    t0 = time.monotonic()
                    try:
                        resp = client.request(r["method"], url, headers=_headers_for(site, r),
                                              content=r["body"].encode() if r["body"] else None)
                        if len(resp.content) > MAX_RESPONSE_BYTES:
                            raise ValueError("پاسخ بزرگ‌تر از حد مجاز است")
                        data = _parse_body(resp)
                        item.update(status=resp.status_code, elapsed_ms=round((time.monotonic() - t0) * 1000), data=data)
                    except Exception as e:  # network / timeout / size
                        item.update(error=str(e))
                        fail += 1
                        break
                    if resp.status_code in (401, 403):
                        item["warning"] = "احتمالاً نشست (کوکی/توکن) منقضی شده؛ HAR جدید آپلود کنید"
                    if not 200 <= resp.status_code < 400:
                        fail += 1
                        break
                    ok += 1
                    records = extract.find_records(data) if isinstance(data, (dict, list)) else []
                    keys = [extract.record_key(x) for x in records]
                    item["records"] = len(records)
                    if not records or keys == prev_keys:  # past the last page
                        break
                    prev_keys = keys
                    records_total += len(records)
                    records_new += _save_records(category_id, records, url)
        status = "ok" if fail == 0 else ("partial" if ok else "failed")
        message = f"{records_total} رکورد ({records_new} جدید)"
    except ScrapeBlocked as e:
        status, message = "failed", str(e)
    except Exception as e:  # never leave a run stuck in "running"
        status, message = "failed", f"خطای داخلی: {e}"

    out_file = None
    if results:
        out_dir = OUTPUT_DIR / f"site_{site['id']}" / f"cat_{category_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"run_{run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out_file.write_text(json.dumps({
            "site": {"id": site["id"], "name": site["name"], "contract_ref": site["contract_ref"]},
            "category": {"id": category_id, "name": cat["name"]},
            "run_id": run_id, "trigger": trigger, "generated_at": db.now(),
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    with db.conn() as c:
        c.execute("UPDATE runs SET finished_at=?, status=?, ok_count=?, fail_count=?, output_file=?, message=? WHERE id=?",
                  (db.now(), status, ok, fail, str(out_file) if out_file else None, message, run_id))
    return {"run_id": run_id, "status": status, "ok": ok, "fail": fail, "message": message}


def run_site(site_id: int, trigger: str = "manual", category_id: int | None = None) -> list[dict]:
    """Run one section, or every section of the site one after another."""
    with db.conn() as c:
        ids = [category_id] if category_id else [
            r["id"] for r in c.execute("SELECT id FROM categories WHERE site_id=? ORDER BY id", (site_id,))]
    return [run_category(cid, trigger) for cid in ids]

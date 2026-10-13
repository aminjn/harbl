"""Replay HAR-derived requests and save responses as JSON."""
import json
import re
import time
from datetime import date, datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from . import db, extract, visual
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


PAGE_PARAMS = ("page", "p", "pageNumber", "pageIndex", "page_number", "pageNo", "pagenum")
NEXT_DATA = re.compile(r"/_next/data/([^/]+)/")


def _page_param(url: str):
    """(name, number) of the page query parameter, or None."""
    for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if k in PAGE_PARAMS and v.isdigit():
            return k, int(v)
    return None


def _with_page(url: str, name: str, n: int) -> str:
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != name] + [(name, str(n))]
    return urlunsplit(u._replace(query=urlencode(q)))


def _canonical(url: str) -> str:
    u = urlsplit(url)
    return urlunsplit(u._replace(query=urlencode(sorted(parse_qsl(u.query, keep_blank_values=True)))))


def _fresh_build_id(client: httpx.Client, url: str, headers: dict) -> str | None:
    """Next.js sites change /_next/data/<buildId>/ on every deploy; read the current one from the home page."""
    u = urlsplit(url)
    try:
        html = client.get(f"{u.scheme}://{u.netloc}/", headers=headers).text
    except Exception:
        return None
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


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


def _save_records(category_id: int, records: list[dict], url: str, list_id: int | None = None) -> int:
    """Upsert records; returns how many were new."""
    new, ts, source = 0, db.now(), extract.source_of(url)
    with db.conn() as c:
        for rec in records:
            flat = extract.flatten(rec)
            key = extract.record_key(rec)
            # Keep fields merged from detail requests (profile, phone) when the list row is refreshed
            old = c.execute("SELECT data FROM items WHERE category_id=? AND item_key=?", (category_id, key)).fetchone()
            if old:
                flat = {**json.loads(old["data"]), **flat}
            cur = c.execute("UPDATE items SET data=?, source_url=?, source=?, list_id=?, last_seen=? WHERE category_id=? AND item_key=?",
                            (json.dumps(flat, ensure_ascii=False), url, source, list_id, ts, category_id, key))
            if cur.rowcount == 0:
                c.execute("INSERT INTO items(category_id, item_key, data, source_url, source, list_id, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                          (category_id, key, json.dumps(flat, ensure_ascii=False), url, source, list_id, ts, ts))
                new += 1
    return new


def _fill(template: str, rec: dict, for_url: bool = False) -> str | None:
    """Put a record's values into a {{field}} template; None if a field is missing."""
    missing = False

    def val(m, quoted):
        nonlocal missing
        v = rec.get(m.group(1))
        if v in (None, ""):
            missing = True
            return ""
        if quoted:  # JSON body: keep numbers as numbers
            return json.dumps(v, ensure_ascii=False)
        return quote(str(v), safe="") if for_url else str(v)

    out = re.sub(r'"\{\{([^{}]+)\}\}"', lambda m: val(m, True), template) if not for_url else template
    out = re.sub(r"\{\{([^{}]+)\}\}", lambda m: val(m, False), out)
    return None if missing else out


def _merge_detail(item_id: int, request_id: int, data) -> None:
    """Add a detail response's fields (profile, phone, ...) to the record."""
    if isinstance(data, dict):
        main = data.get("pageProps", data.get("data", data))
        main = main if isinstance(main, dict) else {"value": main}
    else:
        main = {"value": data}
    extra = {k: v for k, v in extract.flatten(main).items() if v not in (None, "", "[]", "{}")}
    with db.conn() as c:
        row = c.execute("SELECT data, details_done FROM items WHERE id=?", (item_id,)).fetchone()
        rec = json.loads(row["data"])
        rec.update(extra)
        done = [int(x) for x in re.findall(r"\[(\d+)\]", row["details_done"] or "")] + [request_id]
        # details_done is stored as "[1][5]" style text so a LIKE '%[id]%' finds it
        c.execute("UPDATE items SET data=?, details_done=? WHERE id=?",
                  (json.dumps(rec, ensure_ascii=False), "".join(f"[{i}]" for i in sorted(set(done))) , item_id))


def run_category(category_id: int, trigger: str = "manual") -> dict:
    with db.conn() as c:
        cat = db.row(c.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone())
        if not cat:
            raise ScrapeBlocked("بخش پیدا نشد")
        site = db.row(c.execute("SELECT * FROM sites WHERE id=?", (cat["site_id"],)).fetchone())
        reqs = [db.row(r) for r in c.execute(
            "SELECT * FROM requests WHERE category_id=? AND selected=1 ORDER BY id", (category_id,))]
        run_id = c.execute(
            "INSERT INTO runs(site_id, category_id, started_at, status, trigger, message) VALUES (?,?,?,?,?,?)",
            (site["id"], category_id, db.now(), "running", trigger, "شروع…")).lastrowid
    if cat.get("mode") == "visual":
        return _run_visual(cat, site, run_id, trigger)

    results, ok, fail, message, records_total, records_new, detailed = [], 0, 0, None, 0, 0, 0
    build_ids: dict[str, str] = {}  # old Next.js build id -> current one
    fetched: set[tuple] = set()      # pages already fetched in this run (HAR often holds page 1 and 2)
    sent = 0

    def progress():
        with db.conn() as c:
            c.execute("UPDATE runs SET ok_count=?, fail_count=?, message=? WHERE id=?",
                      (ok, fail, f"{sent} درخواست · {records_total} رکورد ({records_new} جدید)"
                                 + (f" · جزئیات {detailed} رکورد" if detailed else ""), run_id))

    def fetch(client, r, url, body):
        """One request with contract check, delay and Next.js build-id refresh. Returns (item, data) or (item, None)."""
        nonlocal sent, ok, fail
        m = NEXT_DATA.search(url)
        if m and m.group(1) in build_ids:
            url = url.replace(m.group(0), f"/_next/data/{build_ids[m.group(1)]}/")
        item = {"request_id": r["id"], "label": r["label"], "method": r["method"], "url": url}
        results.append(item)
        # Hard guarantee: never contact a host outside the contract
        if not host_allowed(url, site["allowed_domains"]):
            item.update(error="دامنه خارج از قرارداد است؛ ارسال نشد")
            fail += 1
            return item, None
        if sent:
            time.sleep(max(site["delay_seconds"], 0))
        sent += 1
        headers = _headers_for(site, r)
        content = body.encode() if body else None
        t0 = time.monotonic()
        try:
            resp = client.request(r["method"], url, headers=headers, content=content)
            m = NEXT_DATA.search(url)
            if resp.status_code == 404 and m:
                new_id = _fresh_build_id(client, url, headers)
                if new_id and new_id != m.group(1):
                    build_ids[m.group(1)] = new_id
                    url = url.replace(m.group(0), f"/_next/data/{new_id}/")
                    item["url"] = url
                    resp = client.request(r["method"], url, headers=headers, content=content)
            if len(resp.content) > MAX_RESPONSE_BYTES:
                raise ValueError("پاسخ بزرگ‌تر از حد مجاز است")
            data = _parse_body(resp)
        except Exception as e:  # network / timeout / size
            item.update(error=str(e))
            fail += 1
            return item, None
        item.update(status=resp.status_code, elapsed_ms=round((time.monotonic() - t0) * 1000))
        if resp.status_code in (401, 403):
            item["warning"] = "احتمالاً نشست (کوکی/توکن) منقضی شده یا لاگین لازم است؛ HAR جدید با کوکی آپلود کنید"
        if not 200 <= resp.status_code < 300:
            item["error_body"] = str(data)[:300]
            return item, None
        ok += 1
        return item, data

    try:
        check_contract(site)
        if not reqs:
            raise ScrapeBlocked("هیچ درخواستی در این بخش نیست؛ HAR این بخش را آپلود کنید")
        lists = [r for r in reqs if r.get("kind") != "detail"]
        details = [r for r in reqs if r.get("kind") == "detail" and r.get("url_template")]
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            # 1) list requests, every page
            for r in lists:
                page = _page_param(r["url"])
                pname, pnum = page or ("page", 1)
                url, prev_keys, pages, rec_path = r["url"], None, 0, None
                while True:
                    page_key = (r["method"], _canonical(url), r["body"])
                    if page_key in fetched:
                        break
                    fetched.add(page_key)
                    item, data = fetch(client, r, url, r["body"])
                    if data is None:
                        # A probed extra page that the site rejects just means there are no more pages
                        if pages and "status" in item:
                            results.remove(item)
                        elif "error" not in item:
                            fail += 1
                        break
                    records, rec_path = extract.find_records(data, rec_path) if isinstance(data, (dict, list)) else ([], rec_path)
                    keys = [extract.record_key(x) for x in records]
                    item["records"] = len(records)
                    if not records or keys == prev_keys:  # past the last page
                        break
                    prev_keys = keys
                    pages += 1
                    records_total += len(records)
                    records_new += _save_records(category_id, records, url, r["id"])
                    progress()
                    # Next page: follow the page parameter, or try adding one (GET only)
                    if pages >= cat["max_pages"] or (not page and r["method"] != "GET"):
                        break
                    url = _with_page(url, pname, pnum + pages)
            # 2) detail requests (profile, phone, ...), once per record that does not have them yet
            for r in details:
                with db.conn() as c:
                    # only records of the list this detail belongs to (not e.g. the city list)
                    todo = [(row["id"], json.loads(row["data"])) for row in c.execute(
                        """SELECT id, data FROM items WHERE category_id=? AND details_done NOT LIKE ?
                           AND (? IS NULL OR list_id IN (SELECT id FROM requests WHERE COALESCE(family, id)=?))""",
                        (category_id, f'%[{r["id"]}]%', r.get("detail_of"), r.get("detail_of")))]
                for item_id, rec in todo:
                    url = _fill(r["url_template"], rec, for_url=True)
                    body = _fill(r["body_template"], rec) if r.get("body_template") else r["body"]
                    if url is None or (r.get("body_template") and body is None):
                        continue  # this record lacks the id/slug the template needs
                    item, data = fetch(client, r, url, body)
                    if data is None:
                        if "error" not in item:
                            fail += 1
                        continue
                    detailed += 1
                    _merge_detail(item_id, r["id"], data)
                    progress()
        with db.conn() as c:
            total = c.execute("SELECT COUNT(*) FROM items WHERE category_id=?", (category_id,)).fetchone()[0]
        status = "ok" if fail == 0 else ("partial" if ok else "failed")
        message = f"{records_total} رکورد از {sent} درخواست ({records_new} جدید{f'، {detailed} جزئیات' if detailed else ''}) · مجموع این بخش: {total}"
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


def _browser_died(e: Exception) -> bool:
    return bool(visual.DEAD_BROWSER.search(str(e)))


def _run_visual(cat: dict, site: dict, run_id: int, trigger: str) -> dict:
    """Point-and-click section: a real browser reads the picked fields page by page."""
    log: list[dict] = []
    stats = {"pages": 0, "rows": 0, "new": 0, "details": 0}

    def progress(st):
        # where the run really is (a continued run counts from its saved position, not from zero)
        if st.get("total"):
            msg = (f"پروفایل {st['at']} از {st['total']}" + (f" (ادامه از {st['from'] + 1})" if st.get("from") else "")
                   + f" · {st['new']} مورد جدید")
        elif st.get("list_page"):
            msg = (f"صفحه لیست {st['list_page']}" + (" (ادامه)" if st.get("list_resumed") else "")
                   + f" · {st['rows']} پروفایل پیدا شده تا اینجا")
        else:
            msg = (f"{st['pages']} صفحه · {st['rows']} مورد ({st['new']} جدید)"
                   + (f" · {st['details']} صفحه جزئیات" if st["details"] else ""))
        with db.conn() as c:
            c.execute("UPDATE runs SET ok_count=?, message=? WHERE id=?", (st["pages"] + st["details"], msg, run_id))

    try:
        check_contract(site)
        for attempt in range(4):
            try:
                stats = visual.run(cat, site, progress, log.append)
                break
            except Exception as e:
                # the browser was closed or crashed mid-run: open a new one and continue from the saved progress
                if attempt < 3 and _browser_died(e) and visual.progress_file(cat["id"]).exists() and not visual.stop_requested(site["id"], cat["id"]):
                    log.append({"note": f"مرورگر بسته شد؛ ادامه از همان‌جا (بار {attempt + 1})"})
                    print(f"[بخش {cat['id']}] مرورگر بسته شد؛ ادامه از همان‌جا", flush=True)
                    time.sleep(5)
                    continue
                raise
        with db.conn() as c:
            total = c.execute("SELECT COUNT(*) FROM items WHERE category_id=?", (cat["id"],)).fetchone()[0]
        errors = sum(1 for x in log if x.get("error"))
        status = "ok" if not errors else ("partial" if stats["rows"] else "failed")
        message = (f"{stats['rows']} مورد از {stats['pages']} صفحه ({stats['new']} جدید"
                   + (f"، جزئیات {stats['details']}" if stats["details"] else "") + f") · مجموع این بخش: {total}")
        if stats.get("from"):  # continued: say where from, so it is clear nothing started over
            message = f"ادامه از پروفایل {stats['from'] + 1} از {stats['total']} · " + message
        if stats.get("stopped"):
            message = "متوقف شد (اجرای بعدی از همین‌جا ادامه می‌دهد) · " + message
        if trigger == "resume":
            message = "ادامه بعد از ری‌استارت · " + message
        if stats.get("empty"):  # fields the profiles did not show: wrong pick, or the number needs login/click
            message += " · خالی ماند: " + "، ".join(f"{k} در {v} پروفایل" for k, v in stats["empty"].items())
            if status == "ok":
                status = "partial"
        if not stats["rows"] and not stats.get("stopped"):
            status, message = "failed", "در صفحه لیست موردی پیدا نشد؛ آدرس لیست را بررسی کنید یا اطلاعات را دوباره روی صفحه انتخاب کنید"
    except ImportError:
        status, message = "failed", "مرورگر روی سرور نصب نیست؛ sudo bash deploy/install-recorder.sh"
    except (ScrapeBlocked, ValueError) as e:
        status, message = "failed", str(e)
    except Exception as e:
        status, message = "failed", ("مرورگر بسته شد (ری‌استارت سرور یا کمبود حافظه)؛ اجرای بعدی از همین‌جا ادامه می‌دهد"
                                      if _browser_died(e) else f"خطای داخلی: {e}")
    out_dir = OUTPUT_DIR / f"site_{site['id']}" / f"cat_{cat['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"run_{run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_file.write_text(json.dumps({"run_id": run_id, "trigger": trigger, "generated_at": db.now(), "pages": log},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    with db.conn() as c:
        c.execute("UPDATE runs SET finished_at=?, status=?, ok_count=?, fail_count=?, output_file=?, message=? WHERE id=?",
                  (db.now(), status, stats["pages"] + stats["details"], sum(1 for x in log if x.get("error")),
                   str(out_file), message, run_id))
    return {"run_id": run_id, "status": status, "message": message}


def run_site(site_id: int, trigger: str = "manual", category_id: int | None = None) -> list[dict]:
    """Run one section, or every section of the site one after another."""
    with db.conn() as c:
        ids = [category_id] if category_id else [
            r["id"] for r in c.execute("SELECT id FROM categories WHERE site_id=? ORDER BY id", (site_id,))]
    out = []
    for cid in ids:
        if visual.stop_requested(site_id):  # the stop button also skips the sections not started yet
            break
        out.append(run_category(cid, trigger))
    return out

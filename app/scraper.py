"""Replay HAR-derived requests and save responses as JSON."""
import json
import time
from datetime import date, datetime
from urllib.parse import quote

import httpx

from . import db
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


def run_site(site_id: int, trigger: str = "manual") -> dict:
    with db.conn() as c:
        site = db.row(c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())
        if not site:
            raise ScrapeBlocked("سایت پیدا نشد")
        reqs = [db.row(r) for r in c.execute(
            "SELECT * FROM requests WHERE site_id=? AND selected=1 ORDER BY id", (site_id,))]
        run_id = c.execute(
            "INSERT INTO runs(site_id, started_at, status, trigger) VALUES (?,?,?,?)",
            (site_id, db.now(), "running", trigger)).lastrowid

    results, ok, fail, message = [], 0, 0, None
    try:
        check_contract(site)
        if not reqs:
            raise ScrapeBlocked("هیچ درخواستی برای این سایت انتخاب نشده است")
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            for i, r in enumerate(reqs):
                if i:
                    time.sleep(max(site["delay_seconds"], 0))
                item = {"request_id": r["id"], "label": r["label"], "method": r["method"], "url": r["url"]}
                # Hard guarantee: never contact a host outside the contract
                if not host_allowed(r["url"], site["allowed_domains"]):
                    item.update(error="دامنه خارج از قرارداد است؛ ارسال نشد")
                    fail += 1
                    results.append(item)
                    continue
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
                t0 = time.monotonic()
                try:
                    resp = client.request(r["method"], r["url"], headers=_safe_headers(headers),
                                          content=r["body"].encode() if r["body"] else None)
                    if len(resp.content) > MAX_RESPONSE_BYTES:
                        raise ValueError("پاسخ بزرگ‌تر از حد مجاز است")
                    item.update(status=resp.status_code,
                                elapsed_ms=round((time.monotonic() - t0) * 1000),
                                data=_parse_body(resp))
                    if resp.status_code in (401, 403):
                        item["warning"] = "احتمالاً نشست (کوکی/توکن) منقضی شده؛ HAR جدید آپلود کنید"
                    if 200 <= resp.status_code < 400:
                        ok += 1
                    else:
                        fail += 1
                except Exception as e:  # network / timeout / size
                    item.update(error=str(e))
                    fail += 1
                results.append(item)
        status = "ok" if fail == 0 else ("partial" if ok else "failed")
    except ScrapeBlocked as e:
        status, message = "failed", str(e)
    except Exception as e:  # never leave a run stuck in "running"
        status, message = "failed", f"خطای داخلی: {e}"

    out_file = None
    if results:
        site_dir = OUTPUT_DIR / f"site_{site_id}"
        site_dir.mkdir(parents=True, exist_ok=True)
        out_file = site_dir / f"run_{run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out_file.write_text(json.dumps({
            "site": {"id": site_id, "name": site["name"], "contract_ref": site["contract_ref"]},
            "run_id": run_id, "trigger": trigger, "generated_at": db.now(),
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    with db.conn() as c:
        c.execute("UPDATE runs SET finished_at=?, status=?, ok_count=?, fail_count=?, output_file=?, message=? WHERE id=?",
                  (db.now(), status, ok, fail, str(out_file) if out_file else None, message, run_id))
    return {"run_id": run_id, "status": status, "ok": ok, "fail": fail, "message": message}

"""Point-and-click collection: the user clicks the fields on the real page, we keep CSS selectors,
then a browser walks the list pages ({page} in the URL), reads every item, and opens each item's
detail page for the fields that live there (e.g. a doctor's phone).

Section config (categories.config, mode='visual'):
  {"list_url": "https://site/list?page={page}", "max_pages": 200,
   "item_selector": "div.cards > div.card",            # one repeated card per record
   "fields": [{"name": "نام", "page": "list"|"detail", "attr": "text"|"href"|"src",
               "rel": "h3 > a", "rel_tags": "h3 > a",  # list: path inside the card
               "selector": "main > div.phone"}],        # detail: path in the page
   "detail_link": "لینک",                               # list field whose href opens the detail page
   "detail_click": "button.show-phone"}                 # optional click before reading the detail page
"""
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from . import db
from .config import DATA_DIR
from .har import host_allowed, is_tracker

SESSIONS = DATA_DIR / "sessions"

# Runs in the page. Returns what is under (x, y) and selectors that find it again.
PICK_JS = r"""
([x, y, where, itemSel, attr]) => {
  let el = document.elementFromPoint(x, y);
  if (!el) return {error: 'چیزی زیر این نقطه نیست'};
  if (attr === 'href' || attr === 'click') el = el.closest('a,button,[role=button],[onclick]') || el;
  if (attr === 'src') el = el.closest('img') || el.querySelector?.('img') || el;
  const esc = CSS.escape;
  const okClass = c => c.length < 40 && !/\d{4,}|^(active|hover|focus|selected|open|show|is-|has-)/.test(c) && !/^(css|jsx|sc)-[a-z0-9]+$/i.test(c);
  const seg = e => { let s = e.tagName.toLowerCase(); const cls = [...e.classList].filter(okClass).slice(0, 3);
                     if (cls.length) s += '.' + cls.map(esc).join('.'); return s; };
  const path = (e, stop, tagsOnly) => { const p = []; while (e && e !== stop && e.nodeType === 1 && e !== document.documentElement) {
                     p.unshift(tagsOnly ? e.tagName.toLowerCase() : seg(e)); e = e.parentElement; } return p.join(' > '); };
  const value = e => attr === 'href' ? (e.closest('a')?.href || '') : attr === 'src' ? (e.currentSrc || e.src || '')
                     : (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ');
  const mark = els => { document.querySelectorAll('.__pick').forEach(e => { e.classList.remove('__pick'); });
                        if (!document.getElementById('__pickcss')) { const st = document.createElement('style'); st.id = '__pickcss';
                          st.textContent = '.__pick{outline:3px solid #f43f5e!important;outline-offset:1px}'; document.head.appendChild(st); }
                        els.forEach(e => e.classList.add('__pick')); };
  if (where === 'list') {
    let card = itemSel ? el.closest(itemSel) : null, newCard = false;
    if (!card) {  // the repeated card: the ancestor with the most same-looking siblings
      let best = null, bestN = 1;
      for (let a = el; a && a.parentElement && a !== document.body; a = a.parentElement) {
        const s = seg(a), n = [...a.parentElement.children].filter(c => c.matches(s)).length;
        if (n > bestN) { best = a; bestN = n; }
      }
      if (!best) return {error: 'این مورد در لیستِ تکرارشونده‌ای نیست؛ روی یکی از کارت‌های لیست کلیک کنید'};
      card = best; newCard = true;
      itemSel = path(card.parentElement, null) + ' > ' + seg(card);
    }
    const rel = el === card ? ':scope' : path(el, card), relTags = el === card ? ':scope' : path(el, card, true);
    const cards = [...document.querySelectorAll(itemSel)];
    const pick = c => rel === ':scope' ? c : (c.querySelector(':scope > ' + rel) || c.querySelector(':scope > ' + relTags));
    const found = cards.map(pick).filter(Boolean);
    mark(found);
    return {item_selector: itemSel, new_card: newCard, rel, rel_tags: relTags, count: cards.length, filled: found.length,
            samples: found.slice(0, 3).map(value)};
  }
  // detail page / button: a selector for this one element
  const parts = [];
  for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
    if (e.id && !/\d{3,}/.test(e.id)) { parts.unshift('#' + esc(e.id)); break; }
    let s = seg(e); const sib = e.parentElement ? [...e.parentElement.children].filter(c => c.matches(s)) : [];
    if (sib.length > 1) s += `:nth-of-type(${[...e.parentElement.children].filter(c => c.tagName === e.tagName).indexOf(e) + 1})`;
    parts.unshift(s);
  }
  const selector = parts.join(' > ');
  mark([el]);
  return {selector, count: document.querySelectorAll(selector).length, samples: [value(el)]};
}
"""

# Runs in the page: read every card of a list page.
LIST_JS = r"""
([itemSel, fields]) => [...document.querySelectorAll(itemSel)].map(c => {
  const o = {};
  for (const f of fields) {
    const e = f.rel === ':scope' ? c : (c.querySelector(':scope > ' + f.rel) || c.querySelector(':scope > ' + f.rel_tags));
    o[f.name] = !e ? null : f.attr === 'href' ? (e.closest('a')?.href || null) : f.attr === 'src' ? (e.currentSrc || e.src || null)
              : (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ');
  }
  return o;
})
"""

DETAIL_JS = r"""
(fields) => { const o = {};
  for (const f of fields) { const e = document.querySelector(f.selector);
    o[f.name] = !e ? null : f.attr === 'href' ? (e.closest('a')?.href || null) : f.attr === 'src' ? (e.currentSrc || e.src || null)
              : (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' '); }
  return o; }
"""


def session_file(category_id: int) -> Path:
    return SESSIONS / f"cat_{category_id}.json"


def page_url(list_url: str, n: int) -> str:
    return list_url.replace("{page}", str(n))


def _key(row: dict, link_field: str | None) -> str:
    if link_field and row.get(link_field):
        return "url:" + row[link_field].split("#")[0]
    return "h:" + hashlib.sha1(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run(cat: dict, site: dict, progress, log) -> dict:
    """Walk the list pages and detail pages. progress(pages, rows, new, details); log(item) records each page."""
    from playwright.sync_api import TimeoutError as PwTimeout, sync_playwright

    cfg = json.loads(cat.get("config") or "{}")
    list_fields = [f for f in cfg.get("fields", []) if f.get("page") == "list" and f.get("rel")]
    detail_fields = [f for f in cfg.get("fields", []) if f.get("page") == "detail" and f.get("selector")]
    link = cfg.get("detail_link")
    if not cfg.get("list_url") or not cfg.get("item_selector") or not list_fields:
        raise ValueError("اول آدرس لیست را بدهید و حداقل یک اطلاعات را روی صفحه لیست انتخاب کنید")
    delay = max(site["delay_seconds"], 0)
    stats = {"pages": 0, "rows": 0, "new": 0, "details": 0}
    paged = "{page}" in cfg["list_url"]
    max_pages = int(cfg.get("max_pages") or 200) if paged else 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-dev-shm-usage"])
        sess = session_file(cat["id"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="fa-IR",
                                  storage_state=str(sess) if sess.exists() else None)
        ctx.route("**/*", lambda r: r.abort() if r.request.resource_type != "document" and is_tracker(r.request.url) else r.continue_())
        page = ctx.new_page()

        def open_(url: str) -> bool:
            if not host_allowed(url, site["allowed_domains"]):
                log({"url": url, "error": "دامنه خارج از قرارداد است؛ باز نشد"})
                return False
            if stats["pages"] or stats["details"]:
                time.sleep(delay)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                return True
            except Exception as e:
                log({"url": url, "error": str(e)[:300]})
                return False

        # 1) list pages
        prev_first = None
        for n in range(1, max_pages + 1):
            url = page_url(cfg["list_url"], n)
            if not open_(url):
                break
            try:
                page.wait_for_selector(cfg["item_selector"], timeout=15000)
            except PwTimeout:
                log({"url": url, "rows": 0})
                break
            page.mouse.wheel(0, 20000)  # lazy-loaded cards
            page.wait_for_timeout(800)
            rows = [r for r in page.evaluate(LIST_JS, [cfg["item_selector"], list_fields]) if any(r.values())]
            log({"url": url, "rows": len(rows)})
            first = json.dumps(rows[:1], ensure_ascii=False)
            if not rows or first == prev_first:  # past the last page
                break
            prev_first = first
            stats["pages"] += 1
            stats["rows"] += len(rows)
            stats["new"] += _save(cat["id"], rows, link, url)
            progress(stats)

        # 2) detail pages, for records that do not have them yet
        if detail_fields and link:
            with db.conn() as c:
                todo = [(r["id"], json.loads(r["data"])) for r in c.execute(
                    "SELECT id, data FROM items WHERE category_id=? AND details_done NOT LIKE '%[v]%'", (cat["id"],))]
            for item_id, rec in todo:
                href = rec.get(link)
                if not href or not open_(href):
                    continue
                try:
                    page.wait_for_selector(detail_fields[0]["selector"], timeout=15000, state="attached")
                except PwTimeout:
                    pass
                if cfg.get("detail_click"):
                    try:
                        page.click(cfg["detail_click"], timeout=5000)
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                extra = page.evaluate(DETAIL_JS, detail_fields)
                log({"url": href, "detail": True, "found": sum(v is not None for v in extra.values())})
                with db.conn() as c:
                    rec.update({k: v for k, v in extra.items() if v not in (None, "")})
                    c.execute("UPDATE items SET data=?, details_done=details_done || '[v]', last_seen=? WHERE id=?",
                              (json.dumps(rec, ensure_ascii=False), db.now(), item_id))
                stats["details"] += 1
                progress(stats)
        browser.close()
    return stats


def _save(category_id: int, rows: list[dict], link: str | None, url: str) -> int:
    new, ts, source = 0, db.now(), urlparse(url).path or "/"
    with db.conn() as c:
        for row in rows:
            key = _key(row, link)
            old = c.execute("SELECT id, data FROM items WHERE category_id=? AND item_key=?", (category_id, key)).fetchone()
            if old:  # keep detail fields gathered earlier
                merged = {**json.loads(old["data"]), **{k: v for k, v in row.items() if v not in (None, "")}}
                c.execute("UPDATE items SET data=?, source_url=?, last_seen=? WHERE id=?",
                          (json.dumps(merged, ensure_ascii=False), url, ts, old["id"]))
            else:
                c.execute("INSERT INTO items(category_id, item_key, data, source_url, source, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                          (category_id, key, json.dumps(row, ensure_ascii=False), url, "لیست", ts, ts))
                new += 1
    return new

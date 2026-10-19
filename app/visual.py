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
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from . import db
from .config import DATA_DIR
from .extract import source_of
from .har import host_allowed, is_tracker

SESSIONS = DATA_DIR / "sessions"

# Coordinates ("lat,lng") for a "location" field: from the picked map/link (map links, map image or
# iframe addresses, data-* attributes), else from the page's own data (Next.js data, JSON-LD, scripts).
GEO_FN = r"""
  const __geo = e => {
    const fix = (a, b) => { a = +a; b = +b; if (!isFinite(a) || !isFinite(b) || (!a && !b)) return null;
      if (Math.abs(a) > 90 || (a > 44 && a < 64 && b > 24 && b < 41)) [a, b] = [b, a];   // lng,lat order (Iran ranges)
      return Math.abs(a) <= 90 && Math.abs(b) <= 180 ? a + ',' + b : null; };
    const num = '(-?\\d{1,3}\\.\\d{3,})';
    const inText = t => { if (!t) return null; t = String(t); try { t = decodeURIComponent(t); } catch (_) {}
      let m = t.match(new RegExp('(?:lat|latitude)["\']?\\s*[:=]\\s*["\']?' + num + '[\\s\\S]{0,120}?(?:lng|lon|long|longitude)["\']?\\s*[:=]\\s*["\']?' + num, 'i'));
      if (m) return fix(m[1], m[2]);
      m = t.match(new RegExp('(?:lng|lon|long|longitude)["\']?\\s*[:=]\\s*["\']?' + num + '[\\s\\S]{0,120}?(?:lat|latitude)["\']?\\s*[:=]\\s*["\']?' + num, 'i'));
      if (m) return fix(m[2], m[1]);
      m = t.match(new RegExp(num + '\\s*(?:,|%2C|\\s)\\s*' + num));
      return m ? fix(m[1], m[2]) : null; };
    const fromData = x => { const d = x.dataset || {}, la = d.lat ?? d.latitude, lo = d.lng ?? d.lon ?? d.long ?? d.longitude;
      return la != null && lo != null ? fix(la, lo) : null; };
    const attrsOf = x => [...(x.attributes || [])].filter(a => /href|src|data-|style|onclick/i.test(a.name)).map(a => a.value);
    if (e) {
      for (const x of [e, ...e.querySelectorAll('a,img,iframe,[data-lat],[data-lng],[style*="background"],[href],[src]')].slice(0, 300))
        { const g = fromData(x); if (g) return g; for (const v of attrsOf(x)) { const g = inText(v); if (g) return g; } }
      for (let a = e.parentElement, i = 0; a && i < 4; a = a.parentElement, i++)
        for (const v of attrsOf(a)) { const g = inText(v); if (g) return g; }
    }
    for (const x of document.querySelectorAll('a[href*="maps"],a[href*="waze"],a[href*="neshan"],a[href*="balad"],iframe[src*="map"],img[src*="map"]'))
      { const g = inText(x.getAttribute('href') || x.getAttribute('src')); if (g) return g; }
    for (const sc of document.querySelectorAll('script#__NEXT_DATA__,script[type="application/ld+json"],script:not([src])'))
      { const g = inText((sc.textContent || '').slice(0, 2000000)); if (g) return g; }
    return null; };
"""

# Runs in the page. Returns what is under (x, y) and selectors that find it again.
PICK_JS = r"""
([x, y, where, itemSel, attr, up]) => {
  // readable text of an element without buttons, icons and "show more/less"-style controls
  const __ui = /^(مشاهده|نمایش|ادامه|مطلب|کامل|بیشتر|کمتر|بستن|show|read|see|view|more|less|[\s.…»«<>›‹\-])+$/i;
  const __txt = e => { if (!e) return null; const c = e.cloneNode(true);
    // a button that now shows a phone number (after "show number") is data, not a control
    c.querySelectorAll('button,[role=button],svg,script,style,noscript,input,select,textarea,.__hl').forEach(x => {
      if (!/[0-9۰-۹٠-٩]{5}/.test(x.textContent || '') || /^(svg|script|style|noscript)$/i.test(x.tagName)) x.remove(); });
    c.querySelectorAll('a,span,div,p,small,i').forEach(x => { const t = (x.textContent || '').trim();
      if (t && t.length < 30 && __ui.test(t)) x.remove(); });
    c.querySelectorAll('p,div,li,br,h1,h2,h3,h4,h5,h6,tr,section,article').forEach(x => x.append(' '));
    return (c.textContent || '').replace(/\s+/g, ' ').trim(); };
  document.querySelectorAll('.__pick').forEach(e => e.classList.remove('__pick'));
  let el = document.elementFromPoint(x, y);
  if (!el) return {error: 'چیزی زیر این نقطه نیست'};
  // "bigger": the surrounding box, `up` levels higher (never past the list card)
  const card0 = where === 'list' && itemSel ? el.closest(itemSel) : null;
  for (let i = 0; i < (up || 0) && el.parentElement && el !== card0 && el.parentElement !== document.body; i++) el = el.parentElement;
  if (attr === 'href' || attr === 'click') el = el.closest('a,button,[role=button],[onclick]') || el;
  if (attr === 'src') el = el.closest('img') || el.querySelector?.('img') || el;
  const esc = CSS.escape;
  // __pick is our own red outline from an earlier pick: never part of a selector
  const okClass = c => c !== '__pick' && c.length < 40 && !/\d{4,}|^(active|hover|focus|selected|open|show|is-|has-)/.test(c) && !/^(css|jsx|sc)-[a-z0-9]+$/i.test(c);
  const seg = e => { let s = e.tagName.toLowerCase(); const cls = [...e.classList].filter(okClass).slice(0, 3);
                     if (cls.length) s += '.' + cls.map(esc).join('.'); return s; };
  const path = (e, stop, tagsOnly) => { const p = []; while (e && e !== stop && e.nodeType === 1 && e !== document.documentElement) {
                     p.unshift(tagsOnly ? e.tagName.toLowerCase() : seg(e)); e = e.parentElement; } return p.join(' > '); };
  // siblings of the same kind: same tag and the classes most of them share (an extra "featured" class does not split them)
  const kind = a => { const same = a.parentElement ? [...a.parentElement.children].filter(c => c.tagName === a.tagName) : [a];
    const common = [...a.classList].filter(okClass).filter(k => same.filter(c => c.classList.contains(k)).length >= same.length * 0.6).slice(0, 3);
    const sel = a.tagName.toLowerCase() + common.map(k => '.' + esc(k)).join('');
    return {sel, list: same.filter(c => c.matches(sel))}; };
  /*__GEO__*/
  const value = e => attr === 'geo' ? (__geo(e) || '') : attr === 'click' ? (e.textContent || '').replace(/\s+/g, ' ').trim() : attr === 'href' ? (e.closest('a')?.href || '') : attr === 'src' ? (e.currentSrc || e.src || '')
                     : __txt(e);
  const mark = els => { document.querySelectorAll('.__pick').forEach(e => { e.classList.remove('__pick'); });
                        if (!document.getElementById('__pickcss')) { const st = document.createElement('style'); st.id = '__pickcss';
                          st.textContent = '.__pick{outline:3px solid #f43f5e!important;outline-offset:1px}'; document.head.appendChild(st); }
                        els.forEach(e => e.classList.add('__pick')); };
  if (where === 'list') {
    let card = itemSel ? el.closest(itemSel) : null, newCard = false;
    if (!card) {  // the repeated card: the ancestor with the most same-looking siblings
      let best = null, bestN = 1;
      for (let a = el; a && a.parentElement && a !== document.body; a = a.parentElement) {
        const n = kind(a).list.length;
        if (n > bestN) { best = a; bestN = n; }
      }
      if (!best) return {error: 'این مورد در لیستِ تکرارشونده‌ای نیست؛ روی یکی از کارت‌های لیست کلیک کنید'};
      card = best; newCard = true;
      // the card selector keeps only classes most sibling cards share ("featured" etc. would drop cards)
      itemSel = path(card.parentElement, null) + ' > ' + kind(card).sel;
    }
    const cards = [...document.querySelectorAll(itemSel)];
    // several ways to find the field again inside every card; cards differ (badges, optional rows), so the
    // ways that work in the most cards come first, and at read time each card uses the first one that works
    let rels = [':scope'];
    if (el !== card) {
      const cands = [':scope > ' + path(el, card), ':scope > ' + path(el, card, true), seg(el)];
      if (el.parentElement !== card) cands.push(seg(el.parentElement) + ' > ' + seg(el));
      if (attr === 'href') cands.push('a[href]');
      if (attr === 'src') cands.push('img');
      rels = [...new Set(cands)].filter(c => { try { return card.querySelector(c) === el; } catch (_) { return false; } })
        .map(c => [c, cards.filter(x => x.querySelector(c)).length]).sort((a, b) => b[1] - a[1]).map(x => x[0]);
      if (!rels.length) rels = [':scope > ' + path(el, card)];
    }
    const pick = c => { for (const r of rels) { const e = r === ':scope' ? c : c.querySelector(r); if (e) return e; } return null; };
    const found = cards.map(pick).filter(Boolean);
    mark(found);
    return {item_selector: itemSel, new_card: newCard, rels, rel: rels[0], rel_tags: rels[0], count: cards.length, filled: found.length,
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
  const positional = parts.join(' > ');
  const unique = c => { try { const m = document.querySelectorAll(c); return m.length === 1 && m[0] === el; } catch (_) { return false; } };
  const cands = [];
  if (el.id && !/\d{3,}/.test(el.id)) cands.push('#' + esc(el.id));
  for (const a of ['name', 'itemprop', 'data-testid', 'data-test', 'aria-label'])
    if (el.getAttribute(a)) cands.push(`${el.tagName.toLowerCase()}[${a}="${CSS.escape(el.getAttribute(a))}"]`);
  cands.push(seg(el));
  if (el.parentElement) cands.push(seg(el.parentElement) + ' > ' + seg(el));
  // nearest ancestor with an id or a distinctive class, then the element's own kind inside it
  for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
    if (a.id && !/\d{3,}/.test(a.id)) { cands.push('#' + esc(a.id) + ' ' + seg(el)); break; }
    const cls = [...a.classList].filter(okClass);
    if (cls.length && document.querySelectorAll(a.tagName.toLowerCase() + '.' + cls.map(esc).join('.')).length === 1) {
      cands.push(seg(a) + ' ' + seg(el)); break; }
  }
  const selectors = [...new Set(cands)].filter(unique);
  selectors.push(positional);
  const selector = selectors[0];
  mark([el]);
  // is this element inside a repeated, visibly framed card (a results list)? then the panel switches to list mode
  let listHint = 0;
  for (let a = el; a && a.parentElement && a !== document.body; a = a.parentElement) {
    // same-looking siblings that are also built the same way inside (doctor cards yes; a profile's different boxes no)
    const linked = c => c.matches('a[href]') || c.querySelector('a[href]');
    const alike = c => Math.abs(c.children.length - a.children.length) <= 2 && linked(c);
    const sibs = kind(a).list.filter(alike);
    if (sibs.length >= 3) {
      const cs = getComputedStyle(a), r = a.getBoundingClientRect();
      const framed = cs.boxShadow !== 'none' || (parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none')
                  || (cs.backgroundColor !== getComputedStyle(a.parentElement).backgroundColor && !/rgba\(.*,\s*0\)$/.test(cs.backgroundColor));
      if (framed && r.height > 60 && r.width > 200 && a.querySelector('a[href]')) { listHint = sibs.length; break; }
    }
  }
  // a "label: value" spot (spec tables, "برند: X" lines): remember the label, so other pages are read by the label
  // wherever its row sits, not by the position the row had on this page
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const lab = t => norm(t).replace(/[:：\s]+$/, '');
  const isLabel = t => { t = lab(t); return t.length >= 2 && t.length <= 40 && !/\d{3,}/.test(t); };
  let label = null, labelMode = null;
  // a "label: value" text is a whole row, never a label of something else
  const pair = t => /[:：]\s*\S/.test(norm(t));
  // looks like a label: "…:" or a header/bold title, or a cell of a table row / list item / definition list
  const labelish = x => /[:：]\s*$/.test(norm(x.textContent)) || /^(TH|DT|LABEL|B|STRONG)$/.test(x.tagName) || !!x.closest('tr,li,dl');
  if (attr === 'text') {
    const own = norm(el.textContent), prev = el.previousElementSibling;
    const m = own.match(/^([^:：]{2,40})[:：]\s*\S/);  // the picked text itself is "label: value"
    if (m && isLabel(m[1])) { label = lab(m[1]); labelMode = 'self'; }
    if (!label && prev && isLabel(prev.textContent) && !pair(prev.textContent) && labelish(prev) && norm(prev.textContent) !== own) {
      label = lab(prev.textContent); labelMode = 'next'; }
    for (let a = el.parentElement, i = 0; !label && a && i < 3 && a !== document.body; a = a.parentElement, i++) {
      if (norm(a.textContent).length > 200) break;
      const first = [...a.querySelectorAll('*')].find(x => !x.contains(el) && !el.contains(x) && !x.children.length && norm(x.textContent));
      if (first && (first.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) && isLabel(first.textContent) && !pair(first.textContent) && labelish(first)) {
        label = lab(first.textContent); labelMode = 'row'; }
    }
  }
  return {selector, selectors, count: document.querySelectorAll(selector).length, samples: [value(el)], list_hint: listHint,
          label, label_mode: labelMode};
}
"""

# Runs in the page while the mouse moves over the remote screen: what a click would take.
# mode 'box' climbs to the nearest visible box (border / shadow / own background), 'exact' keeps
# the element under the mouse. Returns its rectangle, the `up` count for PICK_JS and a text preview.
HOVER_JS = r"""
([x, y, mode, where, itemSel, attr]) => {
  // readable text of an element without buttons, icons and "show more/less"-style controls
  const __ui = /^(مشاهده|نمایش|ادامه|مطلب|کامل|بیشتر|کمتر|بستن|show|read|see|view|more|less|[\s.…»«<>›‹\-])+$/i;
  const __txt = e => { if (!e) return null; const c = e.cloneNode(true);
    // a button that now shows a phone number (after "show number") is data, not a control
    c.querySelectorAll('button,[role=button],svg,script,style,noscript,input,select,textarea,.__hl').forEach(x => {
      if (!/[0-9۰-۹٠-٩]{5}/.test(x.textContent || '') || /^(svg|script|style|noscript)$/i.test(x.tagName)) x.remove(); });
    c.querySelectorAll('a,span,div,p,small,i').forEach(x => { const t = (x.textContent || '').trim();
      if (t && t.length < 30 && __ui.test(t)) x.remove(); });
    c.querySelectorAll('p,div,li,br,h1,h2,h3,h4,h5,h6,tr,section,article').forEach(x => x.append(' '));
    return (c.textContent || '').replace(/\s+/g, ' ').trim(); };
  const el0 = document.elementFromPoint(x, y);
  if (!el0) return null;
  const card0 = where === 'list' && itemSel ? el0.closest(itemSel) : null;
  const vw = innerWidth, vh = innerHeight;
  const boxy = e => {
    const r = e.getBoundingClientRect();
    if (r.width < 120 || r.height < 28 || (r.width > vw * 0.97 && r.height > vh * 0.9)) return false;
    const cs = getComputedStyle(e), bg = cs.backgroundColor;
    const hasBg = bg && bg !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(bg);
    const pbg = e.parentElement ? getComputedStyle(e.parentElement).backgroundColor : '';
    return (parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none' && parseFloat(cs.borderBottomWidth) > 0)
        || cs.boxShadow !== 'none' || (hasBg && bg !== pbg);
  };
  let up = 0;
  if (mode === 'box') {
    let e = el0, n = 0;
    while (e && e !== document.body && !boxy(e) && e !== card0) { e = e.parentElement; n++; }
    up = e && e !== document.body ? n : 0;
  }
  let el = el0;  // same climbing as PICK_JS
  for (let i = 0; i < up && el.parentElement && el !== card0 && el.parentElement !== document.body; i++) el = el.parentElement;
  if (attr === 'href' || attr === 'click') el = el.closest('a,button,[role=button],[onclick]') || el;
  if (attr === 'src') el = el.closest('img') || el;
  const r = el.getBoundingClientRect();
  return {up, x: r.left, y: r.top, w: r.width, h: r.height,
          text: (__txt(el) || '').slice(0, 140)};
}
"""

# Runs in the page: every link that looks like the sample profile link (same site, same address shape).
# The shape keeps the first path part (e.g. /doctor/) and the number of parts; ids and names vary.
LINKS_JS = r"""
(sample) => {
  const s = new URL(sample, location.href);
  const parts = s.pathname.replace(/\/$/, '').split('/').filter(Boolean);
  if (!parts.length) return {pattern: null, links: []};
  const esc = t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = '^/' + parts.map((p, i) => i === 0 ? esc(decodeURIComponent(p)) : '[^/]+').join('/') + '/?$';
  const re = new RegExp(pattern), seen = new Set(), links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let u; try { u = new URL(a.href); } catch (_) { continue; }
    if (u.host !== s.host || !re.test(decodeURIComponent(u.pathname))) continue;
    const h = u.origin + u.pathname + u.search;
    if (!seen.has(h)) { seen.add(h); links.push(h); }
  }
  return {pattern, links};
}
"""

# Runs in the page before reading it: open "show more" style expanders (not links that navigate).
EXPAND_JS = r"""
() => { let n = 0;
  for (const e of document.querySelectorAll('button,[role=button],span,div,a,p')) {
    const t = (e.textContent || '').trim();
    if (t.length > 25 || !/^(مشاهده|نمایش|ادامه|بیشتر|show more|read more|see more)/i.test(t) || /کمتر|less/i.test(t)) continue;
    if (/شماره|تلفن|تماس|phone/i.test(t)) continue;           // "show number" is the user's own button
    if (e.tagName === 'A' && e.getAttribute('href') && !e.getAttribute('href').startsWith('#')) continue;
    if (e.children.length > 3) continue;
    try { e.click(); n++; } catch (_) {}
  }
  return n; }
"""

# Runs in the page: read every card of a list page.
LIST_JS = r"""
([itemSel, fields]) => {
  // readable text of an element without buttons, icons and "show more/less"-style controls
  const __ui = /^(مشاهده|نمایش|ادامه|مطلب|کامل|بیشتر|کمتر|بستن|show|read|see|view|more|less|[\s.…»«<>›‹\-])+$/i;
  const __txt = e => { if (!e) return null; const c = e.cloneNode(true);
    // a button that now shows a phone number (after "show number") is data, not a control
    c.querySelectorAll('button,[role=button],svg,script,style,noscript,input,select,textarea,.__hl').forEach(x => {
      if (!/[0-9۰-۹٠-٩]{5}/.test(x.textContent || '') || /^(svg|script|style|noscript)$/i.test(x.tagName)) x.remove(); });
    c.querySelectorAll('a,span,div,p,small,i').forEach(x => { const t = (x.textContent || '').trim();
      if (t && t.length < 30 && __ui.test(t)) x.remove(); });
    c.querySelectorAll('p,div,li,br,h1,h2,h3,h4,h5,h6,tr,section,article').forEach(x => x.append(' '));
    return (c.textContent || '').replace(/\s+/g, ' ').trim(); };
  return [...document.querySelectorAll(itemSel)].map(c => {
  const o = {};
  for (const f of fields) {
    // older settings stored one child path (without ":scope >"), newer ones a list of ways (see PICK_JS)
    const ways = f.rels && f.rels.length ? f.rels : [f.rel, f.rel_tags].filter(Boolean).map(r => r === ':scope' ? r : ':scope > ' + r);
    let e = null;
    for (const r of ways) { try { e = r === ':scope' ? c : c.querySelector(r); } catch (_) { e = null; } if (e) break; }
    o[f.name] = !e ? null : f.attr === 'href' ? (e.closest('a')?.href || null) : f.attr === 'src' ? (e.currentSrc || e.src || null)
              : __txt(e);
  }
  return o;
}); }
"""

PICK_JS = PICK_JS.replace("/*__GEO__*/", GEO_FN)

DETAIL_JS = r"""
(fields) => {
  // readable text of an element without buttons, icons and "show more/less"-style controls
  const __ui = /^(مشاهده|نمایش|ادامه|مطلب|کامل|بیشتر|کمتر|بستن|show|read|see|view|more|less|[\s.…»«<>›‹\-])+$/i;
  const __txt = e => { if (!e) return null; const c = e.cloneNode(true);
    // a button that now shows a phone number (after "show number") is data, not a control
    c.querySelectorAll('button,[role=button],svg,script,style,noscript,input,select,textarea,.__hl').forEach(x => {
      if (!/[0-9۰-۹٠-٩]{5}/.test(x.textContent || '') || /^(svg|script|style|noscript)$/i.test(x.tagName)) x.remove(); });
    c.querySelectorAll('a,span,div,p,small,i').forEach(x => { const t = (x.textContent || '').trim();
      if (t && t.length < 30 && __ui.test(t)) x.remove(); });
    c.querySelectorAll('p,div,li,br,h1,h2,h3,h4,h5,h6,tr,section,article').forEach(x => x.append(' '));
    return (c.textContent || '').replace(/\s+/g, ' ').trim(); };
  /*__GEO__*/
  const o = {};
  const __norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const __lab = t => __norm(t).replace(/[:：\s]+$/, '');
  // the value next to a label ("کد کالا" → "831290"), wherever that row sits on this page
  const byLabel = f => {
    const want = f.label, cut = t => { t = __norm(t); const i = t.indexOf(want);
      return i < 0 ? t : t.slice(i + want.length).replace(/^[\s:：\-–|]+/, '').trim(); };
    for (const x of document.querySelectorAll('body *')) {
      if (x.children.length > 3) continue;
      const t = __lab(x.textContent);
      if (f.label_mode === 'self') {
        if (t.startsWith(want) && /^[:：]/.test(__norm(x.textContent).slice(want.length).trim()) && x.children.length <= 1) {
          const v = cut(__txt(x)); if (v) return v; }
        continue;
      }
      if (t !== want) continue;
      if (f.label_mode === 'next' && x.nextElementSibling) { const v = __txt(x.nextElementSibling); if (v) return v; }
      for (let a = x.parentElement, i = 0; a && i < 3 && a !== document.body; a = a.parentElement, i++) {
        const v = cut(__txt(a)); if (v) return v; }
    }
    return null; };
  for (const f of fields) { let e = null;
    // a labelled field is read by its label only: when this page has no such row the field is empty (by position
    // it would pick whatever row sits there, e.g. the weight instead of a missing category)
    if (f.label && (f.attr || 'text') === 'text') { o[f.name] = byLabel(f); continue; }
    for (const sel of (f.selectors && f.selectors.length ? f.selectors : [f.selector])) { try { e = document.querySelector(sel); } catch (_) {} if (e) break; }
    if (f.attr === 'geo') { o[f.name] = __geo(e); continue; }
    o[f.name] = !e ? null : f.attr === 'href' ? (e.closest('a')?.href || null) : f.attr === 'src' ? (e.currentSrc || e.src || null)
              : __txt(e);
    if (e && !o[f.name]) {  // a phone shown only as a tel: link
      const t = e.closest('a[href^="tel:"]') || e.querySelector('a[href^="tel:"]');
      if (t) o[f.name] = decodeURIComponent(t.getAttribute('href').slice(4)); } }
  return o; }
"""
DETAIL_JS = DETAIL_JS.replace("/*__GEO__*/", GEO_FN)


FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def clean(value, f: dict):
    """The field's text edits: keep text after/before a marker, remove words, replace words, digits."""
    if not isinstance(value, str):
        return value
    v = value
    if f.get("after") and f["after"] in v:
        v = v.split(f["after"], 1)[1]
    if f.get("before") and f["before"] in v:
        v = v.split(f["before"], 1)[0]
    for w in f.get("remove") or []:
        if w:
            v = v.replace(w, "")
    for pair in f.get("replace") or []:
        if len(pair) == 2 and pair[0]:
            v = v.replace(pair[0], pair[1])
    if f.get("digits") == "en":
        v = v.translate(FA_DIGITS)
    return re.sub(r"\s+", " ", v).strip(" :،,-–|")


def output_row(row: dict, cfg: dict) -> dict:
    """A stored (raw) record as shown/exported: picked fields in order, each field's edits, then the section's rules."""
    rules = [r for r in cfg.get("rules") or [] if len(r) == 2 and r[0]]
    out = {}
    for f in cfg.get("fields", []):
        v = clean(row.get(f["name"]), f)
        if isinstance(v, str):
            for a, b in rules:
                v = v.replace(a, b)
            v = v.strip()
        out[f["name"]] = v
    if cfg.get("flow") == "profiles":
        out["لینک پروفایل"] = row.get("__url")
    return out


def session_file(category_id: int) -> Path:
    return SESSIONS / f"cat_{category_id}.json"


def page_url(list_url: str, n: int) -> str:
    """Page n of a list: fills {page}; without it page 1 is the address itself and later pages get ?page=n."""
    if "{page}" in list_url:
        return list_url.replace("{page}", str(n))
    if n == 1:
        return list_url
    u = urlsplit(list_url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "page"] + [("page", str(n))]
    return urlunsplit(u._replace(query=urlencode(q)))


def _key(row: dict, link_field: str | None) -> str:
    if link_field and row.get(link_field):
        return "url:" + row[link_field].split("#")[0]
    return "h:" + hashlib.sha1(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _norm_profile(href: str, sample: str) -> str:
    """One address per profile: no www., no #fragment, no trailing slash, no ?query unless the sample link needs one."""
    u = urlsplit(href)
    query = u.query if urlsplit(sample).query else ""
    host = u.netloc.lower().removeprefix("www.")  # www.site.com and site.com are the same profile
    return urlunsplit((u.scheme, host, u.path.rstrip("/") or "/", query, ""))


def _dedupe_profiles(category_id: int, sample: str):
    """Rows of the older flows (list cards, single pages) and the same profile saved under two addresses."""
    with db.conn() as c:
        c.execute("DELETE FROM items WHERE category_id=? AND (source='لیست' OR item_key NOT LIKE 'url:%')", (category_id,))
        rows = c.execute("SELECT id, item_key, data, last_seen FROM items WHERE category_id=? ORDER BY last_seen DESC, id DESC",
                         (category_id,)).fetchall()
        kept: dict[str, dict] = {}
        for r in rows:  # newest first: an older copy of a profile goes
            key = "url:" + _norm_profile(r["item_key"][4:], sample)
            if key in kept:  # an older copy: its values only fill what the newest copy lacks
                k = kept[key]
                k["data"] = {**{a: b for a, b in json.loads(r["data"]).items() if b not in (None, "")}, **k["data"]}
                k["changed"] = True
                c.execute("DELETE FROM items WHERE id=?", (r["id"],))
            else:
                kept[key] = {"id": r["id"], "item_key": r["item_key"],
                             "data": {a: b for a, b in json.loads(r["data"]).items() if b not in (None, "")}, "changed": False}
        for key, r in kept.items():  # renamed only after the copies are gone (keys are unique)
            if key != r["item_key"] or r["changed"]:
                r["data"]["__url"] = key[4:]
                c.execute("UPDATE items SET item_key=?, data=? WHERE id=?",
                          (key, json.dumps(r["data"], ensure_ascii=False), r["id"]))


HEAVY = {"image", "media", "font"}
DEAD_BROWSER = re.compile(r"has been closed|Target crashed|Browser closed|Connection closed|crashed")
PROGRESS = DATA_DIR / "progress"


def progress_file(category_id: int) -> Path:
    """Where a running profiles collection keeps how far it got, so a restart continues instead of starting over."""
    return PROGRESS / f"cat_{category_id}.json"


def _load_progress(category_id: int, sig: str) -> dict | None:
    f = progress_file(category_id)
    try:
        st = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return st if st.get("sig") == sig else None  # other addresses / link: a new collection


def _save_progress(category_id: int, st: dict):
    PROGRESS.mkdir(parents=True, exist_ok=True)
    tmp = progress_file(category_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(progress_file(category_id))
def stop_file(site_id: int, category_id: int | None = None) -> Path:
    return PROGRESS / (f"stop_cat_{category_id}" if category_id else f"stop_site_{site_id}")


def stop_requested(site_id, category_id=None) -> bool:
    """The panel's stop button (a file: each collection runs in its own process)."""
    return (site_id is not None and stop_file(site_id).exists()) or bool(category_id and stop_file(site_id, category_id).exists())


def request_stop(site_id: int, category_id: int | None = None):
    PROGRESS.mkdir(parents=True, exist_ok=True)
    stop_file(site_id, category_id).touch()


def clear_stop(site_id: int, category_id: int | None = None):
    stop_file(site_id, category_id).unlink(missing_ok=True)


def run_profiles(cat: dict, site: dict, progress, log) -> dict:
    """List pages only give profile links (by the shape of one picked link); every field is read on the profiles."""
    from playwright.sync_api import TimeoutError as PwTimeout, sync_playwright

    cfg = json.loads(cat.get("config") or "{}")
    fields = [f for f in cfg.get("fields", []) if f.get("selector") or f.get("selectors")]
    urls = [u.strip() for u in (cfg.get("list_url") or "").splitlines() if u.strip()]
    sample = cfg.get("link_sample")
    if not urls or not sample:
        raise ValueError("در قسمت ۱ آدرس صفحه لیست را بدهید و «لینک پروفایل» را روی یکی از پزشکان انتخاب کنید")
    if not fields:
        raise ValueError("در قسمت ۲ هیچ اطلاعاتی روی صفحه پروفایل انتخاب نشده است")
    outside = [u for u in urls if not host_allowed(u.replace("{page}", "1"), site["allowed_domains"])]
    if len(outside) == len(urls):  # say why, instead of "nothing found on the list"
        host = urlsplit(urls[0]).hostname or urls[0]
        raise ValueError(f"آدرس لیست روی {host} است ولی این سایت فقط برای {'، '.join(site['allowed_domains'])} قرارداد دارد؛ "
                         f"این بخش را در سایت مربوط به {host} بسازید، یا اگر قرارداد شامل آن هم هست، {host} را در «تنظیمات سایت» به دامنه‌ها اضافه کنید")
    delay, max_pages = max(site["delay_seconds"], 0), int(cfg.get("max_pages") or 200)
    stats = {"pages": 0, "rows": 0, "new": 0, "details": 0}
    # how far an earlier run of the same addresses got (it was cut off by a restart): continue from there
    sig = hashlib.sha1(json.dumps([urls, sample]).encode()).hexdigest()[:16]
    st = _load_progress(cat["id"], sig)
    if st:
        st["attempts"] = st.get("attempts", 0) + 1
    else:
        st = {"sig": sig, "started": db.now(), "lists": {}, "profiles": [], "origin": {}, "read": 0, "attempts": 0}
    profiles: list[str] = st["profiles"]
    read_at_start = st["read"]
    origin: dict[str, str] = st["origin"]  # profile -> its list address (the group in the panel)
    _save_progress(cat["id"], st)
    _dedupe_profiles(cat["id"], sample)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions", "--mute-audio",
                                           "--js-flags=--max-old-space-size=512"])
        sess = session_file(cat["id"])

        def fresh(state):
            """A new browser tab set. Pictures, videos and fonts are not loaded: only text and addresses are read
            (a picture field keeps its address), and a long run stays light for the server."""
            c = browser.new_context(viewport={"width": 1280, "height": 900}, locale="fa-IR", storage_state=state)
            c.route("**/*", lambda r: r.abort() if r.request.resource_type in HEAVY
                    or (r.request.resource_type != "document" and is_tracker(r.request.url)) else r.continue_())
            pg = c.new_page()
            pg.on("dialog", lambda d: d.dismiss())  # an alert() would freeze every read of the page
            return c, pg

        ctx, page = fresh(str(sess) if sess.exists() else None)
        opened = [0]

        def stopped() -> bool:
            if stop_requested(site.get("id"), cat["id"]):
                stats["stopped"] = True
                say("متوقف شد (دکمه توقف)")
                return True
            return False

        def say(msg: str):  # shows in: journalctl -u har-scraper -f
            print(f"[بخش {cat['id']}] {msg}", flush=True)

        def open_(url: str) -> bool:
            if not host_allowed(url, site["allowed_domains"]):
                log({"url": url, "error": "دامنه خارج از قرارداد است؛ باز نشد"})
                return False
            if opened[0]:
                time.sleep(delay)
            opened[0] += 1
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                return True
            except Exception as e:
                if DEAD_BROWSER.search(str(e)):  # not this page's fault: stop here, the progress file has the rest
                    raise
                log({"url": url, "error": str(e)[:300]})
                say(f"باز نشد: {url} — {str(e)[:150]}")
                return False

        def links_now() -> set:
            try:
                return {_norm_profile(h, sample) for h in page.evaluate(LINKS_JS, sample)["links"]}
            except Exception:  # the page is navigating
                return set()

        def changed(before: set, wait_ms: int = 12000) -> bool:
            """Did the list show other profiles? (the next page can take a while to load)"""
            for _ in range(wait_ms // 500):
                page.wait_for_timeout(500)
                if links_now() - before:
                    return True
            return False

        def page_links() -> list[str]:
            # wait for the links, then scroll until no new ones load
            for _ in range(30):
                if page.evaluate(LINKS_JS, sample)["links"]:
                    break
                page.wait_for_timeout(500)
            seen = -1
            for _ in range(25):
                links = page.evaluate(LINKS_JS, sample)["links"]
                if len(links) == seen:
                    break
                seen = len(links)
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(900)
            return links

        def click_next(n: int, new_only: bool = True) -> bool:
            """Page n: the picked "next" button, else the pagination button with the number n, else ?page=n.
            Moved = profiles not collected yet appear (a wrong button may lead back to an earlier page);
            new_only=False when walking again over pages read before a restart."""
            before = links_now() | (set(profiles) if new_only else set())
            try:
                page.mouse.wheel(0, 20000)  # pagination often renders at the bottom only
                page.wait_for_timeout(400)
            except Exception:
                pass
            for sel in [] if next_bad[0] else cfg.get("next_selectors") or []:
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible() and loc.is_enabled():
                        loc.click(timeout=5000)
                        if changed(before):
                            return True
                        say(f"صفحه {n}: دکمه صفحه بعد زده شد ولی پزشک جدیدی نیامد؛ از این به بعد شماره صفحه‌ها زده می‌شود")
                        next_bad[0] = True
                        break
                except Exception:
                    continue
            fa = str(n).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
            for t in (str(n), fa):
                try:
                    if page.evaluate(CLICK_TEXT_JS, t) and changed(before):
                        say(f"صفحه {n}: با دکمه شماره صفحه رفت")
                        return True
                except Exception:
                    pass
            for base in [page_base[0]]:
                if base and open_(page_url(base, n)):
                    page.wait_for_timeout(1500)
                    if links_now() - before:
                        say(f"صفحه {n}: با آدرس ?page={n} رفت")
                        return True
            return False

        def to_next(base: str, n: int, new_only: bool = True) -> bool:
            if cfg.get("next_selectors"):
                return click_next(n, new_only)
            return open_(page_url(base, n))

        # 1) list pages: collect profile links
        page_base, next_bad = [None], [False]
        for base in urls:
            page_base[0] = base
            lst = st["lists"].setdefault(base, {"page": 0, "done": False})
            if lst["done"]:
                say(f"صفحه لیست (قبلاً کامل شده): {base}")
                continue
            say(f"صفحه لیست: {base}")
            if not open_(base.replace("{page}", "1")):
                continue
            n, lst_start = 1, lst["page"]
            if lst["page"]:
                # continue after the last page read before the restart: jump there, else walk the pages again
                target, jumped = lst["page"] + 1, False
                if open_(page_url(base, target)):
                    page_links()
                    jumped = bool(links_now() - set(profiles))
                if jumped:
                    n = target
                    say(f"ادامه از صفحه {n}")
                else:
                    say(f"رفتن دوباره تا صفحه {lst['page']}…")
                    open_(base.replace("{page}", "1"))
                    while n < lst["page"] and not stopped() and to_next(base, n + 1, new_only=False):
                        n += 1
                    if n < lst["page"] or stopped() or not to_next(base, n + 1):
                        lst["done"] = not stopped()
                        _save_progress(cat["id"], st)
                        continue
                    n += 1
                    say(f"ادامه از صفحه {n}")
            while n <= max_pages and not stopped():
                links = page_links()
                links = list(dict.fromkeys(_norm_profile(h, sample) for h in links))
                new = [h for h in links if h not in origin and host_allowed(h, site["allowed_domains"])]
                log({"url": page.url, "links": len(links), "new": len(new)})
                say(f"صفحه {n}: {len(links)} لینک پروفایل، {len(new)} جدید (تا اینجا {len(profiles) + len(new)})")
                if not new:  # past the last page
                    lst["done"] = True
                    break
                profiles.extend(new)
                for h in new:
                    origin[h] = source_of(base.replace("{page}", "1"))
                lst["page"] = n
                _save_progress(cat["id"], st)
                stats["pages"] += 1
                progress({**stats, "rows": len(profiles), "list_page": n, "list_resumed": bool(lst_start)})
                if n == max_pages or not to_next(base, n + 1):
                    if n < max_pages:
                        say(f"صفحه {n + 1}: صفحه بعدی پیدا نشد؛ پایان این لیست")
                    lst["done"] = not stopped()
                    break
                n += 1
            _save_progress(cat["id"], st)

        # 2) every profile: read the picked fields
        empty = {f["name"]: 0 for f in fields}
        done_before = st["read"] if not stopped() else len(profiles)
        if not stats.get("stopped"):
            say(f"{len(profiles)} پروفایل پیدا شد؛ خواندن پروفایل‌ها…"
                + (f" (ادامه از پروفایل {done_before + 1})" if done_before else ""))
        for i, href in enumerate(profiles, 1):
            if i <= done_before:  # read before the restart
                continue
            if stopped():
                break
            if i % 40 == 0:  # memory of a browser that opened hundreds of pages keeps growing
                try:
                    state = ctx.storage_state()  # keep the login
                    ctx.close()
                except Exception:
                    state = None
                ctx, page = fresh(state)
            say(f"پروفایل {i} از {len(profiles)}: {href}")
            if not open_(href):
                continue
            for sel in [s for f in fields[:1] for s in (f.get("selectors") or [f.get("selector")]) if s]:
                try:
                    page.wait_for_selector(sel, timeout=15000, state="attached")
                    break
                except PwTimeout:
                    continue
            try:
                if page.evaluate(EXPAND_JS):
                    page.wait_for_timeout(700)
            except Exception:
                pass
            clicked = _click_show(page, cfg)
            row = _read_settled(page, fields, 8000) if clicked else _read_settled(page, fields, 3000, only_if_missing=True)
            log({"url": href, "found": len(row), "clicked": clicked})
            stats["details"] += 1
            for f in fields:
                empty[f["name"]] += f["name"] not in row
            if row:
                stats["rows"] += 1
                stats["new"] += _save(cat["id"], [{**row, "__url": href}], "__url", href, single=True,
                                      source=origin.get(href))
            st["read"] = i
            if i % 10 == 0:
                st["attempts"] = 0  # it moves forward: a later restart may continue it again
                _save_progress(cat["id"], st)
            progress({**stats, "at": i, "total": len(profiles), "from": read_at_start})
        browser.close()
    stats["empty"] = {k: v for k, v in empty.items() if v}
    stats["total"], stats["from"] = len(profiles), read_at_start
    finished = (not stats.get("stopped") and st["read"] >= len(profiles)
                and all(st["lists"].get(b, {}).get("done") for b in urls))
    if finished:
        progress_file(cat["id"]).unlink(missing_ok=True)  # complete: the next run is a new pass
    else:  # stop button, or a list page that did not open: the next run continues from here
        _save_progress(cat["id"], st)
    return stats


# Runs in the page: click every visible element whose whole text is `text` (e.g. "نمایش شماره" of each office).
CLICK_TEXT_JS = r"""
(text) => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const hits = [...document.querySelectorAll('button,[role=button],a,span,div,p,li')]
    .filter(e => norm(e.textContent) === text && e.offsetParent !== null);
  // the innermost match only (a wrapper div has the same text as its button)
  const inner = hits.filter(e => !hits.some(o => o !== e && e.contains(o))).slice(0, 10);
  for (const e of inner) { try { e.click(); } catch (_) {} }
  return inner.length; }
"""


def _clicks(cfg: dict) -> list[dict]:
    """The "show ..." buttons, in order. Older settings had a single one."""
    if cfg.get("clicks"):
        return [c for c in cfg["clicks"] if c.get("text") or c.get("selectors")]
    if cfg.get("detail_click") or cfg.get("click_text"):
        return [{"selectors": cfg.get("click_selectors") or [cfg.get("detail_click")], "text": cfg.get("click_text") or ""}]
    return []


def _click_show(page, cfg: dict) -> bool:
    """Click each "show number"-style button in order: by its text (every copy on the page), else by its selectors."""
    def press(btn) -> int:
        n = 0
        if btn.get("text"):
            try:
                n = page.evaluate(CLICK_TEXT_JS, btn["text"])
            except Exception:
                n = 0
        if not n:
            for sel in [x for x in btn.get("selectors") or [] if x]:
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=5000)
                        return 1
                except Exception:
                    continue
        return n

    clicked = False
    for i, btn in enumerate(_clicks(cfg)):
        # a later button (e.g. "confirm" in a small window) appears only after the one before: wait for it
        tries = 8 if i and clicked else 1
        for _ in range(tries):
            if press(btn):
                clicked = True
                page.wait_for_timeout(700)
                break
            if tries > 1:
                page.wait_for_timeout(500)
    return clicked


def _read_settled(page, fields: list, wait_ms: int, only_if_missing: bool = False) -> dict:
    """Read the fields; after a click, keep reading until nothing new appears (the number loads late)."""
    read = lambda: {k: v for k, v in page.evaluate(DETAIL_JS, fields).items() if v not in (None, "")}
    row = read()
    if not wait_ms or (only_if_missing and len(row) == len(fields)):
        return row
    page.wait_for_timeout(1000)
    waited, last, same = 1000, None, 0
    while waited < wait_ms:
        try:
            row = read()
        except Exception:  # the click navigated: read the new page
            page.wait_for_timeout(500)
            waited += 500
            continue
        same = same + 1 if row == last else 0
        if len(row) == len(fields) and same >= 1:
            break
        if same >= 3:
            break
        last = row
        page.wait_for_timeout(500)
        waited += 500
    return row


def run(cat: dict, site: dict, progress, log) -> dict:
    """Read every address of the section. progress(stats); log(item) records each page opened.

    One address per line: with {page} it is a list walked page by page (then each record's detail
    page if detail fields are picked); without {page} it is a single page whose fields make one record."""
    if json.loads(cat.get("config") or "{}").get("flow") == "profiles":
        return run_profiles(cat, site, progress, log)
    from playwright.sync_api import TimeoutError as PwTimeout, sync_playwright

    cfg = json.loads(cat.get("config") or "{}")
    list_fields = [f for f in cfg.get("fields", []) if f.get("page") == "list" and f.get("rel")]
    detail_fields = [f for f in cfg.get("fields", []) if f.get("page") == "detail" and f.get("selector")]
    link = cfg.get("detail_link")
    urls = [u.strip() for u in (cfg.get("list_url") or "").splitlines() if u.strip()]
    if not urls:
        raise ValueError("اول آدرس صفحه را در قدم ۱ بنویسید")
    if not (list_fields and cfg.get("item_selector")) and not detail_fields:
        raise ValueError("هنوز هیچ اطلاعاتی روی صفحه انتخاب نشده است")
    delay = max(site["delay_seconds"], 0)
    stats = {"pages": 0, "rows": 0, "new": 0, "details": 0}
    max_pages = int(cfg.get("max_pages") or 200)

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

        def read_page() -> dict:
            """The picked page fields of the open page (after the optional button click)."""
            try:
                first = detail_fields[0]
                page.wait_for_selector(", ".join(first.get("selectors") or [first["selector"]]), timeout=15000, state="attached")
            except PwTimeout:
                pass
            if cfg.get("detail_click"):
                try:
                    page.click(cfg["detail_click"], timeout=5000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
            try:  # collapsed boxes: open "show more" so the full text is read
                if page.evaluate(EXPAND_JS):
                    page.wait_for_timeout(700)
            except Exception:
                pass
            return page.evaluate(DETAIL_JS, detail_fields)

        def list_pages(base: str):
            prev_first = None
            for n in range(1, max_pages + 1):
                url = page_url(base, n)
                if not open_(url):
                    break
                try:
                    page.wait_for_selector(cfg["item_selector"], timeout=15000)
                except PwTimeout:
                    log({"url": url, "rows": 0})
                    break
                # cards that load while scrolling: scroll until no new ones appear
                seen = -1
                for _ in range(25):
                    count = page.locator(cfg["item_selector"]).count()
                    if count == seen:
                        break
                    seen = count
                    page.mouse.wheel(0, 20000)
                    page.wait_for_timeout(900)
                cards = page.evaluate(LIST_JS, [cfg["item_selector"], list_fields])
                rows = [r for r in cards if any(r.values())]
                log({"url": url, "cards": len(cards), "rows": len(rows)})
                first = json.dumps(cards[:2], ensure_ascii=False)
                if not cards or first == prev_first:  # past the last page
                    break
                prev_first = first
                stats["pages"] += 1
                stats["rows"] += len(rows)
                stats["new"] += _save(cat["id"], rows, link, url)
                progress(stats)

        for base in urls:
            if ("{page}" in base or cfg.get("is_list")) and list_fields and cfg.get("item_selector"):
                list_pages(base)
            elif detail_fields and open_(base.replace("{page}", "1")):
                # a single page: its picked fields make one record
                row = {k: v for k, v in read_page().items() if v not in (None, "")}
                log({"url": base, "found": len(row)})
                if row:
                    stats["pages"] += 1
                    stats["rows"] += 1
                    stats["new"] += _save(cat["id"], [{**row, "__url": base}], "__url", base, single=True)
                    progress(stats)

        # detail pages of list records that do not have them yet
        if detail_fields and link:
            with db.conn() as c:
                todo = [(r["id"], json.loads(r["data"])) for r in c.execute(
                    "SELECT id, data FROM items WHERE category_id=? AND details_done NOT LIKE '%[v]%'", (cat["id"],))]
            for item_id, rec in todo:
                href = rec.get(link)
                if not href or not open_(href):
                    continue
                extra = read_page()
                log({"url": href, "detail": True, "found": sum(v is not None for v in extra.values())})
                with db.conn() as c:
                    rec.update({k: v for k, v in extra.items() if v not in (None, "")})
                    c.execute("UPDATE items SET data=?, details_done=details_done || '[v]', last_seen=? WHERE id=?",
                              (json.dumps(rec, ensure_ascii=False), db.now(), item_id))
                stats["details"] += 1
                progress(stats)
        browser.close()
    return stats


def _save(category_id: int, rows: list[dict], link: str | None, url: str, single: bool = False,
          source: str | None = None) -> int:
    """source: the group shown in the panel (profiles flow: which list address the profile came from)."""
    new, ts = 0, db.now()
    with db.conn() as c:
        for row in rows:
            key = _key(row, link)
            old = c.execute("SELECT id, data FROM items WHERE category_id=? AND item_key=?", (category_id, key)).fetchone()
            if old:  # keep detail fields gathered earlier
                merged = {**json.loads(old["data"]), **{k: v for k, v in row.items() if v not in (None, "")}}
                c.execute("UPDATE items SET data=?, source_url=?, last_seen=? WHERE id=?",
                          (json.dumps(merged, ensure_ascii=False), url, ts, old["id"]))
                if source:
                    c.execute("UPDATE items SET source=? WHERE id=?", (source, old["id"]))
            else:
                # a single page is complete already: no detail page to visit
                c.execute("INSERT INTO items(category_id, item_key, data, source_url, source, first_seen, last_seen, details_done) VALUES (?,?,?,?,?,?,?,?)",
                          (category_id, key, json.dumps(row, ensure_ascii=False), url, source or ("صفحه" if single else "لیست"), ts, ts, "[v]" if single else ""))
                new += 1
    return new

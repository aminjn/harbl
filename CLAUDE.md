# HAR Scraper — handoff notes for Claude Code

## What this is
A web panel (Persian, RTL) for scraping sites **the owner has a contract with**. The user uploads a browser HAR file per site; the system extracts the API requests (XHR/fetch/JSON) with their headers/cookies, replays them with httpx, and stores responses as JSON. Runs are manual or scheduled.

Owner's language: Persian. UI text and user-facing messages are Persian.

## Stack
- Python 3.10+ · FastAPI · httpx · APScheduler (BackgroundScheduler, in-process) · SQLite
- Single static page `app/static/index.html` (vanilla JS, no build step)
- Auth: HTTP Basic, credentials from env (`HARSCRAPER_USER`, `HARSCRAPER_PASS`)

## Layout
```
app/main.py        FastAPI routes, Basic auth, scheduler wiring, no-cache middleware
app/har.py         HAR parsing → request templates (filters static files, non-API, out-of-domain, dupes)
app/scraper.py     run_site(): contract checks, replay, JSON output to DATA_DIR/output/site_<id>/
app/db.py          SQLite schema: sites, requests, runs
app/config.py      env-based config
app/static/index.html  panel
deploy/install.sh  Ubuntu installer (systemd + nginx + ufw + optional certbot)
deploy/wheels/     offline wheels (py3.10–3.13, x86_64) for servers without PyPI access
```

## Contract safeguards (keep these)
- Requests are only sent to hosts in the site's `allowed_domains` (checked at HAR import AND at replay).
- Runs refuse after `contract_expires`, when site disabled, or with no `contract_ref`.
- Configurable delay between requests; optional UA suffix (`HARSCRAPER_UA_SUFFIX`).

## Production deployment (current)
- ArvanCloud Ubuntu server, panel at **har.servein.ir** (currently plain http, behind Arvan CDN).
- Installed by `deploy/install.sh` (or the copy-paste version that embeds all files):
  - code: `/opt/har-scraper` (venv in `/opt/har-scraper/venv`)
  - data: `/var/lib/har-scraper` (SQLite + JSON outputs), owned by system user `harscraper`
  - env: `/etc/har-scraper.env`
  - service: `har-scraper.service` → uvicorn 127.0.0.1:8000, **1 worker only** (scheduler is in-process; >1 worker = duplicate scheduled runs)
  - nginx site: `/etc/nginx/sites-available/har-scraper`, `client_max_body_size 200M`
- Useful: `systemctl status har-scraper`, `journalctl -u har-scraper -n 100`, `tail /var/log/nginx/error.log`
- After changing code in `/opt/har-scraper`: `sudo systemctl restart har-scraper`

## Status
- Tested end-to-end locally (mock API + sample HAR): import, filtering, replay with cookies, POST bodies, JSON output, schedule, expired-contract block, auth.
- Full installer run tested on clean Ubuntu 24.04 (with systemd stubbed).
- Panel loads in production (screenshot confirmed), but the owner reports **"it doesn't work correctly"** — exact symptom not yet known. Ask the owner what fails (saving a site? HAR upload? run? output?) and check browser DevTools → Network/Console + `journalctl`.

## Fixes already in code (Oct 2026)
- Chrome 130+ exports *sanitized* HAR by default (no Cookie/Authorization) → upload now reports `no_auth` and the panel explains how to export with sensitive data.
- Per-site `extra_headers` (e.g. `Cookie: ...`) override HAR headers at replay; values are never returned to the browser (only `extra_header_names`). PUT without `extra_headers` keeps them; an empty value removes one.
- Upload stats list skipped `outside_hosts`; Firefox `postData.params` bodies are rebuilt; non-ASCII header values are percent-encoded.
- Runs never stay stuck in `running` (generic except + reset on startup); DB auto-migrates the new column.
- Panel shows meaningful errors for 413/502/504/non-JSON (CDN) responses.
- Owner asked for a **simple** panel: no tabs; new site = HAR file + name + contract number (domain auto = base domain of the HAR's page; advanced settings folded in <details>); site view = requests list + "گرفتن داده" + results. Keep it simple.
- HAR import drops trackers/analytics/ads (`har.is_tracker`: host list, `sentry.`-style prefixes, path segments like collect/envelope) and only keeps requests that returned JSON (not 204/4xx beacons).

## Sections (categories) and stored data
- A site has **sections** (`categories`: e.g. doctors / clinics / pharmacies). HAR upload, requests, runs and data are per section; `POST /api/sites/{id}/run` (and the scheduler) runs every section in turn.
- After each response, `extract.find_records` takes the largest list of objects in the JSON as the records; each is flattened (`a.b` keys) and upserted into `items` by id/slug/code (else content hash). Export: `/api/categories/{id}/export?fmt=csv|json` (CSV has a BOM for Excel).
- `categories.max_pages`: if a request URL has `page=`-style param, pages are fetched until an empty or repeated page.
- Old databases: requests without a section are moved into a section named «عمومی» on startup.
- Paging is automatic: a request with a `page=`-style param is followed until an empty/repeated page (cap `max_pages`, default 200); a GET without one gets `page=2,3…` probed. The record list's JSON path from page 1 is reused on later pages. Pages already fetched in a run are skipped (canonical URL).
- Next.js `/_next/data/<buildId>/` URLs: on 404 the current buildId is read from the site's home page and the URL rewritten.
- Records carry `source` (endpoint name, e.g. `specialties/cardio`) used for groups in the panel and in the grouped JSON export.
- **Detail requests** (profile, phone behind login): at HAR import `har.detect_details` looks for requests whose URL path/query/JSON body contains a value (id, slug…) of another request's list records (from the HAR's own response bodies). They become `kind='detail'` with `url_template`/`body_template` (`{{field}}`), `detail_of` = family of that list. At run time lists run first, then each detail runs once per record of its list (`items.list_id`) that lacks it (`items.details_done` "[id][id]"), and its fields are merged into the record. List refreshes keep merged fields.
- Requests can be edited (URL on the site's domain, e.g. other specialty/city), duplicated (copies keep `family`) and switched list↔detail in the panel.
- **Chosen fields** per section (`categories.fields` = `[{key,label}]`, empty = all): "☑ انتخاب اطلاعات" lists every field found in stored records (`GET /api/categories/{id}/fields`: fill count + sample) to tick, rename and order. `_project` applies it to the panel table, JSON/CSV export and `/feed`. All fields stay stored, so the choice can change any time without re-running.
- **Point-and-click sections** (`mode='visual'`, default for new sections; `app/visual.py`): the owner names fields, opens the site in the recording browser, arms a field chip and clicks the element (`/api/recorder/pick` runs `PICK_JS`: list fields get the repeated card `item_selector` + a path inside the card, detail fields a page selector), plus the "detail link" (href on the list) and an optional button clicked before reading detail pages. Config in `categories.config`. Runs use sync Playwright with the login saved from the recorder (`DATA_DIR/sessions/cat_<id>.json`, storage_state written on "پایان و ذخیره"): list URL with `{page}` until an empty/repeated page, then each record's detail page once (`details_done` "[v]"). Output columns = picked field names in order. API/HAR mode remains as «پیشرفته».
- Visual addresses: `config.list_url` holds one address per line. With `{page}` = a list walked page by page (fields picked on it are "list" fields inside the repeated card); without = a single page (e.g. one profile) whose picked fields make one record (`details_done='[v]'`). Where a field lives is decided automatically at pick time (page matches a `{page}` address → list, otherwise the exact element). The owner asked that the address is NEVER changed automatically and that no confirm dialogs interrupt picking.
- `config.is_list` («این آدرس‌ها صفحه لیست هستند»): every address is a list even without `{page}`; page 1 is the address, later pages get `?page=n` (`visual.page_url`) until empty/repeated; each list page is scrolled until no new cards appear. The profile-link and "show button" chips are always available in list mode (they must be picked before any profile field exists).
- Lists are also detected automatically: a page-mode pick on one of the section's own addresses that lies inside ≥3 alike, visibly framed sibling cards with a link (`list_hint` from `PICK_JS`; "alike" = same first child and ±1 children, so a profile's different boxes do not count) switches the section to `is_list` and re-picks as a list field, without any dialog. Page fields remember where they were picked (`at`).
- Picks are stored as several ways to find the element, best first, tried in order at read time: list fields `rels` (exact path in the card, tags-only path, the element's own tag+classes anywhere in the card, …; ordered by how many cards each fills), page fields `selectors` (id / name-like attributes / own classes / distinctive ancestor + own classes when unique on the page, positional path last). Cards of a list are "the same kind" by tag + the classes most siblings share (`kind()`), so extra classes like "featured" or badges do not split them. The end of a list is detected by card count, not by filled rows.
- Visual settings are never lost: the panel keeps its own copy of the section's settings (`vcfg._cid`) and only loads from the server when switching sections; saving never fails over an address (missing `https://` is added, out-of-domain lines are kept with a warning and skipped at run time); save errors/warnings stay visible in `#v-err`.
- Live outline while a field is armed: mouse moves call `/api/recorder/hover` (`HOVER_JS`) and the panel draws the returned rectangle + text preview over the screenshot. Mode «✎ دقیق» = element under the mouse; «▢ کل باکس» = nearest visible box (border/shadow/own background). The click picks exactly what is outlined (hover's `up` is passed to pick).
- Text of picked elements (`__txt` in the page JS) drops buttons, icons and short "show more/less"-style controls; before reading a page the collector clicks "مشاهده بیشتر"-style expanders (`EXPAND_JS`, never links that navigate nor "show number" buttons). The same picks apply to every address in the box (pages must share the site's template).
- Visual picking: "⬆ بزرگ‌تر / ⬇ کوچک‌تر" re-pick the same point `up` levels higher (`PICK_JS` arg). Per-field text edits (`after`, `before`, `remove`, `replace`, `digits`) and section-wide `rules` ([[word, replacement]]) live in the config; values are stored raw and edits are applied on output (`visual.output_row` via `main._shaper` for table, export and feed), so changing them never needs a re-run. JS `vClean` mirrors `visual.clean` for previews.
- **Profiles flow (current panel for visual sections, `config.flow='profiles'`)**: the owner found picking on list cards unreliable, so the panel is split in two parts. Part 1 «صفحه لیست»: addresses (`list_url`, never changed automatically) + «🔗 لینک پروفایل» (one picked link → `link_sample`; `visual.LINKS_JS` finds every link on a page with the same URL shape: first path segment fixed, the rest `[^/]+`) + optional «⏭ دکمه صفحه بعد» (`next_selectors`; without it pages go `?page=n`). Part 2 «صفحه پروفایل»: sample profile (`profile_url`, default = `link_sample`), every field is picked there (`selectors` by identity: id/name-like attrs/classes, positional last) + optional «دکمه نمایش» (`detail_click`). `visual.run_profiles` walks list pages (scrolling until no new links, stops when a page adds no new links), then opens each profile once and reads the fields; rows are keyed by profile URL and get a «لینک پروفایل» column. The older list-card flow is still in `visual.run` for configs without `flow`.
- Profiles flow details: "show ..." buttons are a list `config.clicks` = [{selectors, text}] (older settings: `detail_click`/`click_selectors`/`click_text`, read by `visual._clicks`), clicked in order on each profile (e.g. "contact" then "confirm" in a modal): by exact text first (every visible innermost copy, e.g. one per office), else by selectors; a later button is waited for up to ~4s after the one before. Panel: chips «دکمه نمایش n» + «+ دکمه نمایش»; after the click `_read_settled` re-reads until all fields are filled and stable (≤8s) because numbers load late. `__txt` keeps buttons whose text holds ≥5 digits (the number often appears inside the button) and DETAIL_JS falls back to a `tel:` link. Runs report fields left empty («خالی ماند: تلفن در N پروفایل») as status partial. A run of this flow first cleans the section (`_dedupe_profiles`): rows of older flows (`source='لیست'` or a key not starting with `url:`) are deleted, and profiles are keyed by a normalized URL (`_norm_profile`: no fragment, no trailing slash, lower-case host without `www.`, query dropped unless the sample link has one); duplicates merge into the newest copy (older copies only fill its empty fields). The cleanup also runs on every settings save (`PUT /api/categories/{id}/visual`), not only on runs. Field picks remember `at` (path); on load, picks whose first path segment differs from `link_sample`'s are cleared (they were made on the list page). «🧪 آزمایش این صفحه» in the recorder bar reads the picked fields on the open page (`POST /api/recorder/read`).
- Field type «لوکیشن» (`attr='geo'`, `visual.GEO_FN` injected into PICK_JS/DETAIL_JS): "lat,lng" from the picked map element (data-lat/lng, href/src/style of it and its descendants/ancestors: map links, static map images, iframes), else page-wide (maps/waze/neshan/balad links, map iframes/images, `__NEXT_DATA__`, JSON-LD, inline scripts with lat/lng keys). lng,lat order is swapped using Iran ranges. No click on «مسیریابی» needed.
- Owner does not want to re-pick fields per specialty/city: (1) one section can hold many list addresses (one per line, the panel says so); profile rows get `source` = the list address they came from (`extract.source_of`), so the panel shows a group button per address; (2) «+ بخش جدید» asks (one confirm, at creation only) to copy the current visual section: `POST /api/sites/{id}/categories` with `copy_from` copies its config with an empty `list_url` plus its saved login file.
- **Recording browser** (`app/recorder.py`): "🎥 ضبط با مرورگر" opens headless Chromium on the server via Playwright (async API, one recording at a time, closes after 20 min idle) with `record_har_path`; the panel polls `/api/recorder/screen` (JPEG, `X-Page-Url` header) and forwards clicks/scroll/keys/text to `/api/recorder/input`. "پایان و ذخیره" closes the context (writes the HAR), imports it into the section via `_import_har` (same as upload, replaces requests) and deletes the HAR file. Optional dependency: `sudo bash deploy/install-recorder.sh` (pip playwright==1.56.0 + `playwright install --with-deps chromium` into /opt/har-scraper/browsers + systemd drop-in with PLAYWRIGHT_BROWSERS_PATH); also called from install.sh.
- Owner: panel already runs on https — do not bring up HTTPS again.
- **Feed for the owner's own website**: `GET /feed/sites` and `GET /feed/sections/{id}?since=&limit=&offset=&source=` with `?key=` or `X-API-Key` (env `HARSCRAPER_FEED_KEY`, else random key in `DATA_DIR/feed_key.txt`). Read-only; not behind Basic auth.

## Likely production issues to check first
1. **Arvan CDN caching** API responses → site list doesn't update after save. Fixed in code with `Cache-Control: no-store` middleware (needs deploy + purge CDN cache). Also consider a CDN rule to bypass cache for the whole host.
2. **CDN upload limit / timeout** on large HAR files → upload fails (413/504). Check Arvan CDN settings, or bypass CDN (DNS-only) for this host.
3. **Basic auth through CDN**: confirm `Authorization` header is forwarded; if login loops, bypass CDN.
4. **No HTTPS**: HAR contains session cookies; set up SSL (certbot with CDN proxy off, or Arvan CDN SSL).
5. Session expiry: 401/403 in run results means a fresh HAR is needed (UI already warns).

## Deploying a code change to the server
```bash
# from the project on the server
sudo cp -r app /opt/har-scraper/ && sudo systemctl restart har-scraper
```
Then purge the Arvan CDN cache for the domain.

## Ideas the owner may ask for next
- Field extraction rules (JSONPath) per request, e.g. only name/price
- Automatic pagination (page/offset params)
- Excel/CSV export, webhook/Telegram notification on failure

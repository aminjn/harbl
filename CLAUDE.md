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

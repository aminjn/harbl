"""Parse HAR files into replayable request templates."""
import base64
import json
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

from . import extract as ext

# Headers that must not be copied verbatim when replaying
DROP_HEADERS = {
    "content-length", "host", "connection", "accept-encoding", "transfer-encoding",
    "keep-alive", "upgrade", "te", "trailer", "proxy-authorization", "proxy-connection",
}
STATIC_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
              ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".map")

# Analytics / ads / error-reporting services: never site data, always skipped
TRACKER_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "googlesyndication.com",
    "google.com", "gstatic.com", "googleapis.com", "clarity.ms", "hotjar.com", "hotjar.io",
    "facebook.com", "facebook.net", "sentry.io", "yektanet.com", "mediaad.org", "metrix.ir",
    "zeblines.com", "zebline.com", "flowchat.tech", "backtory.com", "najva.com", "pushe.co",
    "goftino.com", "raychat.io", "crisp.chat", "tawk.to", "newrelic.com", "nr-data.net",
    "mixpanel.com", "segment.io", "segment.com", "amplitude.com", "webengage.com", "smartlook.com",
    "yandex.ru", "mc.yandex.ru", "bing.com", "linkedin.com", "twitter.com", "tiktok.com",
)
TRACKER_HOST_PREFIXES = ("ads.", "ad.", "sentry.", "analytics.", "stats.", "tracker.", "track.", "log.", "logs.", "metrics.", "telemetry.")
# Whole path segments only, so e.g. /api/track-order is not caught
TRACKER_PATH_SEGMENTS = {"envelope", "collect", "track", "analytics", "beacon", "pixel", "telemetry",
                         "sentry", "gtag", "gtm.js", "rum", "impressions", "impression",
                         "asyncspc.php", "ajs.php", "lg.php", "ck.php"}


def is_tracker(url: str) -> bool:
    u = urlparse(url)
    host, path = (u.hostname or "").lower(), u.path.lower()
    if any(host == t or host.endswith("." + t) for t in TRACKER_HOSTS):
        return True
    if host.startswith(TRACKER_HOST_PREFIXES):
        return True
    return any(seg in TRACKER_PATH_SEGMENTS for seg in path.split("/"))


class HarError(ValueError):
    pass


def load(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        raise HarError(f"فایل HAR معتبر نیست: {e}")
    if "log" not in data or "entries" not in data["log"]:
        raise HarError("ساختار HAR نامعتبر است (log.entries پیدا نشد)")
    return data


def host_allowed(url: str, allowed: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    for d in allowed:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _is_api(entry: dict) -> bool:
    """A request that returned data (JSON), not a fire-and-forget beacon."""
    resp = entry.get("response", {})
    if resp.get("status") in (0, 204) or (resp.get("status") or 0) >= 400:
        return False
    content = resp.get("content", {})
    mime = (content.get("mimeType") or "").lower()
    if "json" in mime or "graphql" in mime:
        return True
    rtype = (entry.get("_resourceType") or "").lower()
    text = (content.get("text") or "").lstrip()
    return rtype in ("xhr", "fetch") and text[:1] in ("{", "[")


def extract(data: dict, allowed_domains: list[str], api_only: bool = True) -> tuple[list[dict], dict]:
    """Return (templates, stats). Only hosts in allowed_domains are kept."""
    out, seen = [], set()
    stats = {"total": 0, "kept": 0, "outside_domain": 0, "static_or_non_api": 0, "duplicates": 0, "trackers": 0}
    outside_hosts: dict[str, int] = {}
    has_auth = False
    for e in data["log"]["entries"]:
        stats["total"] += 1
        req = e.get("request", {})
        url = req.get("url", "")
        method = req.get("method", "GET").upper()
        if method in ("HEAD", "OPTIONS") or not url.startswith(("http://", "https://")):
            stats["static_or_non_api"] += 1
            continue
        if is_tracker(url):
            stats["trackers"] += 1
            continue
        if not host_allowed(url, allowed_domains):
            stats["outside_domain"] += 1
            h = (urlparse(url).hostname or "").lower()
            outside_hosts[h] = outside_hosts.get(h, 0) + 1
            continue
        path = urlparse(url).path.lower()
        if path.endswith(STATIC_EXT) or (api_only and not _is_api(e)):
            stats["static_or_non_api"] += 1
            continue

        headers = {}
        for h in req.get("headers", []):
            name = h.get("name", "")
            if not name or name.startswith(":") or name.lower() in DROP_HEADERS:
                continue
            headers[name] = h.get("value", "")
        # Some HAR exporters put cookies only in request.cookies
        if not any(k.lower() == "cookie" for k in headers) and req.get("cookies"):
            headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in req["cookies"])

        if any(k.lower() in ("cookie", "authorization") for k in headers):
            has_auth = True

        body = None
        pd = req.get("postData")
        if pd:
            body = pd.get("text")
            # Firefox may store form posts only as params
            if body is None and pd.get("params"):
                body = urlencode([(x.get("name", ""), x.get("value", "")) for x in pd["params"]])

        key = (method, url, body)
        if key in seen:
            stats["duplicates"] += 1
            continue
        seen.add(key)

        status = e.get("response", {}).get("status")
        content = e.get("response", {}).get("content", {})
        text = content.get("text") or ""
        if content.get("encoding") == "base64":
            try:
                text = base64.b64decode(text).decode("utf-8", "replace")
            except ValueError:
                text = ""
        out.append({
            "response_text": text,
            "label": urlparse(url).path or "/",
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
            "har_status": status,
        })
    stats["kept"] = len(out)
    # Hosts that were skipped, most frequent first, so the user can add them to allowed domains
    stats["outside_hosts"] = sorted(outside_hosts, key=outside_hosts.get, reverse=True)[:10]
    # Chrome's default "sanitized" HAR export strips Cookie/Authorization; replay will then get 401/403
    stats["no_auth"] = bool(out) and not has_auth
    return out, stats


PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


def _record_values(templates: list[dict]) -> dict[str, tuple[int, str]]:
    """value -> (index of the list request it came from, field name), from the HAR's own list responses."""
    values: dict[str, tuple[int, str]] = {}
    for i, t in enumerate(templates):
        try:
            data = json.loads(t["response_text"])
        except ValueError:
            continue
        records, _ = ext.find_records(data)
        if len(records) < 2:
            continue
        for rec in records:
            for k, v in ext.flatten(rec).items():
                if isinstance(v, (str, int)) and not isinstance(v, bool) and len(str(v)) >= 3:
                    values.setdefault(str(v), (i, k))
    return values


def detect_details(templates: list[dict]) -> None:
    """Mark requests that fetch ONE record of another request's list (a doctor's profile, a phone number...).

    Their URL/body contains a value of a listed record (id, slug, ...). They get a template such as
    /dr/{{id}}/{{slug}} and are repeated for every collected record at run time."""
    values = _record_values(templates)
    for i, t in enumerate(templates):
        t["kind"], t["url_template"], t["body_template"], t["detail_of_index"] = "list", None, None, None
        fields: dict[str, str] = {}

        def sub(v: str) -> str | None:
            stem, ext_ = (v[:-len(e)], e) if (e := next((x for x in (".json", ".html", ".htm") if v.endswith(x)), "")) else (v, "")
            hit = values.get(stem)
            if hit and hit[0] != i:
                fields[stem] = hit[1]
                t["detail_of_index"] = hit[0]
                return "{{" + hit[1] + "}}" + ext_
            return None

        u = urlsplit(t["url"])
        path = "/".join(sub(unquote(seg)) or seg for seg in u.path.split("/"))
        query = "&".join(f"{quote(k, safe='')}={sub(v) or quote(v, safe='')}"
                         for k, v in parse_qsl(u.query, keep_blank_values=True))
        body = t["body"]
        if body:
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    obj = {k: (sub(str(v)) or v) if isinstance(v, (str, int)) and not isinstance(v, bool) else v
                           for k, v in obj.items()}
                    body = json.dumps(obj, ensure_ascii=False)
            except ValueError:
                pass
        if not fields:
            continue
        try:
            own, _ = ext.find_records(json.loads(t["response_text"]))
        except ValueError:
            own = []
        # A list filtered by a slug (e.g. doctors of one specialty) is still a list; an id in the URL means one record
        if len(own) >= 2 and not any(v.isdigit() for v in fields):
            continue
        t["kind"] = "detail"
        t["url_template"] = urlunsplit(u._replace(path=path, query=query))
        t["body_template"] = body if body != t["body"] else None
        t["label"] = "جزئیات: " + (u.path or "/")

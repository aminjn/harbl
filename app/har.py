"""Parse HAR files into replayable request templates."""
import json
from urllib.parse import urlencode, urlparse

# Headers that must not be copied verbatim when replaying
DROP_HEADERS = {
    "content-length", "host", "connection", "accept-encoding", "transfer-encoding",
    "keep-alive", "upgrade", "te", "trailer", "proxy-authorization", "proxy-connection",
}
STATIC_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
              ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".map")


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
    rtype = (entry.get("_resourceType") or "").lower()
    if rtype in ("xhr", "fetch"):
        return True
    mime = (entry.get("response", {}).get("content", {}).get("mimeType") or "").lower()
    if "json" in mime or "graphql" in mime:
        return True
    return False


def extract(data: dict, allowed_domains: list[str], api_only: bool = True) -> tuple[list[dict], dict]:
    """Return (templates, stats). Only hosts in allowed_domains are kept."""
    out, seen = [], set()
    stats = {"total": 0, "kept": 0, "outside_domain": 0, "static_or_non_api": 0, "duplicates": 0}
    outside_hosts: dict[str, int] = {}
    has_auth = False
    for e in data["log"]["entries"]:
        stats["total"] += 1
        req = e.get("request", {})
        url = req.get("url", "")
        method = req.get("method", "GET").upper()
        if not url.startswith(("http://", "https://")):
            stats["static_or_non_api"] += 1
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
        out.append({
            "label": f"{method} {urlparse(url).path or '/'}",
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

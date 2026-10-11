"""Turn API responses into flat records (one row per doctor / clinic / product ...)."""
import hashlib
import json
import re
from urllib.parse import unquote, urlparse

ID_KEYS = ("id", "_id", "uuid", "slug", "code", "Id", "ID")
MAX_CELL = 500


def find_records(data, path: tuple | None = None) -> tuple[list[dict], tuple | None]:
    """(records, path). The largest list of objects in the response is the record list.

    Pass the path found on page 1 for the next pages, so an empty last page is not mistaken
    for some other, smaller list in the same response."""
    if path is not None:
        node = data
        for k in path:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return [], path
        return ([x for x in node if isinstance(x, dict)] if isinstance(node, list) else []), path
    best, best_path, saw_list = [], None, False
    stack = [(data, ())]
    while stack:
        node, at = stack.pop()
        if isinstance(node, dict):
            stack.extend((v, at + (k,)) for k, v in node.items())
        elif isinstance(node, list):
            saw_list = True
            dicts = [x for x in node if isinstance(x, dict)]
            if dicts and len(dicts) >= len(node) / 2 and len(dicts) > len(best):
                best, best_path = dicts, at
            # lists nested inside list items are not followed: paths only go through object keys
    if not best and not saw_list and isinstance(data, dict) and data:
        # A single-object response (e.g. one doctor's profile page)...
        inner = data.get("pageProps", data.get("data", data))
        # ...but not tiny status replies like {"success": true}
        if isinstance(inner, dict) and len(flatten(inner)) >= 3:
            return [inner], None
    return best, best_path


def flatten(d: dict, prefix: str = "", depth: int = 0) -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and depth < 2:
            out.update(flatten(v, key + ".", depth + 1))
        elif isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v):
            out[key] = "، ".join(str(x) for x in v)
        elif isinstance(v, (dict, list)):
            out[key] = json.dumps(v, ensure_ascii=False)[:MAX_CELL]
        else:
            out[key] = v
    return out


def record_key(rec: dict) -> str:
    for k in ID_KEYS:
        if rec.get(k) not in (None, ""):
            return f"{k}:{rec[k]}"
    return "h:" + hashlib.sha1(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_of(url: str) -> str:
    """Readable endpoint name used to group records, e.g. "specialties/cardiology" or "api/v3/geolocations"."""
    path = urlparse(url).path
    path = re.sub(r"^/_next/data/[^/]+", "", path)  # Next.js build id changes on every deploy
    path = re.sub(r"(\.json|/index)$", "", path)
    segs = []
    for seg in path.strip("/").split("/"):
        seg = unquote(seg)
        segs.append("*" if seg.isdigit() or len(seg) > 40 else seg)
    return "/".join(segs) or "/"

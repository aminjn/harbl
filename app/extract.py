"""Turn API responses into flat records (one row per doctor / clinic / product ...)."""
import hashlib
import json

ID_KEYS = ("id", "_id", "uuid", "slug", "code", "Id", "ID")
MAX_CELL = 500


def find_records(data) -> list[dict]:
    """The largest list of objects anywhere in the response is taken as the record list."""
    best: list[dict] = []
    saw_list = False
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            saw_list = True
            dicts = [x for x in node if isinstance(x, dict)]
            if dicts and len(dicts) >= len(node) / 2 and len(dicts) > len(best):
                best = dicts
            stack.extend(node)
    if not best and not saw_list and isinstance(data, dict) and data:
        # A single-object response (e.g. one doctor's profile page); an empty list means "no more pages"
        inner = data.get("pageProps", data.get("data", data))
        # ...but not tiny status replies like {"success": true}
        if isinstance(inner, dict) and len(flatten(inner)) >= 3:
            best = [inner]
    return best


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

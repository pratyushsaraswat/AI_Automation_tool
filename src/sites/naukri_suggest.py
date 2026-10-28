"""Naukri's own keyword suggester (the dropdown under its search box) — no login/cookies needed.
'golang' -> ['Golang', 'Golang Developer', ...]  so searches use the exact terms Naukri indexes jobs by."""
import json
import re

import httpx

URL = "https://taxonomy-suggest.naukri.com/suggest/dsCommonSuggester"
NOISE = re.compile(r"\b(loan|gold|sales|tele|bpo|insurance|recovery|collection|intern|trainee|fresher)\b", re.I)


def suggest(query: str, limit: int = 8) -> list[str]:
    try:
        r = httpx.get(URL, timeout=10, headers={"referer": "https://www.naukri.com/",
                                                "user-agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0.0.0"},
                      params={"limit": limit, "appId": 105, "resultField": "id,name", "subCategory": "dsJSsuggestor",
                              "query": query, "category": "top", "sourceId": 4001, "astext": query, "callback": "cb"})
        body = re.search(r"cb\((.*)\)\s*;?\s*$", r.text, re.S)
        items = json.loads(body.group(1))["resultList"]["top"] if body else []
    except (httpx.HTTPError, ValueError, KeyError):
        return []
    first = query.lower().split()[0] if query.split() else ""
    out = []
    for it in items:
        name = (it.get("name") or [""])[0]
        words = re.findall(r"[a-z0-9+#.]+", name.lower())
        if name and not NOISE.search(name) and any(w.startswith(first) for w in words) and name not in out:
            out.append(name)
    return out

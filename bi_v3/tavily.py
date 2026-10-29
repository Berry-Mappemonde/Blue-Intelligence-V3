"""Tavily Search / Extract. Key from env TAVILY_API_KEY only."""
from __future__ import annotations
import json, os, urllib.request

CREDITS = {"search": 0, "extract": 0}

def _post(path, body):
    body["api_key"] = os.environ["TAVILY_API_KEY"]
    req = urllib.request.Request("https://api.tavily.com/" + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)

def search(query: str, max_results: int = 6, include_domains=None) -> list[dict]:
    b = {"query": query, "max_results": max_results, "search_depth": "basic"}
    if include_domains:
        b["include_domains"] = include_domains
    CREDITS["search"] += 1
    return _post("search", b).get("results", [])

def extract(urls: list[str]) -> list[dict]:
    if not urls:
        return []
    CREDITS["extract"] += (len(urls) + 4) // 5
    return _post("extract", {"urls": urls[:20]}).get("results", [])

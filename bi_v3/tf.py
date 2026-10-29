"""Nemotron via Nebius Token Factory (OpenAI-compatible). Key from env NEBIUS_API_KEY only."""
from __future__ import annotations
import json, os, re, time, urllib.request

BASE = os.environ.get("BI_TF_BASE", "https://api.tokenfactory.nebius.com/v1")
MODELS = {
    "triage": os.environ.get("BI_TF_TRIAGE", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"),
    "extract": os.environ.get("BI_TF_EXTRACT", "nvidia/nemotron-3-super-120b-a12b"),
    "judge": os.environ.get("BI_TF_JUDGE", "nvidia/Nemotron-3-Ultra-550b-a55b"),
}
# USD per 1M tokens (in, out) — public Token Factory catalogue read 28/10/2026 (BI-V3-PLAN §4)
PRICES = {MODELS["triage"]: (0.06, 0.24), MODELS["extract"]: (0.30, 0.90), MODELS["judge"]: (1.0, 3.0)}

class Ledger:
    def __init__(self):
        self.calls = []
    def add(self, model, usage, ms):
        pi, po = PRICES.get(model, (1.0, 3.0))
        ti, to = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        self.calls.append({"model": model, "in": ti, "out": to, "ms": ms, "usd": ti / 1e6 * pi + to / 1e6 * po})
    def summary(self):
        s = {"calls": len(self.calls), "tokens_in": sum(c["in"] for c in self.calls),
             "tokens_out": sum(c["out"] for c in self.calls), "usd_estimated": round(sum(c["usd"] for c in self.calls), 5), "by_model": {}}
        for c in self.calls:
            m = s["by_model"].setdefault(c["model"], {"calls": 0, "usd": 0.0}); m["calls"] += 1; m["usd"] = round(m["usd"] + c["usd"], 5)
        return s

LEDGER = Ledger()

def chat(role: str, system: str, user: str, max_tokens: int = 1200, temperature: float = 0.0) -> str:
    key = os.environ.get("NEBIUS_API_KEY")
    if not key:
        raise RuntimeError("NEBIUS_API_KEY not set")
    model = MODELS[role]
    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.load(r)
    LEDGER.add(model, res.get("usage") or {}, int((time.time() - t) * 1000))
    from . import tracing
    tracing.log_llm(role, model, system, user, res)
    return res["choices"][0]["message"].get("content") or ""

def parse_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = m.group(1) if m else text
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = raw.find(opener), raw.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(raw[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None

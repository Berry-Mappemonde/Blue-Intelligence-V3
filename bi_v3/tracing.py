"""Optional tracing. LangSmith (if LANGSMITH_API_KEY + LANGSMITH_TRACING=true) else local JSONL
in OpenTelemetry-like span shape so traces can be replayed into LangSmith/Langfuse later."""
from __future__ import annotations
import json, os, time, uuid
_PATH = [None]

def set_sink(path: str):
    _PATH[0] = path

def log_llm(role, model, system, user, res):
    span = {"span_id": uuid.uuid4().hex[:16], "ts": time.time(), "name": f"llm.{role}",
            "attributes": {"gen_ai.system": "nebius-token-factory", "gen_ai.request.model": model,
                           "gen_ai.usage.input_tokens": (res.get("usage") or {}).get("prompt_tokens"),
                           "gen_ai.usage.output_tokens": (res.get("usage") or {}).get("completion_tokens")},
            "input": user[:2000], "output": ((res.get("choices") or [{}])[0].get("message") or {}).get("content", "")[:2000]}
    if _PATH[0]:
        with open(_PATH[0], "a") as f:
            f.write(json.dumps(span, ensure_ascii=False) + "\n")
    if os.environ.get("LANGSMITH_API_KEY") and os.environ.get("LANGSMITH_TRACING") == "true":
        try:
            from langsmith import Client
            Client().create_run(name=span["name"], run_type="llm", inputs={"system": system, "user": user},
                                outputs={"output": span["output"]}, extra={"metadata": span["attributes"]})
        except Exception:
            pass

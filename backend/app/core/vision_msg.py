"""Multimodal messages (text + JPEG) for NIM / OpenRouter / Claude."""
from __future__ import annotations

import base64


def _b64(img: bytes) -> str:
    return base64.b64encode(img).decode("ascii")


def openai_user_content(text: str, images: list[bytes] | None = None):
    """OpenAI-compatible content: str if no image, else a list of parts."""
    if not images:
        return text
    parts: list[dict] = [{"type": "text", "text": text}]
    for img in images:
        if not img:
            continue
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{_b64(img)}"},
        })
    return parts if len(parts) > 1 else text


def claude_user_content(text: str, images: list[bytes] | None = None) -> list[dict]:
    parts: list[dict] = [{"type": "text", "text": text}]
    for img in images or []:
        if not img:
            continue
        parts.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _b64(img),
            },
        })
    return parts

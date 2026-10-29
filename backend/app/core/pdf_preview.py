"""Subprocess: first pages of a PDF as JPEG (Review vision).

The parent process never imports ``fitz``.
Launched via ``python -m app.core.pdf_preview IN.pdf OUTDIR [max_pages]``.
"""
from __future__ import annotations

import sys
from pathlib import Path


def render_pdf_pages(inp: str, out_dir: str, max_pages: int = 2,
                     max_width: int = 1280) -> list[str]:
    import fitz

    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with fitz.open(inp) as pdf:
        for i, page in enumerate(list(pdf[:max_pages])):
            rect = page.rect
            scale = min(2.0, max_width / max(rect.width, 1))
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            path = dest / f"{i:02d}.jpg"
            pix.save(str(path), jpg_quality=70)
            written.append(str(path))
    return written


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print("usage: python -m app.core.pdf_preview IN.pdf OUTDIR [max_pages]",
              file=sys.stderr)
        return 2
    inp, out_dir = argv[0], argv[1]
    max_pages = int(argv[2]) if len(argv) > 2 else 2
    try:
        render_pdf_pages(inp, out_dir, max_pages)
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

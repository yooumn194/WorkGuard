"""Markdown / txt parser: one block per non-empty line, location = line_<n>."""
from __future__ import annotations

from pathlib import Path


def parse(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_text(text)


def parse_text(text: str) -> dict:
    blocks = []
    section = ""
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip()
        blocks.append(
            {"location": f"line_{idx}", "text": stripped, "meta": {"section": section}}
        )
    return {"artifact_type": "markdown", "blocks": blocks}

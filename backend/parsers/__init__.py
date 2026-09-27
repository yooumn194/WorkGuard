"""Document parsers: markdown / txt / docx / xlsx -> structured blocks with locations.

A ParsedDocument is a plain dict:
    {"artifact_type": "markdown", "blocks": [{"location": "...", "text": "...", "meta": {...}}]}

Locations are stable identifiers used for source citation AND for write-back
targeting ("line_12" | "para_5" | "tbl_0_row_2" | "Sheet1!A3:B3").
"""
from __future__ import annotations

from pathlib import Path

from backend.parsers import docx_parser, markdown_parser, xlsx_parser

SUFFIX_MAP = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "txt",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
}


def detect_kind(filename: str) -> str | None:
    return SUFFIX_MAP.get(Path(filename).suffix.lower())


def parse_file(path: str | Path, kind: str | None = None) -> dict:
    p = Path(path)
    kind = kind or detect_kind(p.name)
    if kind in ("markdown", "txt"):
        return markdown_parser.parse(p)
    if kind == "docx":
        return docx_parser.parse(p)
    if kind == "xlsx":
        return xlsx_parser.parse(p)
    raise ValueError(f"Unsupported artifact type: {kind} ({p.name})")

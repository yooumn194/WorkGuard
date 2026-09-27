"""XLSX parser (openpyxl, read-only): one block per non-empty row.

For two-column key/value rows the block text is "Key: Value" so the fact
extractor can treat it like a sentence. A "Project/项目" row sets a context
entity that is attached to later rows in the same sheet (meta.context_entity).
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import openpyxl

_ENTITY_CONTEXT_KEYS = ("project", "项目", "product", "产品")


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    return str(value).strip()


def _is_numeric_like(text: str) -> bool:
    import re

    return bool(re.fullmatch(r"[\d./\-年月日\s]+", text or ""))


def parse(path: str | Path) -> dict:
    workbook = openpyxl.load_workbook(str(path), data_only=False, read_only=False)
    blocks: list[dict] = []

    for sheet in workbook.worksheets:
        context_entity = ""
        for row in sheet.iter_rows():
            values = [_cell_text(c.value) for c in row]
            if not any(values):
                continue
            first_row = row[0].row
            cells = {
                f"{openpyxl.utils.get_column_letter(c.column)}{first_row}": _cell_text(c.value)
                for c in row
                if _cell_text(c.value)
            }
            non_empty = [v for v in values if v]
            if len(non_empty) == 2:
                text = f"{non_empty[0]}: {non_empty[1]}"
                key = non_empty[0].lower().strip()
                if (
                    any(k in key for k in _ENTITY_CONTEXT_KEYS)
                    and not _is_numeric_like(non_empty[1])
                ):
                    context_entity = non_empty[1]
            else:
                text = " | ".join(non_empty)
            blocks.append(
                {
                    "location": f"{sheet.title}!row_{first_row}",
                    "text": text,
                    "meta": {"cells": cells, "context_entity": context_entity},
                }
            )

    return {"artifact_type": "xlsx", "blocks": blocks}

"""Document write-back tools.

Format-safety policy (user decision, README documents it honestly):

  markdown / txt  -> direct in-place replace (line-level, surface-form aware)
  docx            -> controlled_write ONLY replaces text fully contained in a
                     single run (zero style damage); anything else is reported
                     as needs_manual and degraded to a suggestion patch.
  docx / xlsx     -> default mode never touches the file: it emits a
                     "修改建议片段" patch file (<name>.workguard-patch.md) with
                     exact locations + highlighted before/after.

Every modification snapshots a new ArtifactVersion first (proposal #34), and
every tool returns the unified envelope {"success", "result", "error"}.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import sqlalchemy.orm as orm
from sqlalchemy import func, select

from backend.models import Artifact, ArtifactVersion, uid
from backend.parsers import parse_file
from backend.utils.dates import find_dates, render_like


def _envelope(result=None, error: str | None = None) -> dict:
    if error:
        return {"success": False, "result": None, "error": error}
    return {"success": True, "result": result or {}, "error": None}


def read_text(artifact: Artifact) -> str:
    return Path(artifact.source_path).read_text(encoding="utf-8", errors="replace")


def file_sha256(artifact: Artifact) -> str:
    """Hash the exact on-disk bytes for optimistic write/rollback guards."""
    return hashlib.sha256(Path(artifact.source_path).read_bytes()).hexdigest()


def commit_version(session: orm.Session, artifact: Artifact) -> ArtifactVersion:
    """Store the CURRENT on-disk content as a new version (proposal #34:
    v(n) -> Change -> v(n+1)). Called right AFTER a successful write, so the
    previous version row is always a valid rollback point."""
    path = Path(artifact.source_path)
    if artifact.type in ("docx", "xlsx"):
        raw = base64.b64encode(path.read_bytes()).decode("ascii")
    else:
        # read_bytes avoids Python universal-newline conversion so rollback
        # preserves CRLF, BOM and final-newline bytes for UTF-8 text files.
        raw = path.read_bytes().decode("utf-8", errors="replace")
    parsed = parse_file(path, artifact.type)
    highest = session.scalar(
        select(func.max(ArtifactVersion.version)).where(ArtifactVersion.artifact_id == artifact.id)
    ) or 0
    version_number = max(highest, artifact.current_version) + 1
    version = ArtifactVersion(
        id=uid("ver"),
        artifact_id=artifact.id,
        version=version_number,
        content_hash=hashlib.sha256(raw.encode()).hexdigest()[:16],
        raw_content=raw,
        parsed_content=parsed,
    )
    artifact.current_version = version_number
    session.add(version)
    session.flush()
    return version


def restore_version(session: orm.Session, artifact: Artifact, version: ArtifactVersion) -> dict:
    """Rollback content while appending a new immutable history version.

    Rewinding ``current_version`` would let a later write reuse an existing
    version number. Restores therefore create v(n+1) containing the old bytes.
    """
    if version is None:
        return _envelope(error="restore failed: previous version is missing")
    path = Path(artifact.source_path)
    try:
        if artifact.type in ("docx", "xlsx"):
            path.write_bytes(base64.b64decode(version.raw_content))
        else:
            path.write_bytes(version.raw_content.encode("utf-8"))
    except OSError as exc:
        return _envelope(error=f"restore failed: {exc}")
    restored = commit_version(session, artifact)
    return _envelope({
        "restored_from_version": version.version,
        "current_version": restored.version,
        "version_id": restored.id,
    })


# ------------------------------------------------------------------ markdown
def write_markdown(artifact: Artifact, edits: list[dict]) -> dict:
    """edits: [{"location": "line_N", "old_iso", "new_iso"}]"""
    path = Path(artifact.source_path)
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _envelope(error="non-UTF-8 text write refused; file untouched")
    newline = "\r\n" if "\r\n" in text else "\n"
    had_final_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    index = {f"line_{i+1}": i for i in range(len(lines))}
    applied, failures = [], []
    for edit in edits:
        target = index.get(edit.get("location", ""))
        scopes = [target] if target is not None and target >= 0 else range(len(lines))
        matches: list[tuple[int, int, int, str]] = []
        for i in scopes:
            matches.extend((i, *match) for match in _find_surface_matches(lines[i], edit["old_iso"]))
        if len(matches) != 1:
            failures.append({"location": edit.get("location", ""),
                             "reason": "old value must occur exactly once in target scope",
                             "matches": len(matches)})
            continue
        i, start, end, hit = matches[0]
        new_surface = render_like(hit, edit["new_iso"])
        if new_surface == hit and edit["new_iso"] != edit["old_iso"]:
            failures.append({"location": edit.get("location", ""),
                             "reason": "surface form cannot represent this date change"})
            continue
        lines[i] = lines[i][:start] + new_surface + lines[i][end:]
        applied.append({"location": f"line_{i+1}", "old": hit, "new": new_surface})
    if failures:
        return _envelope(error=f"ambiguous or missing target; file untouched: {failures}")
    output = newline.join(lines) + (newline if had_final_newline and lines else "")
    path.write_bytes((b"\xef\xbb\xbf" if has_bom else b"") + output.encode("utf-8"))
    return _envelope({"applied": applied, "failures": failures})


def _find_surface(original: str, iso: str) -> str | None:
    """Find how *iso* is actually spelled inside *original*, tolerating
    arbitrary whitespace ("2026 年 9 月 20 日"). Longest variant first."""
    matches = _find_surface_matches(original, iso)
    return matches[0][2] if matches else None


def _find_surface_matches(original: str, iso: str) -> list[tuple[int, int, str]]:
    """Return semantic date matches, never a substring from another year.

    A plain search for the short variant ``09-20`` would also match inside
    ``2025-09-20``. Parsing complete date expressions first prevents a 2026
    replacement from silently changing an explicitly stated 2025 date.
    """
    year = int(iso.split("-", 1)[0])
    return [
        (match["start"], match["end"], match["surface"])
        for match in find_dates(original, default_year=year)
        if match["iso"] == iso
    ]


def contains_date_value(text: str, iso: str) -> bool:
    return bool(_find_surface_matches(text, iso))


# ---------------------------------------------------------------------- docx
def write_docx(artifact: Artifact, edits: list[dict]) -> dict:
    """Controlled write: replace only when the surface sits inside ONE run.

    Merging runs would silently destroy intra-paragraph formatting, so any
    spanned match is reported (never applied) and the caller can fall back to
    a suggestion patch. Style/numbering/layout stay untouched otherwise.
    """
    import docx as docx_lib

    path = Path(artifact.source_path)
    document = docx_lib.Document(str(path))
    applied, failures = [], []

    def patch_paragraph(paragraph, location: str, old_iso: str, new_iso: str) -> bool:
        paragraph_matches = _find_surface_matches(paragraph.text, old_iso)
        if len(paragraph_matches) > 1:
            failures.append({"location": location,
                             "reason": "multiple old-date occurrences; refused as ambiguous"})
            return True
        for run in paragraph.runs:
            matches = _find_surface_matches(run.text, old_iso)
            if len(matches) == 1:
                start, end, hit = matches[0]
                new_surface = render_like(hit, new_iso)
                if new_surface == hit and new_iso != old_iso:
                    failures.append({"location": location,
                                     "reason": "surface form cannot represent this date change"})
                    return True
                run.text = run.text[:start] + new_surface + run.text[end:]
                applied.append({"location": location, "old": hit, "new": new_surface})
                return True
        # spans multiple runs -> refuse (format-safety), caller falls back to patch
        if _find_surface(paragraph.text, old_iso):
            failures.append({"location": location,
                             "reason": "old value spans multiple runs; "
                                       "refused in controlled mode (format safety)"})
            return True  # handled, but as a refusal
        return False

    for edit in edits:
        location = edit.get("location", "")
        handled = False
        for i, para in enumerate(document.paragraphs):
            if location in ("", f"para_{i}"):
                if patch_paragraph(para, f"para_{i}", edit["old_iso"], edit["new_iso"]):
                    handled = True
                    break
        if not handled:
            for t, table in enumerate(document.tables):
                for r, row in enumerate(table.rows):
                    if location != f"tbl_{t}_row_{r}":
                        continue
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            if patch_paragraph(para, location, edit["old_iso"], edit["new_iso"]):
                                handled = True
                                break
    if not applied and not failures:
        return _envelope(error="no matching paragraph found; file untouched")
    if failures:
        return _envelope(error=f"controlled write refused; file untouched: {failures}")
    document.save(str(path))
    return _envelope({"applied": applied, "refused": failures})


# ------------------------------------------------------------------ xlsx
def write_xlsx(artifact: Artifact, edits: list[dict]) -> dict:
    """Controlled write: replace plain-string / datetime cells only.

    Formula cells (values starting with '=') are NEVER overwritten — that is
    how spreadsheets get silently destroyed. Number formats are preserved.
    """
    import datetime as dt

    import openpyxl

    path = Path(artifact.source_path)
    workbook = openpyxl.load_workbook(str(path), data_only=False)
    applied, skipped, assignments = [], [], []
    for edit in edits:
        location = edit.get("location", "")  # "Sheet1!row_3"
        sheet_name, _, row_part = location.partition("!")
        try:
            row_index = int(row_part.replace("row_", ""))
        except ValueError:
            skipped.append({"location": location, "reason": "unparsable location"})
            continue
        if sheet_name not in workbook.sheetnames:
            skipped.append({"location": location, "reason": "worksheet not found", "matches": 0})
            continue
        sheet = workbook[sheet_name]
        if row_index < 1 or row_index > sheet.max_row:
            skipped.append({"location": location, "reason": "row out of range", "matches": 0})
            continue
        matches: list[tuple[Any, Any, tuple[int, int, str] | None]] = []
        for cell in sheet[row_index]:
            value = cell.value
            if isinstance(value, str) and not value.startswith("="):
                surface_matches = _find_surface_matches(value, edit["old_iso"])
                matches.extend((cell, value, match) for match in surface_matches)
            elif isinstance(value, (dt.datetime, dt.date)):
                if value.isoformat()[:10] == edit["old_iso"]:
                    matches.append((cell, value, None))
        if len(matches) != 1:
            skipped.append({"location": location,
                            "reason": "expected exactly one matching non-formula cell",
                            "matches": len(matches)})
            continue
        cell, value, match = matches[0]
        if match is None:
            y, m, d = (int(p) for p in edit["new_iso"].split("-"))
            new_value = dt.datetime(y, m, d) if isinstance(value, dt.datetime) else dt.date(y, m, d)
            assignments.append((cell, new_value))
            applied.append({"location": f"{sheet.title}!{cell.coordinate}",
                            "old": edit["old_iso"], "new": edit["new_iso"]})
        else:
            start, end, hit = match
            new_surface = render_like(hit, edit["new_iso"])
            if new_surface == hit and edit["new_iso"] != edit["old_iso"]:
                skipped.append({"location": location,
                                "reason": "surface form cannot represent this date change"})
                continue
            assignments.append((cell, value[:start] + new_surface + value[end:]))
            applied.append({"location": f"{sheet.title}!{cell.coordinate}",
                            "old": hit, "new": new_surface})
    if skipped:
        return _envelope(error=f"ambiguous or missing cell; workbook untouched: {skipped}")
    for cell, new_value in assignments:
        cell.value = new_value  # existing number formats and styles are preserved
    # openpyxl does not calculate formulas. Preserve formulas and force the
    # spreadsheet application to recalculate dependent values when reopened.
    workbook.calculation.calcMode = "auto"
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    workbook.save(str(path))
    return _envelope({"applied": applied, "skipped": skipped,
                      "formula_recalculation": "on_open"})


# ------------------------------------------------------------- suggestion patch
def generate_patch(
    artifact: Artifact,
    edits: list[dict],
    blocks: list[dict],
    reason: str = "",
    patch_id: str = "",
) -> dict:
    """Write a suggestion file next to the artifact; the original is untouched.

    This is the MVP write-back answer for DOCX/XLSX: "生成修改建议片段 + 高亮
    定位" instead of silently overwriting user files.
    """
    import re

    source = Path(artifact.source_path)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", patch_id).strip("_")
    if not safe_id:
        digest_input = repr([(e.get("location"), e.get("old_iso"), e.get("new_iso"))
                             for e in edits]).encode()
        safe_id = hashlib.sha256(digest_input).hexdigest()[:12]
    patch_path = source.with_name(f"{source.name}.workguard-{safe_id}.patch.md")
    block_by_location = {b["location"]: b["text"] for b in blocks}
    lines = [
        f"# WorkGuard 修改建议 — {artifact.name}",
        "",
        f"> 原文件未做任何修改（{artifact.type.upper()} 格式默认仅生成建议，避免样式/公式被破坏）。",
        f"> {reason}",
        "",
    ]
    ambiguous = 0
    for i, edit in enumerate(edits, start=1):
        location = edit.get("location", "")
        original = block_by_location.get(location, "")
        hit = _find_surface(original, edit["old_iso"])
        new_surface = render_like(hit, edit["new_iso"]) if hit else edit["new_iso"]
        matches = _find_surface_matches(original, edit["old_iso"])
        suggested = (
            original[:matches[0][0]] + new_surface + original[matches[0][1]:]
            if len(matches) == 1 else original
        )
        if len(matches) != 1 or (matches and new_surface == matches[0][2]
                                 and edit["new_iso"] != edit["old_iso"]):
            ambiguous += 1
        safety_note = (
            "" if len(matches) == 1 and new_surface != matches[0][2]
            else "（目标缺失、不唯一或现有写法无法表达该变化，请人工定位）"
        )
        lines += [
            f"## 建议 {i} @ `{location}`",
            "",
            f"- 原文：`{original}`",
            f"- 替换：~~{hit or edit['old_iso']}~~ → **{new_surface}**",
            f"- 修改后：`{suggested}`",
            f"- 定位检查：{'唯一命中' if len(matches) == 1 else safety_note}",
            "",
        ]
    patch_path.write_text("\n".join(lines), encoding="utf-8")
    return _envelope({
        "patch_path": str(patch_path),
        "edits": len(edits),
        "ambiguous_edits": ambiguous,
        "safe_to_apply": ambiguous == 0,
    })


# ------------------------------------------------------------------ post verify
def post_verify(artifact: Artifact, old_iso: str, new_iso: str, locations: list[str]) -> dict:
    """Re-read the artifact and confirm the new value landed and the old is gone."""
    if artifact.type == "xlsx":

        blocks = parse_file(artifact.source_path, "xlsx")["blocks"]
        old_hits, new_hits = [], []
        targets = set(locations) if locations and locations != [""] else None
        for block in blocks:
            if targets is not None and block["location"] not in targets:
                continue
            text = block["text"]
            if contains_date_value(text, old_iso):
                old_hits.append(block["location"])
            if _find_surface(text, new_iso):
                new_hits.append(block["location"])
        ok = bool(new_hits) and not old_hits
        return {"success": ok, "expected": new_iso, "actual": "found" if ok else "not found",
                "old_residuals": old_hits, "locations": new_hits}

    blocks = parse_file(artifact.source_path)["blocks"]
    targets = set(locations) if locations and locations != [""] else {b["location"] for b in blocks}
    evidence_new = ""
    ok_old_gone = True
    for block in blocks:
        in_scope = block["location"] in targets
        if in_scope and _find_surface(block["text"], new_iso):
            evidence_new = evidence_new or block["text"]
        if in_scope and _find_surface(block["text"], old_iso):
            ok_old_gone = False
    ok = bool(evidence_new) and ok_old_gone
    return {"success": ok, "expected": new_iso, "actual": evidence_new or "not found",
            "old_residuals": [] if ok_old_gone else ["see artifact"],
            "locations": sorted(targets)}

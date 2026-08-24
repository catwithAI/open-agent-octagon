"""Bounded, non-evaluating XLSX structural preview renderer.

The renderer only reads OOXML parts from a container that has already passed
``inspect_artifact``.  It never evaluates formulae, loads external resources,
or expands arbitrary ZIP members.  Output is deliberately bounded so it is
safe to return in the preview descriptor and cheap for the browser to render.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET


RENDERER_NAME = "octagon-xlsx-structural"
RENDERER_VERSION = "1"

MAX_SHEETS = 32
MAX_ROWS_PER_SHEET = 500
MAX_COLUMNS = 100
MAX_CELLS_TOTAL = 10_000
MAX_SHARED_STRINGS = 20_000
MAX_MERGES_PER_SHEET = 2_000
MAX_COLUMN_METADATA = 500
MAX_XML_BYTES = 16 * 1024 * 1024

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS = {"x": _MAIN_NS, "r": _DOC_REL_NS, "pr": _PKG_REL_NS}
_CELL_REF = re.compile(r"^([A-Z]{1,4})([1-9][0-9]*)$")


@dataclass(slots=True)
class WorkbookPreviewError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise WorkbookPreviewError("xlsx_part_missing", f"工作簿缺少 {name}") from exc
    if info.file_size > MAX_XML_BYTES:
        raise WorkbookPreviewError("xlsx_xml_too_large", f"{name} 超过结构化预览上限")
    raw = archive.read(info)
    # Scan the complete bounded part: a long XML declaration/comment must not
    # be able to push a DTD beyond a prefix-only check.
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise WorkbookPreviewError("xlsx_unsafe_xml", f"{name} 包含不允许的 XML 声明")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise WorkbookPreviewError("xlsx_xml_invalid", f"{name} XML 损坏") from exc


def _column_index(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - 64
    return value


def _cell_position(ref: str) -> tuple[int, int] | None:
    match = _CELL_REF.fullmatch(ref)
    if not match:
        return None
    return int(match.group(2)), _column_index(match.group(1))


def _text_content(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext())


def _shared_strings(archive: zipfile.ZipFile) -> tuple[list[str], bool]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return [], False
    root = _read_xml(archive, "xl/sharedStrings.xml")
    values: list[str] = []
    truncated = False
    for item in root.findall("x:si", _NS):
        if len(values) >= MAX_SHARED_STRINGS:
            truncated = True
            break
        values.append(_text_content(item))
    return values, truncated


def _number_formats(archive: zipfile.ZipFile) -> list[str | None]:
    if "xl/styles.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/styles.xml")
    custom = {
        int(item.attrib["numFmtId"]): item.attrib.get("formatCode", "")
        for item in root.findall("x:numFmts/x:numFmt", _NS)
        if item.attrib.get("numFmtId", "").isdigit()
    }
    builtins = {
        0: "General", 1: "0", 2: "0.00", 9: "0%", 10: "0.00%",
        14: "date", 15: "date", 16: "date", 17: "date",
        18: "time", 19: "time", 20: "time", 21: "time", 22: "datetime",
        49: "@",
    }
    formats: list[str | None] = []
    for xf in root.findall("x:cellXfs/x:xf", _NS):
        raw_id = xf.attrib.get("numFmtId", "0")
        num_fmt_id = int(raw_id) if raw_id.isdigit() else 0
        formats.append(custom.get(num_fmt_id, builtins.get(num_fmt_id)))
    return formats


def _sheet_targets(
    archive: zipfile.ZipFile,
) -> tuple[list[tuple[str, str]], bool, bool]:
    workbook = _read_xml(archive, "xl/workbook.xml")
    workbook_props = workbook.find("x:workbookPr", _NS)
    date1904 = (
        workbook_props is not None
        and workbook_props.attrib.get("date1904", "").lower() in {"1", "true"}
    )
    relationships = _read_xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {
        rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
        for rel in relationships.findall("pr:Relationship", _NS)
        if rel.attrib.get("TargetMode") != "External"
    }
    sheets: list[tuple[str, str]] = []
    workbook_sheets = workbook.findall("x:sheets/x:sheet", _NS)
    for index, sheet in enumerate(workbook_sheets, start=1):
        if len(sheets) >= MAX_SHEETS:
            break
        rel_id = sheet.attrib.get(f"{{{_DOC_REL_NS}}}id", "")
        target = targets.get(rel_id)
        if not target:
            continue
        normalized = posixpath.normpath(posixpath.join("xl", target.replace("\\", "/")))
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts or not normalized.startswith("xl/"):
            continue
        sheets.append((sheet.attrib.get("name") or f"Sheet {index}", normalized))
    if not sheets:
        raise WorkbookPreviewError("xlsx_sheet_missing", "工作簿没有可读取的工作表")
    return sheets, len(workbook_sheets) > len(sheets), date1904


def _cell_value(
    cell: ET.Element,
    shared: list[str],
    number_formats: list[str | None],
    *,
    date1904: bool,
) -> dict[str, Any]:
    cell_type = cell.attrib.get("t", "n")
    formula_node = cell.find("x:f", _NS)
    value_node = cell.find("x:v", _NS)
    raw = value_node.text if value_node is not None and value_node.text is not None else ""
    if cell_type == "s":
        try:
            display: Any = shared[int(raw)]
        except (ValueError, IndexError):
            display = ""
    elif cell_type == "inlineStr":
        display = _text_content(cell.find("x:is", _NS))
    elif cell_type == "b":
        display = raw == "1"
    elif cell_type in {"str", "e", "d"}:
        display = raw
    elif raw == "":
        display = None
    else:
        try:
            number = float(raw)
            display = int(number) if number.is_integer() else number
        except ValueError:
            display = raw
    style_raw = cell.attrib.get("s", "")
    style_index = int(style_raw) if style_raw.isdigit() else None
    number_format = (
        number_formats[style_index]
        if style_index is not None and style_index < len(number_formats)
        else None
    )
    display_value: Any = display
    if isinstance(display, (int, float)) and number_format:
        lower = number_format.lower()
        try:
            has_date = number_format in {"date", "datetime"} or any(
                token in lower for token in ("yy", "dd", "mmm")
            )
            has_time = number_format in {"time", "datetime"} or any(
                token in lower for token in ("hh", "h:", "ss")
            )
            if has_date or has_time:
                epoch = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
                date = epoch + timedelta(days=float(display))
                if has_date and has_time:
                    display_value = date.strftime("%Y-%m-%d %H:%M:%S")
                elif has_date:
                    display_value = date.strftime("%Y-%m-%d")
                else:
                    display_value = date.strftime("%H:%M:%S")
            elif "%" in number_format:
                decimals = 2 if "0.00%" in number_format else 0
                display_value = f"{float(display) * 100:.{decimals}f}%"
            elif any(symbol in number_format for symbol in ("¥", "￥", "$", "€", "£")):
                symbol = next(item for item in ("¥", "￥", "$", "€", "£") if item in number_format)
                decimals = 2 if ".00" in number_format else 0
                display_value = f"{symbol}{float(display):,.{decimals}f}"
        except (OverflowError, ValueError):
            display_value = display
    return {
        "value": display,
        "display_value": display_value,
        "raw_value": raw,
        "value_type": cell_type,
        "formula": formula_node.text if formula_node is not None else None,
        "number_format": number_format,
    }


def _render_sheet(
    archive: zipfile.ZipFile,
    *,
    name: str,
    target: str,
    shared: list[str],
    number_formats: list[str | None],
    remaining_cells: int,
    date1904: bool,
) -> tuple[dict[str, Any], int]:
    root = _read_xml(archive, target)
    rows: list[dict[str, Any]] = []
    cells_used = 0
    truncated = False
    for row in root.findall("x:sheetData/x:row", _NS):
        if len(rows) >= MAX_ROWS_PER_SHEET or cells_used >= remaining_cells:
            truncated = True
            break
        row_index = int(row.attrib.get("r", len(rows) + 1))
        cells: list[dict[str, Any]] = []
        for cell in row.findall("x:c", _NS):
            ref = cell.attrib.get("r", "")
            position = _cell_position(ref)
            if position is None or position[1] > MAX_COLUMNS:
                truncated = True
                continue
            if cells_used >= remaining_cells:
                truncated = True
                break
            cells.append({
                "ref": ref,
                "row": position[0],
                "column": position[1],
                **_cell_value(cell, shared, number_formats, date1904=date1904),
            })
            cells_used += 1
        if cells or row.attrib.get("hidden") == "1":
            rows.append({
                "index": row_index,
                "hidden": row.attrib.get("hidden") == "1",
                "height": float(row.attrib["ht"]) if row.attrib.get("ht") else None,
                "cells": cells,
            })

    merge_nodes = root.findall("x:mergeCells/x:mergeCell", _NS)
    if len(merge_nodes) > MAX_MERGES_PER_SHEET:
        truncated = True
    merges = [
        item.attrib["ref"]
        for item in merge_nodes[:MAX_MERGES_PER_SHEET]
        if item.attrib.get("ref")
    ]
    columns = []
    column_nodes = root.findall("x:cols/x:col", _NS)
    if len(column_nodes) > MAX_COLUMN_METADATA:
        truncated = True
    for item in column_nodes[:MAX_COLUMN_METADATA]:
        columns.append({
            "min": int(item.attrib.get("min", "1")),
            "max": int(item.attrib.get("max", item.attrib.get("min", "1"))),
            "width": float(item.attrib["width"]) if item.attrib.get("width") else None,
            "hidden": item.attrib.get("hidden") == "1",
        })
    pane = root.find("x:sheetViews/x:sheetView/x:pane", _NS)
    frozen = None
    if pane is not None and pane.attrib.get("state") in {"frozen", "frozenSplit"}:
        frozen = {
            "top_left_cell": pane.attrib.get("topLeftCell"),
            "x_split": int(float(pane.attrib.get("xSplit", "0"))),
            "y_split": int(float(pane.attrib.get("ySplit", "0"))),
        }
    dimension = root.find("x:dimension", _NS)
    return ({
        "name": name,
        "dimension": dimension.attrib.get("ref") if dimension is not None else None,
        "rows": rows,
        "merges": merges,
        "columns": columns,
        "frozen": frozen,
        "truncated": truncated,
    }, cells_used)


def render_workbook(path: Path) -> dict[str, Any]:
    """Return a bounded workbook preview without executing formulas."""
    try:
        with zipfile.ZipFile(path) as archive:
            shared, shared_truncated = _shared_strings(archive)
            number_formats = _number_formats(archive)
            targets, sheets_truncated, date1904 = _sheet_targets(archive)
            sheets: list[dict[str, Any]] = []
            total_cells = 0
            for name, target in targets:
                if total_cells >= MAX_CELLS_TOTAL:
                    break
                sheet, used = _render_sheet(
                    archive,
                    name=name,
                    target=target,
                    shared=shared,
                    number_formats=number_formats,
                    remaining_cells=MAX_CELLS_TOTAL - total_cells,
                    date1904=date1904,
                )
                total_cells += used
                sheets.append(sheet)
            return {
                "kind": "workbook",
                "sheets": sheets,
                "truncated": (
                    shared_truncated
                    or sheets_truncated
                    or total_cells >= MAX_CELLS_TOTAL
                    or any(sheet["truncated"] for sheet in sheets)
                ),
                "limits": {
                    "sheets": MAX_SHEETS,
                    "rows_per_sheet": MAX_ROWS_PER_SHEET,
                    "columns": MAX_COLUMNS,
                    "cells_total": MAX_CELLS_TOTAL,
                },
                "formulas_evaluated": False,
            }
    except WorkbookPreviewError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        raise WorkbookPreviewError("xlsx_render_failed", "工作簿结构化预览失败") from exc

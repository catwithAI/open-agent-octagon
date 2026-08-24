"""Bounded DOCX semantic document renderer for the isolated Office worker."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET


RENDERER_NAME = "octagon-docx-structural"
RENDERER_VERSION = "1"
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_BLOCKS = 5000
MAX_TABLE_CELLS = 10_000
MAX_TEXT_BYTES = 4 * 1024 * 1024

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W, "r": R, "pr": PR}


@dataclass(slots=True)
class DocumentPreviewError(Exception):
    code: str
    message: str


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise DocumentPreviewError("docx_part_missing", f"文档缺少 {name}") from exc
    if info.file_size > MAX_XML_BYTES:
        raise DocumentPreviewError("docx_xml_too_large", f"{name} 超过预览上限")
    raw = archive.read(info)
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise DocumentPreviewError("docx_unsafe_xml", f"{name} 包含不允许的 XML 声明")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DocumentPreviewError("docx_xml_invalid", f"{name} XML 损坏") from exc


def _safe_url(value: str) -> str | None:
    parsed = urlparse(value)
    return value if parsed.scheme.lower() in {"http", "https", "mailto"} else None


def _relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    name = "word/_rels/document.xml.rels"
    if name not in archive.namelist():
        return {}
    root = _read_xml(archive, name)
    result = {}
    for rel in root.findall("pr:Relationship", NS):
        if rel.attrib.get("TargetMode") != "External":
            continue
        safe = _safe_url(rel.attrib.get("Target", ""))
        if safe:
            result[rel.attrib.get("Id", "")] = safe
    return result


def _styles(archive: zipfile.ZipFile) -> dict[str, str]:
    if "word/styles.xml" not in archive.namelist():
        return {}
    root = _read_xml(archive, "word/styles.xml")
    result = {}
    for style in root.findall("w:style", NS):
        style_id = style.attrib.get(f"{{{W}}}styleId")
        name = style.find("w:name", NS)
        if style_id and name is not None:
            result[style_id] = name.attrib.get(f"{{{W}}}val", style_id)
    return result


def _node_text(node: ET.Element) -> str:
    parts: list[str] = []
    for item in node.iter():
        if item.tag == f"{{{W}}}t":
            parts.append(item.text or "")
        elif item.tag == f"{{{W}}}tab":
            parts.append("\t")
        elif item.tag in {f"{{{W}}}br", f"{{{W}}}cr"}:
            parts.append("\n")
    return "".join(parts)


def _paragraph(node: ET.Element, styles: dict[str, str], links: dict[str, str]) -> dict[str, Any]:
    props = node.find("w:pPr", NS)
    style_node = props.find("w:pStyle", NS) if props is not None else None
    style_id = style_node.attrib.get(f"{{{W}}}val") if style_node is not None else None
    style = styles.get(style_id or "", style_id)
    list_item = props is not None and props.find("w:numPr", NS) is not None
    runs = []
    for child in node:
        if child.tag == f"{{{W}}}hyperlink":
            rel_id = child.attrib.get(f"{{{R}}}id", "")
            text = _node_text(child)
            if text:
                runs.append({"text": text, "href": links.get(rel_id)})
        elif child.tag == f"{{{W}}}r":
            text = _node_text(child)
            if text:
                rpr = child.find("w:rPr", NS)
                runs.append({
                    "text": text,
                    "bold": rpr is not None and rpr.find("w:b", NS) is not None,
                    "italic": rpr is not None and rpr.find("w:i", NS) is not None,
                    "href": None,
                })
    text = "".join(run["text"] for run in runs)
    lower = (style or "").lower()
    heading = None
    if lower.startswith("heading"):
        suffix = "".join(ch for ch in lower if ch.isdigit())
        heading = max(1, min(6, int(suffix or "1")))
    return {"kind": "paragraph", "text": text, "runs": runs, "style": style, "heading": heading, "list_item": list_item}


def _table(
    node: ET.Element,
    styles: dict[str, str],
    links: dict[str, str],
    *,
    max_cells: int,
) -> tuple[dict[str, Any], int, bool]:
    rows = []
    cells_used = 0
    truncated = False
    for tr in node.findall("w:tr", NS):
        cells = []
        for tc in tr.findall("w:tc", NS):
            if cells_used >= max_cells:
                truncated = True
                break
            paragraphs = [_paragraph(p, styles, links) for p in tc.findall("w:p", NS)]
            cells.append({"text": "\n".join(p["text"] for p in paragraphs), "blocks": paragraphs})
            cells_used += 1
        if cells:
            rows.append(cells)
        if truncated:
            break
    return {"kind": "table", "rows": rows}, cells_used, truncated


def _section_page(body: ET.Element) -> dict[str, float | None]:
    section = body.find("w:sectPr", NS)
    size = section.find("w:pgSz", NS) if section is not None else None
    def points(name: str) -> float | None:
        raw = size.attrib.get(f"{{{W}}}{name}") if size is not None else None
        return int(raw) / 20 if raw and raw.isdigit() else None
    return {"width_pt": points("w"), "height_pt": points("h")}


def _peripheral_blocks(
    archive: zipfile.ZipFile,
    prefix: str,
    styles: dict[str, str],
    links: dict[str, str],
    *,
    max_blocks: int,
    max_text_bytes: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    blocks = []
    text_bytes = 0
    truncated = False
    for name in sorted(
        item for item in archive.namelist()
        if item.startswith(f"word/{prefix}") and item.endswith(".xml")
    )[:20]:
        root = _read_xml(archive, name)
        for paragraph in root.findall(".//w:p", NS)[:200]:
            block = _paragraph(paragraph, styles, links)
            if not block["text"]:
                continue
            encoded = len(json_text(block).encode("utf-8"))
            if len(blocks) >= max_blocks or text_bytes + encoded > max_text_bytes:
                truncated = True
                return blocks, text_bytes, truncated
            blocks.append(block)
            text_bytes += encoded
    return blocks, text_bytes, truncated


def render_document(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            root = _read_xml(archive, "word/document.xml")
            body = root.find("w:body", NS)
            if body is None:
                raise DocumentPreviewError("docx_body_missing", "文档没有正文")
            styles = _styles(archive)
            links = _relationships(archive)
            blocks = []
            text_bytes = 0
            table_cells = 0
            truncated = False
            for child in body:
                if len(blocks) >= MAX_BLOCKS:
                    truncated = True
                    break
                if child.tag == f"{{{W}}}p":
                    block = _paragraph(child, styles, links)
                elif child.tag == f"{{{W}}}tbl":
                    remaining_cells = MAX_TABLE_CELLS - table_cells
                    if remaining_cells <= 0:
                        truncated = True
                        continue
                    block, used, table_truncated = _table(
                        child, styles, links, max_cells=remaining_cells
                    )
                    table_cells += used
                    if table_truncated:
                        truncated = True
                else:
                    continue
                encoded = len(json_text(block).encode("utf-8"))
                if text_bytes + encoded > MAX_TEXT_BYTES:
                    truncated = True
                    break
                text_bytes += encoded
                blocks.append(block)
            drawings = len(root.findall(".//w:drawing", NS))
            features = []
            if root.findall(".//w:ins", NS) or root.findall(".//w:del", NS):
                # _paragraph only consumes direct runs/hyperlinks; revision
                # containers are deliberately omitted instead of pretending
                # they were flattened into the visible text.
                features.append("tracked-changes-not-rendered")
            if "word/comments.xml" in archive.namelist():
                features.append("comments-not-rendered")
            if "word/footnotes.xml" in archive.namelist() or "word/endnotes.xml" in archive.namelist():
                features.append("notes-not-rendered")
            remaining_blocks = max(0, MAX_BLOCKS - len(blocks))
            remaining_text = max(0, MAX_TEXT_BYTES - text_bytes)
            headers, header_bytes, headers_truncated = _peripheral_blocks(
                archive, "header", styles, links,
                max_blocks=remaining_blocks, max_text_bytes=remaining_text,
            )
            remaining_blocks = max(0, remaining_blocks - len(headers))
            remaining_text = max(0, remaining_text - header_bytes)
            footers, _footer_bytes, footers_truncated = _peripheral_blocks(
                archive, "footer", styles, links,
                max_blocks=remaining_blocks, max_text_bytes=remaining_text,
            )
            return {
                "kind": "document",
                "blocks": blocks,
                "headers": headers,
                "footers": footers,
                "page": _section_page(body),
                "truncated": truncated or headers_truncated or footers_truncated,
                "images_omitted": drawings,
                "external_links": len(links),
                "features": features,
                "active_content_executed": False,
                "limits": {"blocks": MAX_BLOCKS, "table_cells": MAX_TABLE_CELLS},
            }
    except DocumentPreviewError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, KeyError) as exc:
        raise DocumentPreviewError("docx_render_failed", "Word 文档结构化预览失败") from exc


def json_text(value: Any) -> str:
    if isinstance(value, dict):
        return "".join(json_text(item) for item in value.values())
    if isinstance(value, list):
        return "".join(json_text(item) for item in value)
    return value if isinstance(value, str) else ""

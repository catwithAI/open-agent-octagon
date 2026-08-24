"""Bounded PPTX static-layout renderer for the isolated Office worker."""

from __future__ import annotations

import base64
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET


RENDERER_NAME = "octagon-pptx-static"
RENDERER_VERSION = "1"
MAX_SLIDES = 100
MAX_ELEMENTS_PER_SLIDE = 500
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGES_BYTES = 16 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"p": P, "a": A, "r": R, "pr": PR}


@dataclass(slots=True)
class PresentationPreviewError(Exception):
    code: str
    message: str


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise PresentationPreviewError("pptx_part_missing", f"演示文稿缺少 {name}") from exc
    if info.file_size > MAX_XML_BYTES:
        raise PresentationPreviewError("pptx_xml_too_large", f"{name} 超过预览上限")
    raw = archive.read(info)
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise PresentationPreviewError("pptx_unsafe_xml", f"{name} 包含不允许的 XML 声明")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PresentationPreviewError("pptx_xml_invalid", f"{name} XML 损坏") from exc


def _safe_target(base: str, target: str) -> str | None:
    normalized = posixpath.normpath(posixpath.join(base, target.replace("\\", "/")))
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or not normalized.startswith("ppt/"):
        return None
    return normalized


def _relationships(archive: zipfile.ZipFile, name: str, base: str) -> dict[str, str]:
    if name not in archive.namelist():
        return {}
    root = _read_xml(archive, name)
    result: dict[str, str] = {}
    for rel in root.findall("pr:Relationship", NS):
        if rel.attrib.get("TargetMode") == "External":
            continue
        target = _safe_target(base, rel.attrib.get("Target", ""))
        if target:
            result[rel.attrib.get("Id", "")] = target
    return result


def _xfrm(node: ET.Element, width: int, height: int) -> dict[str, float]:
    xfrm = node.find(".//a:xfrm", NS)
    off = xfrm.find("a:off", NS) if xfrm is not None else None
    ext = xfrm.find("a:ext", NS) if xfrm is not None else None
    def ratio(value: str | None, total: int) -> float:
        try:
            return max(0.0, min(1.0, int(value or "0") / max(total, 1)))
        except ValueError:
            return 0.0
    return {
        "x": ratio(off.attrib.get("x") if off is not None else None, width),
        "y": ratio(off.attrib.get("y") if off is not None else None, height),
        "width": ratio(ext.attrib.get("cx") if ext is not None else None, width),
        "height": ratio(ext.attrib.get("cy") if ext is not None else None, height),
    }


def _shape_text(shape: ET.Element) -> str:
    paragraphs = []
    for paragraph in shape.findall(".//a:p", NS):
        text = "".join((item.text or "") for item in paragraph.findall(".//a:t", NS))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _shape(shape: ET.Element, width: int, height: int) -> dict[str, Any] | None:
    text = _shape_text(shape)
    if not text:
        return None
    placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
    fill = shape.find("p:spPr/a:solidFill/a:srgbClr", NS)
    run = shape.find(".//a:rPr", NS)
    if run is None:
        run = shape.find(".//a:defRPr", NS)
    font_size = None
    if run is not None and run.attrib.get("sz", "").isdigit():
        font_size = int(run.attrib["sz"]) / 100
    return {
        "kind": "text",
        **_xfrm(shape, width, height),
        "text": text,
        "role": placeholder.attrib.get("type") if placeholder is not None else None,
        "fill": f"#{fill.attrib['val']}" if fill is not None and re.fullmatch(r"[0-9A-Fa-f]{6}", fill.attrib.get("val", "")) else None,
        "font_size": font_size,
    }


def _image_data(archive: zipfile.ZipFile, target: str) -> str | None:
    try:
        info = archive.getinfo(target)
    except KeyError:
        return None
    if info.file_size > MAX_IMAGE_BYTES:
        return None
    raw = archive.read(info)
    mime = None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    if mime is None:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _picture(
    archive: zipfile.ZipFile, node: ET.Element, rels: dict[str, str], width: int, height: int
) -> tuple[dict[str, Any] | None, int]:
    blip = node.find("p:blipFill/a:blip", NS)
    rel_id = blip.attrib.get(f"{{{R}}}embed") if blip is not None else None
    target = rels.get(rel_id or "")
    if not target:
        return None, 0
    data = _image_data(archive, target)
    if data is None:
        return None, 0
    return ({"kind": "image", **_xfrm(node, width, height), "data_uri": data}, len(data))


def _table(node: ET.Element, width: int, height: int) -> dict[str, Any] | None:
    table = node.find(".//a:tbl", NS)
    if table is None:
        return None
    rows = []
    for tr in table.findall("a:tr", NS)[:100]:
        rows.append([_shape_text(tc) for tc in tr.findall("a:tc", NS)[:50]])
    return {"kind": "table", **_xfrm(node, width, height), "rows": rows}


def _slide_targets(archive: zipfile.ZipFile) -> tuple[int, int, list[str], bool]:
    presentation = _read_xml(archive, "ppt/presentation.xml")
    size = presentation.find("p:sldSz", NS)
    width = int(size.attrib.get("cx", "12192000")) if size is not None else 12192000
    height = int(size.attrib.get("cy", "6858000")) if size is not None else 6858000
    rels = _relationships(archive, "ppt/_rels/presentation.xml.rels", "ppt")
    targets = []
    nodes = presentation.findall("p:sldIdLst/p:sldId", NS)
    for node in nodes[:MAX_SLIDES]:
        target = rels.get(node.attrib.get(f"{{{R}}}id", ""))
        if target:
            targets.append(target)
    if not targets:
        targets = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"(\d+)", name).group(1)),  # type: ignore[union-attr]
        )[:MAX_SLIDES]
    if not targets:
        raise PresentationPreviewError("pptx_slide_missing", "演示文稿没有可读取的幻灯片")
    return width, height, targets, len(nodes) > len(targets)


def render_presentation(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            width, height, targets, truncated = _slide_targets(archive)
            slides = []
            text_bytes = 0
            image_bytes = 0
            features: set[str] = set()
            for number, target in enumerate(targets, start=1):
                root = _read_xml(archive, target)
                if root.tag != f"{{{P}}}sld":
                    raise PresentationPreviewError("pptx_slide_invalid", f"{target} 不是有效幻灯片")
                if root.find("p:transition", NS) is not None:
                    features.add("transitions-not-rendered")
                if root.find("p:timing", NS) is not None:
                    features.add("animations-not-rendered")
                for graphic in root.findall(".//a:graphicData", NS):
                    if "chart" in graphic.attrib.get("uri", "").lower():
                        features.add("charts-not-rendered")
                slide_name = PurePosixPath(target).name
                rel_name = f"ppt/slides/_rels/{slide_name}.rels"
                rels = _relationships(archive, rel_name, "ppt/slides")
                elements: list[dict[str, Any]] = []
                for shape in root.findall(".//p:sp", NS):
                    item = _shape(shape, width, height)
                    if item:
                        encoded = len(item["text"].encode("utf-8"))
                        if text_bytes + encoded > MAX_TEXT_BYTES:
                            truncated = True
                            break
                        text_bytes += encoded
                        elements.append(item)
                    if len(elements) >= MAX_ELEMENTS_PER_SLIDE:
                        truncated = True
                        break
                for frame in root.findall(".//p:graphicFrame", NS):
                    item = _table(frame, width, height)
                    if item and len(elements) < MAX_ELEMENTS_PER_SLIDE:
                        encoded = sum(
                            len(cell.encode("utf-8"))
                            for row in item["rows"] for cell in row
                        )
                        if text_bytes + encoded <= MAX_TEXT_BYTES:
                            elements.append(item)
                            text_bytes += encoded
                        else:
                            truncated = True
                    elif item:
                        truncated = True
                for picture in root.findall(".//p:pic", NS):
                    item, used = _picture(archive, picture, rels, width, height)
                    if item and image_bytes + used <= MAX_IMAGES_BYTES and len(elements) < MAX_ELEMENTS_PER_SLIDE:
                        elements.append(item)
                        image_bytes += used
                    elif used:
                        truncated = True
                notes = None
                notes_target = next((value for value in rels.values() if "/notesSlides/" in value), None)
                if notes_target and notes_target in archive.namelist():
                    notes_root = _read_xml(archive, notes_target)
                    notes_text = _shape_text(notes_root)
                    if notes_text:
                        candidate = notes_text[:20_000]
                        remaining = max(0, MAX_TEXT_BYTES - text_bytes)
                        encoded = candidate.encode("utf-8")
                        if len(encoded) > remaining:
                            candidate = encoded[:remaining].decode("utf-8", errors="ignore")
                            truncated = True
                        notes = candidate or None
                        text_bytes += len(candidate.encode("utf-8"))
                slides.append({"number": number, "elements": elements, "notes": notes})
            return {
                "kind": "presentation",
                "width": width,
                "height": height,
                "aspect_ratio": width / max(height, 1),
                "slides": slides,
                "features": sorted(features),
                "truncated": truncated,
                "limits": {"slides": MAX_SLIDES, "elements_per_slide": MAX_ELEMENTS_PER_SLIDE},
                "active_content_executed": False,
            }
    except PresentationPreviewError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, KeyError) as exc:
        raise PresentationPreviewError("pptx_render_failed", "演示文稿静态预览失败") from exc

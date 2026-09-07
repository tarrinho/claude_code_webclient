#!/usr/bin/env python3
"""Render the backend comparison Markdown as a small, dependency-free PDF.

The WebConsole invokes this fixed build step from an authenticated route. It
uses only the standard library so a runtime does not need to install a PDF
package before it can serve the reviewed report.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import textwrap
from pathlib import Path

_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842
_LEFT = 48
_TOP = 790
_BOTTOM = 48
_BODY_SIZE = 9
_BODY_LEADING = 13
_HEADING_SIZE = 15
_TITLE_SIZE = 21
_MAX_CHARS = 91


def _pdf_text(value: str) -> str:
    """Encode text for a built-in PDF font without emitting invalid syntax."""
    value = value.replace("\t", "    ")
    value = value.encode("latin-1", "replace").decode("latin-1")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _plain(value: str) -> str:
    """Turn common Markdown decoration into readable plain text."""
    value = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"[image: \1]", value)
    value = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[`*_]+", "", value)
    value = re.sub(r"^\s{0,3}[-*+]\s+", "• ", value)
    value = re.sub(r"^\s*\d+[.)]\s+", "• ", value)
    return html.unescape(value).strip()


def _markdown_lines(source: str) -> list[tuple[str, str]]:
    """Return ``(text, style)`` rows suitable for the simple PDF renderer."""
    rows: list[tuple[str, str]] = []
    in_code = False
    for raw in source.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if not line.strip():
            rows.append(("", "body"))
            continue
        if in_code:
            rows.append((line, "code"))
            continue
        if line.startswith("# "):
            rows.append((_plain(line[2:]), "title"))
        elif line.startswith("## "):
            rows.append((_plain(line[3:]), "heading"))
        elif line.startswith("### "):
            rows.append((_plain(line[4:]), "subheading"))
        elif line.startswith("|"):
            cells = [_plain(cell.strip()) for cell in line.strip("|").split("|")]
            if set(cells) <= {"", "-", ":", "--", "---"} or all(
                not cell.replace("-", "").replace(":", "").strip() for cell in cells
            ):
                continue
            rows.append((" | ".join(cells), "table"))
        else:
            rows.append((_plain(line), "body"))
    return rows


def _wrap(text: str, style: str) -> list[tuple[str, str]]:
    if not text:
        return [("", style)]
    width = 78 if style in {"table", "code"} else _MAX_CHARS
    return [(part, style) for part in textwrap.wrap(
        text, width=width, break_long_words=True, break_on_hyphens=False
    ) or [""]]


def _stream(rows: list[tuple[str, str]]) -> list[bytes]:
    pages: list[bytes] = []
    current: list[tuple[str, float, float, str, float]] = []
    y = _TOP

    def flush() -> None:
        nonlocal current, y
        if not current:
            return
        content = ["q", "BT"]
        for text, size, _leading, font, line_y in current:
            content.append(
                f"/{font} {size} Tf 1 0 0 1 {_LEFT} {line_y:.1f} Tm ({_pdf_text(text)}) Tj"
            )
        content.extend(["ET", "Q"])
        pages.append(("\n".join(content) + "\n").encode("latin-1"))
        current = []
        y = _TOP

    for text, style in rows:
        for wrapped, wrapped_style in _wrap(text, style):
            if wrapped_style == "title":
                size, leading, font = _TITLE_SIZE, 27, "F2"
            elif wrapped_style == "heading":
                size, leading, font = _HEADING_SIZE, 21, "F2"
            elif wrapped_style == "subheading":
                size, leading, font = 11, 16, "F2"
            elif wrapped_style == "code":
                size, leading, font = 7.5, 11, "F3"
            elif wrapped_style == "table":
                size, leading, font = 7.2, 10, "F1"
            else:
                size, leading, font = _BODY_SIZE, _BODY_LEADING, "F1"
            if y - leading < _BOTTOM:
                flush()
            current.append((wrapped, size, leading, font, y))
            y -= leading
        if style in {"title", "heading", "subheading"}:
            y -= 5
    flush()
    return pages


def _pdf(source: Path) -> bytes:
    rows = _markdown_lines(source.read_text(encoding="utf-8"))
    streams = _stream(rows)
    objects: list[bytes] = []

    def add(value: str | bytes) -> int:
        objects.append(value.encode("latin-1") if isinstance(value, str) else value)
        return len(objects)

    catalog = add(b"")
    pages = add(b"")
    font_body = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_bold = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    font_mono = add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    page_ids: list[int] = []
    for stream in streams:
        stream_id = add(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream + b"endstream"
        )
        page_ids.append(add(b""))
        page_id = page_ids[-1]
        objects[page_id - 1] = (
            b"<< /Type /Page /Parent " + str(pages).encode("ascii") + b" 0 R "
            b"/MediaBox [0 0 595 842] /Resources << /Font << /F1 "
            + str(font_body).encode("ascii") + b" 0 R /F2 "
            + str(font_bold).encode("ascii") + b" 0 R /F3 "
            + str(font_mono).encode("ascii") + b" 0 R >> >> /Contents "
            + str(stream_id).encode("ascii") + b" 0 R >>"
        )
    kids = b" ".join(str(page).encode("ascii") + b" 0 R" for page in page_ids)
    objects[pages - 1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count "
        + str(len(page_ids)).encode("ascii") + b" >>"
    )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages " + str(pages).encode("ascii") + b" 0 R >>"

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        b"trailer\n<< /Size " + str(len(objects) + 1).encode("ascii")
        + b" /Root " + str(catalog).encode("ascii") + b" 0 R >>\n"
        + b"startxref\n" + str(xref).encode("ascii") + b"\n%%EOF\n"
    )
    return bytes(output)


def render(source: Path, target: Path) -> None:
    """Render *source* atomically to *target*."""
    data = _pdf(source)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temporary.write_bytes(data)
    os.replace(temporary, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    render(args.source, args.target)
    print(args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

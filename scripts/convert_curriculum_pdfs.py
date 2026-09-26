#!/usr/bin/env python3
"""Convert chapter PDFs under curriculum/ to markdown in place, then delete PDFs."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
import pymupdf4llm

CURRICULUM = Path(__file__).resolve().parent.parent / "curriculum"

SINGLE_INLINE = {
    "stdin", "stdout", "stderr", "tmpfile", "tmpnam", "setvbuf", "setbuf",
    "remove", "rename", "freopen", "fopen", "fclose", "fflush", "getc", "putc",
}


def sanitize(name: str) -> str:
    name = re.sub(r"[\[\]]", "", name)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def is_code_font(name: str) -> bool:
    n = name.lower()
    return "courier" in n or "mono" in n or "typewriter" in n


def is_heading_font(name: str) -> bool:
    return any(
        k in name
        for k in (
            "FranklinGothic",
            "Helvetica-Bold",
            "Times-Bold",
            "StoneSerifStd-Semibold",
            "FranklinGothic-Demi",
        )
    )


def is_header_footer(y: float, page_h: float, size: float) -> bool:
    if size >= 16:
        return False
    return y < 46 or y > page_h - 28


@dataclass
class Line:
    y: float
    x: float
    text: str
    md_inline: str
    code: bool
    heading: int | None
    margin: bool
    size: float
    bullet: bool


def inline_markdown(spans) -> tuple[str, bool]:
    parts = []
    bullet = False
    for s in spans:
        t = s["text"]
        if not t:
            continue
        font = s["font"]
        if "ZapfDingbats" in font:
            if t.strip():
                parts.append("•")
                bullet = True
            continue
        if is_code_font(font) and t.strip():
            lead = len(t) - len(t.lstrip())
            trail = len(t) - len(t.rstrip())
            core = t.strip().replace("`", "\\`")
            parts.append(" " * lead + f"`{core}`" + " " * trail)
        else:
            parts.append(t)
    text = "".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+,", ",", text)
    return text.strip(), bullet


def classify_heading(spans, text: str) -> int | None:
    if not text.strip():
        return None
    max_size = max(s["size"] for s in spans)
    fonts = [s["font"] for s in spans]
    if not any(is_heading_font(f) for f in fonts):
        return None
    if re.match(r"^\d+\.\s", text) and max_size < 12:
        return None
    if max_size < 10.5 and not any(
        k in text for k in ("Listing", "Table", "PROGRAM", "Caution", "Note", "Q & A", "Q&A")
    ):
        return None
    if max_size >= 22:
        return 1
    if max_size >= 14:
        return 2
    if max_size >= 11.5:
        return 3
    if any(k in text for k in ("Listing", "Table", "PROGRAM", "Caution", "Note", "Q & A", "Q&A")):
        return 4
    return None


def page_lines(page, book: str) -> list[Line]:
    d = page.get_text("dict")
    raw = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b["lines"]:
            spans = [s for s in line["spans"] if s.get("text") is not None]
            if not spans:
                continue
            y = min(s["bbox"][1] for s in spans)
            x = min(s["bbox"][0] for s in spans)
            size = max(s["size"] for s in spans)
            plain = "".join(s["text"] for s in spans)
            if is_header_footer(y, page.rect.height, size):
                continue
            fonts_nd = [s for s in spans if "ZapfDingbats" not in s["font"]]
            code = bool(fonts_nd) and all(is_code_font(s["font"]) for s in fonts_nd)
            md, bullet = (plain, False) if code else inline_markdown(spans)
            heading = None if code else classify_heading(spans, plain.strip())
            margin = book == "king" and x < 132 and not code
            if not md.strip() and not code:
                continue
            raw.append(
                Line(y=y, x=x, text=plain, md_inline=md, code=code, heading=heading,
                     margin=margin, size=size, bullet=bullet)
            )
    raw.sort(key=lambda ln: (round(ln.y, 1), ln.x))
    merged: list[Line] = []
    for ln in raw:
        if merged and abs(ln.y - merged[-1].y) < 2.2:
            prev = merged[-1]
            if prev.margin and not ln.margin:
                merged.append(ln)
                continue
            if not prev.code and not ln.code:
                joiner = " " if prev.md_inline and ln.md_inline else ""
                prev.md_inline = (prev.md_inline + joiner + ln.md_inline).strip()
                prev.text = (prev.text + " " + ln.text).strip()
                prev.bullet = prev.bullet or ln.bullet
                if ln.heading and not prev.heading:
                    prev.heading = ln.heading
                continue
        merged.append(ln)
    return merged


def join_paragraph(lines: list[Line]) -> str:
    chunks = []
    for ln in lines:
        t = ln.md_inline.strip()
        if not t:
            continue
        if chunks and chunks[-1].endswith("-") and re.match(r"^[a-zà-öø-ÿ]", t):
            chunks[-1] = chunks[-1][:-1] + t
        else:
            chunks.append(t)
    text = " ".join(chunks)
    text = re.sub(r" +", " ", text)
    if text.startswith("•"):
        text = "- " + text[1:].strip()
    return text


def fence_code(lines: list[Line]) -> str:
    raw = [ln.text.rstrip() for ln in lines]
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()
    if not raw:
        return ""
    if len(raw) == 1:
        tok = raw[0].strip()
        if tok in SINGLE_INLINE or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tok):
            return f"`{tok}`"
    nonempty = [ln for ln in raw if ln.strip()]
    if nonempty:
        indents = [len(ln) - len(ln.lstrip(" ")) for ln in nonempty]
        pad = min(indents)
        raw = [ln[pad:] if len(ln) >= pad else ln for ln in raw]
    return "```c\n" + "\n".join(raw) + "\n```"


def emit_lines(lines: list[Line]) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.margin and not ln.code:
            label = re.sub(r"[*`]+", "", ln.md_inline).strip()
            if label and label not in {"Q", "&", "A", "Q&A"}:
                out.append(f"**{label}**")
            i += 1
            continue
        if ln.heading:
            hashes = "#" * ln.heading
            title = re.sub(r"[*`]+", "", ln.md_inline).strip()
            out.append(f"{hashes} {title}")
            i += 1
            continue
        if ln.code:
            j = i
            while j < n and lines[j].code:
                if j > i and lines[j].y - lines[j - 1].y > 24:
                    break
                j += 1
            fenced = fence_code(lines[i:j])
            if fenced:
                out.append(fenced)
            i = j
            continue
        j = i + 1
        while j < n:
            nxt = lines[j]
            if nxt.code or nxt.heading or nxt.margin:
                break
            if nxt.bullet:
                break
            if nxt.y - lines[j - 1].y > 16:
                break
            j += 1
        para = join_paragraph(lines[i:j])
        if para:
            out.append(para)
        i = j
    return out


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def extract_images(
    pdf: Path, img_dir: Path, img_stem: str, base_dir: Path
) -> dict[int, list[str]]:
    img_dir.mkdir(parents=True, exist_ok=True)
    pymupdf4llm.to_markdown(
        str(pdf),
        write_images=True,
        image_path=str(img_dir) + "/",
        image_format="png",
        filename=img_stem,
        dpi=150,
        pages=None,
    )
    mapping: dict[int, list[str]] = {}
    for p in sorted(img_dir.glob("*.png")):
        m = re.search(r"pdf-(\d{4})-\d+", p.name)
        if not m:
            continue
        size = png_size(p)
        if size:
            w, h = size
            if w < 80 or h < 20 or (w < 100 and h > 800):
                p.unlink(missing_ok=True)
                continue
        page_no = int(m.group(1))
        rel = p.relative_to(base_dir).as_posix()
        mapping.setdefault(page_no, []).append(rel)
    return mapping


def merge_adjacent_fences(md: str) -> str:
    def repl(m):
        a = m.group(1).rstrip("\n")
        b = m.group(2).lstrip("\n")
        return f"```c\n{a}\n{b}\n```"
    prev = None
    while prev != md:
        prev = md
        md = re.sub(r"```c\n(.*?)```\n\n```c\n(.*?)```", repl, md, flags=re.S)
    return md


def postprocess(md: str, pdf_stem: str) -> str:
    book = "king" if "king" in pdf_stem.lower() else "primer"
    md = re.sub(
        r"\*\*(\d+\.\d+)\*\*\n\n(#{2,3}) (.+)",
        lambda m: f"## {m.group(1)} {m.group(3)}",
        md,
    )
    md = merge_adjacent_fences(md)
    headings = {re.sub(r"^#+\s*", "", ln).strip() for ln in md.splitlines() if ln.startswith("#")}
    cleaned = []
    for para in re.split(r"\n\n+", md):
        s = para.strip()
        if s in headings and not s.startswith("#"):
            continue
        if re.fullmatch(r"\*\*\d+ .+\*\*", s):
            continue
        cleaned.append(para)
    md = "\n\n".join(cleaned)
    while True:
        m = re.match(r"^(# [^\n]+)\n\n(# [^\n]+)\n\n", md)
        if not m:
            break
        md = f"{m.group(1)} {m.group(2)[2:].strip()}\n\n" + md[m.end():]
    md = re.sub(r"^# (\d+)\n\n# (.+)$", r"# \1 \2", md, flags=re.M)
    m = re.search(r"\[king\] (\d+)-(.+)", pdf_stem, re.I)
    if book == "king" and m:
        num, rest = m.group(1), m.group(2).replace("-", " ").title()
        title = f"# {num} {rest}"
        if not md.lstrip().startswith(f"# {num}"):
            md = f"{title}\n\n" + md
        else:
            md = re.sub(rf"^# {num}\s+{num}\s+", f"# {num} ", md, count=1)
    m = re.search(r"\[c-primer-plus\] (\d+)-(.+)", pdf_stem, re.I)
    if book == "primer" and m:
        num, rest = m.group(1), m.group(2).replace("-", " ").title()
        if not md.lstrip().startswith(f"# {num}"):
            md = f"# {num} {rest}\n\n" + md
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def convert_pdf(pdf: Path) -> str:
    stem = pdf.stem
    book = "king" if "king" in stem.lower() else "primer"
    base_dir = pdf.parent
    img_stem = sanitize(stem)
    img_dir = base_dir / "images" / img_stem
    imgs = extract_images(pdf, img_dir, img_stem, base_dir)
    doc = fitz.open(pdf)
    parts: list[str] = []
    for idx, page in enumerate(doc):
        parts.extend(emit_lines(page_lines(page, book)))
        for rel in imgs.get(idx + 1, []):
            parts.append(f"![]({rel})")
    doc.close()
    return postprocess("\n\n".join(parts), stem)


def main() -> int:
    if not CURRICULUM.is_dir():
        print("Curriculum directory not found:", CURRICULUM, file=sys.stderr)
        return 1
    pdfs = sorted(CURRICULUM.rglob("*.pdf"))
    if not pdfs:
        print("No PDFs found under", CURRICULUM)
        return 0
    failed: list[str] = []
    for pdf in pdfs:
        rel = pdf.relative_to(CURRICULUM)
        print(f"Converting {rel}...", flush=True)
        try:
            md = convert_pdf(pdf)
            out = pdf.with_suffix(".md")
            out.write_text(md, encoding="utf-8")
            pdf.unlink()
            print(
                f"  -> {out.name}: {len(md):,} chars, "
                f"{md.count('```c')} code blocks, {md.count('![](')} images "
                f"(deleted {pdf.name})",
                flush=True,
            )
        except Exception as exc:
            print(f"  FAILED: {exc}", flush=True)
            failed.append(str(rel))
    print(f"Done. Converted {len(pdfs) - len(failed)}/{len(pdfs)} PDFs.")
    if failed:
        print("Failures:", file=sys.stderr)
        for name in failed:
            print(f"  {name}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
EPUB parser for audiobook generation.

Key design: extract heading map BEFORE any length-based filtering.
This fixes the bug where short _split_000 files (Chinese chapter headings,
7-11 chars) were silently dropped, causing missing Chinese titles in ch02-ch12.
"""

import re
import hashlib
from pathlib import Path
from typing import Optional
from html.parser import HTMLParser

import ebooklib
from ebooklib import epub

_CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def _html_to_text(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', text).strip()


def _extract_headings_from_html(html: str) -> dict:
    """
    Extract h1/h2/h3 and calibre9 paragraph headings from raw HTML.
    Returns {"zh": "中文标题", "en": "English Title"} where found.
    """
    headings = {"zh": None, "en": None}

    # h1/h2/h3 tags — typically Chinese chapter headings
    h_matches = re.findall(r'<h[123][^>]*>(.*?)</h[123]>', html, re.DOTALL | re.IGNORECASE)
    for raw in h_matches:
        text = re.sub(r'<[^>]+>', '', raw).strip()
        text = re.sub(r'\s+', ' ', text).strip()
        if not text:
            continue
        if _has_cjk(text) and not headings["zh"]:
            # Strip leading chapter numbers like "01   " or "02    "
            text = re.sub(r'^\d+\s+', '', text).strip()
            headings["zh"] = text
        elif not _has_cjk(text) and not headings["en"]:
            headings["en"] = text

    # calibre9 paragraphs — typically English subtitles in this EPUB format
    c9_matches = re.findall(
        r'<p[^>]*class="calibre9"[^>]*>(.*?)</p>', html, re.DOTALL | re.IGNORECASE
    )
    for raw in c9_matches:
        text = re.sub(r'<[^>]+>', '', raw).strip()
        text = re.sub(r'\s+', ' ', text).strip()
        if not text:
            continue
        if _has_cjk(text) and not headings["zh"]:
            headings["zh"] = text
        elif not _has_cjk(text) and not headings["en"]:
            headings["en"] = text

    return headings


def _build_heading_map(book: epub.EpubBook) -> dict:
    """
    Phase 1: scan ALL document items (regardless of text length) to build
    a map from item file_name → {"zh": ..., "en": ...}.

    This must run BEFORE any length-based filtering so that short heading-only
    files (like _split_000.html with 7-11 chars) are not silently dropped.
    """
    heading_map = {}
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        html = item.get_content().decode('utf-8', errors='ignore')
        headings = _extract_headings_from_html(html)
        if headings["zh"] or headings["en"]:
            heading_map[item.file_name] = headings
    return heading_map


def _sibling_heading(file_name: str, heading_map: dict) -> dict:
    """
    For a content file like 'split_002_split_001.html', look up the heading
    from its sibling 'split_002_split_000.html' (the heading-only file).
    """
    # Replace last _split_NNN with _split_000
    sibling = re.sub(r'(_split_\d+)\.html$', '_split_000.html', file_name)
    if sibling != file_name and sibling in heading_map:
        return heading_map[sibling]
    # Also try the parent split (split_002.html for split_002_split_001.html)
    parent = re.sub(r'_split_\d+\.html$', '.html', file_name)
    if parent != file_name and parent in heading_map:
        return heading_map[parent]
    return {"zh": None, "en": None}


def _make_title(own_headings: dict, sibling_headings: dict, fallback_text: str) -> str:
    """Compose a human-readable bilingual title."""
    zh = own_headings.get("zh") or sibling_headings.get("zh")
    en = own_headings.get("en") or sibling_headings.get("en")
    if zh and en:
        return f"{zh} / {en}"
    if zh:
        return zh
    if en:
        return en
    # Fallback: first ~40 chars of plain text
    return fallback_text[:40].strip() or "[无标题]"


def _split_id_from_filename(file_name: str) -> str:
    """Extract a compact split identifier from EPUB internal filename."""
    # e.g. "OEBPS/1717587639_split_003_split_001.html" → "split_003_split_001"
    base = Path(file_name).stem  # strip .html
    # Remove leading numeric prefix (timestamp)
    base = re.sub(r'^\d+_', '', base)
    return base


def extract_chapters_epub(
    epub_path: Path,
    min_content_chars: int = 80,
    max_chars_per_chapter: int = 15000,
) -> list[dict]:
    """
    Parse an EPUB file and return a list of chapter dicts:
    {
        "title": str,
        "text": str,
        "epub_split_ids": [str],  # source file identifiers
        "char_count": int,
        "flags": [str],           # "too_short", "too_long"
    }

    Two-phase approach:
    1. Build heading_map from ALL items (no length filter)
    2. Merge content items into chapters using heading_map for titles
    """
    book = epub.read_epub(str(epub_path))

    # Phase 1: extract all headings regardless of content length
    heading_map = _build_heading_map(book)

    # Phase 2: iterate spine items, merge into chapters
    chapters = []
    buf_text = ""
    buf_splits = []
    buf_title = None

    def flush_buffer():
        nonlocal buf_text, buf_splits, buf_title
        if not buf_text.strip():
            return
        flags = []
        char_count = len(buf_text)
        if char_count < 200:
            flags.append("too_short")
        if char_count > max_chars_per_chapter:
            flags.append("too_long")
        chapters.append({
            "title": buf_title or "[无标题]",
            "text": buf_text.strip(),
            "epub_split_ids": list(buf_splits),
            "char_count": char_count,
            "flags": flags,
        })
        buf_text = ""
        buf_splits = []
        buf_title = None

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        html = item.get_content().decode('utf-8', errors='ignore')
        text = _html_to_text(html)
        fname = item.file_name

        own_headings = heading_map.get(fname, {"zh": None, "en": None})
        sibling_headings = _sibling_heading(fname, heading_map)

        # Heading-only files (very short, just a chapter title marker):
        # use them to flush the previous chapter and set the title for the next one
        is_heading_only = (
            len(text) < min_content_chars and
            (own_headings["zh"] or own_headings["en"])
        )
        if is_heading_only:
            flush_buffer()
            buf_title = _make_title(own_headings, {}, text)
            continue

        # Skip truly empty or navigation files
        if len(text) < min_content_chars and not own_headings["zh"] and not own_headings["en"]:
            continue

        # Determine if this item starts a new chapter:
        # A new chapter begins if the buffer is empty OR if this item has a CJK heading
        # (meaning it's the start of a clearly new chapter)
        has_own_cjk_heading = bool(own_headings.get("zh"))
        if buf_text and has_own_cjk_heading:
            flush_buffer()

        # If no title set yet, derive from this item's headings
        if buf_title is None:
            buf_title = _make_title(own_headings, sibling_headings, text)

        split_id = _split_id_from_filename(fname)
        buf_splits.append(split_id)
        buf_text += "\n" + text

    flush_buffer()
    return chapters


def compute_book_id(epub_path: Path) -> str:
    """Stable 8-char book identifier from filename + file size."""
    stat = epub_path.stat()
    key = f"{epub_path.name}:{stat.st_size}"
    return hashlib.sha256(key.encode()).hexdigest()[:8]

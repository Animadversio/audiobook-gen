"""
Markdown parser for audiobook generation.

Splits a Markdown file into chapters based on # and ## headings.
"""

import re
import hashlib
from pathlib import Path


_CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def _clean_heading(text: str) -> str:
    """Strip leading #s and whitespace from a markdown heading line."""
    return re.sub(r'^#+\s*', '', text).strip()


def extract_chapters_markdown(
    md_path: Path,
    heading_level: int = 1,  # split on # (h1) by default; use 2 for ##
    min_content_chars: int = 200,
    max_chars_per_chapter: int = 15000,
) -> list[dict]:
    """
    Parse a Markdown file and return chapter dicts:
    {
        "title": str,
        "text": str,
        "epub_split_ids": [str],   # e.g. ["md:line_42"]
        "char_count": int,
        "flags": [str],
    }
    """
    text = md_path.read_text(encoding='utf-8', errors='ignore')
    lines = text.splitlines(keepends=True)

    heading_pattern = re.compile(r'^(#{1,%d})\s+(.+)' % heading_level)

    chapters = []
    buf_lines = []
    buf_title = None
    buf_start_line = 0

    def flush_buffer():
        nonlocal buf_lines, buf_title, buf_start_line
        content = ''.join(buf_lines).strip()
        if not content:
            return
        flags = []
        char_count = len(content)
        if char_count < min_content_chars:
            flags.append("too_short")
        if char_count > max_chars_per_chapter:
            flags.append("too_long")
        chapters.append({
            "title": buf_title or "[无标题]",
            "text": content,
            "epub_split_ids": [f"md:line_{buf_start_line}"],
            "char_count": char_count,
            "flags": flags,
        })
        buf_lines = []
        buf_title = None

    for i, line in enumerate(lines):
        m = heading_pattern.match(line)
        if m:
            level = len(m.group(1))
            if level <= heading_level:
                # New chapter starts here
                flush_buffer()
                buf_title = _clean_heading(line)
                buf_start_line = i + 1
                continue
        buf_lines.append(line)

    flush_buffer()
    return chapters


def compute_book_id(md_path: Path) -> str:
    """Stable 8-char book identifier from filename + file size."""
    stat = md_path.stat()
    key = f"{md_path.name}:{stat.st_size}"
    return hashlib.sha256(key.encode()).hexdigest()[:8]

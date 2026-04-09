"""
Audiobook Library — manages the local book collection.

Directory layout:
    {library_root}/
      library.json                  ← global index of all books
      {book_id}/                    ← one folder per book
        meta.json                   ← book metadata + progress summary
        segments.json               ← full segment plan (source of truth)
        dashboard.html              ← static status page snapshot
        audio/
          seg0001.mp3
          seg0001.json
          ...
        source/
          original.epub             ← copy of source file (optional)

Priority for library root:
  1. --library CLI argument
  2. AUDIOBOOK_LIBRARY environment variable
  3. ~/.local/share/audiobook-gen  (Linux/WSL default)
  4. ~/Library/audiobook-gen       (macOS default)
"""

import json
import os
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path


# ──────────────────────────────────────────────
# Root resolution
# ──────────────────────────────────────────────

def default_library_root() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "audiobook-gen"
    return Path.home() / ".local" / "share" / "audiobook-gen"


def resolve_library_root(cli_arg: str | None = None) -> Path:
    """
    Resolve library root from CLI arg > env var > platform default.
    Creates the directory if it doesn't exist.
    """
    if cli_arg:
        root = Path(cli_arg).expanduser().resolve()
    elif "AUDIOBOOK_LIBRARY" in os.environ:
        root = Path(os.environ["AUDIOBOOK_LIBRARY"]).expanduser().resolve()
    else:
        root = default_library_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


# ──────────────────────────────────────────────
# Per-book paths
# ──────────────────────────────────────────────

class BookPaths:
    """All filesystem paths for a single book."""

    def __init__(self, library_root: Path, book_id: str):
        self.book_dir = library_root / book_id
        self.audio_dir = self.book_dir / "audio"
        self.source_dir = self.book_dir / "source"
        self.meta_file = self.book_dir / "meta.json"
        self.segments_file = self.book_dir / "segments.json"
        self.dashboard_file = self.book_dir / "dashboard.html"

    def ensure_dirs(self):
        self.book_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(exist_ok=True)
        self.source_dir.mkdir(exist_ok=True)

    def seg_mp3(self, seg_index: int) -> Path:
        return self.audio_dir / f"seg{seg_index:04d}.mp3"

    def seg_json(self, seg_index: int) -> Path:
        return self.audio_dir / f"seg{seg_index:04d}.json"


# ──────────────────────────────────────────────
# Library index
# ──────────────────────────────────────────────

class Library:
    """Manages the library.json global index."""

    def __init__(self, root: Path):
        self.root = root
        self.index_file = root / "library.json"
        self._data = self._load()

    def _load(self) -> dict:
        if self.index_file.exists():
            return json.loads(self.index_file.read_text())
        return {"library_version": "1", "books": {}}

    def _save(self):
        self.index_file.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def book_paths(self, book_id: str) -> BookPaths:
        return BookPaths(self.root, book_id)

    def register_book(
        self,
        book_id: str,
        title: str,
        input_file: str,
        youtube_playlist: str | None = None,
    ) -> BookPaths:
        """Register a book in the index and create its directory structure."""
        paths = BookPaths(self.root, book_id)
        paths.ensure_dirs()

        entry = self._data["books"].get(book_id, {})
        entry.update({
            "title": title,
            "input_file": str(input_file),
            "registered_at": entry.get("registered_at") or datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        if youtube_playlist:
            entry["youtube_playlist"] = youtube_playlist
        self._data["books"][book_id] = entry
        self._save()
        return paths

    def update_book_progress(self, book_id: str, plan: dict):
        """Sync progress summary from segment plan into library index."""
        segs = plan.get("segments", [])
        total = len([s for s in segs if not s.get("skip")])
        done = len([s for s in segs if s.get("youtube_video_id")])
        generated = len([s for s in segs if s.get("generated_at")])
        total_sec = sum(s.get("audio_duration_sec") or 0 for s in segs)

        entry = self._data["books"].get(book_id, {})
        entry.update({
            "total_segments": total,
            "done_segments": done,
            "generated_segments": generated,
            "total_audio_sec": int(total_sec),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self._data["books"][book_id] = entry
        self._save()

    def copy_source(self, book_id: str, input_path: Path) -> Path:
        """Copy the original input file into source/ for archival."""
        paths = BookPaths(self.root, book_id)
        dest = paths.source_dir / input_path.name
        if not dest.exists():
            shutil.copy2(input_path, dest)
            print(f"Source archived: {dest}")
        return dest

    def list_books(self) -> list[dict]:
        """Return all registered books with their metadata."""
        books = []
        for book_id, entry in self._data["books"].items():
            books.append({"book_id": book_id, **entry})
        return sorted(books, key=lambda b: b.get("updated_at", ""), reverse=True)

    def find_book(self, query: str) -> dict | None:
        """Find a book by book_id prefix or title substring."""
        for book_id, entry in self._data["books"].items():
            if book_id.startswith(query) or query.lower() in entry.get("title", "").lower():
                return {"book_id": book_id, **entry}
        return None

    def print_library(self):
        """Print a human-readable library listing."""
        books = self.list_books()
        if not books:
            print("Library is empty.")
            return
        print(f"\n{'book_id':>10}  {'Done':>5}  {'Total':>5}  {'Duration':>9}  Title")
        print("─" * 70)
        for b in books:
            bid = b["book_id"]
            done = b.get("done_segments", "?")
            total = b.get("total_segments", "?")
            sec = b.get("total_audio_sec", 0)
            h, r = divmod(sec, 3600)
            m = r // 60
            dur = f"{h}h{m:02d}m" if h else f"{m}m"
            title = b.get("title", "?")[:40]
            print(f"{bid:>10}  {done!s:>5}  {total!s:>5}  {dur:>9}  {title}")
        print(f"\nLibrary root: {self.root}")

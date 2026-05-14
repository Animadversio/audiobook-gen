"""
YouTube uploader for audiobook segments.

Features:
- Deduplication via text_hash in video description
- Converts MP3 to MP4 (black background) for YouTube upload
- Stores upload metadata back into segment JSON
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Resolve ffmpeg — not on system PATH in conda envs on WSL
_FFMPEG = shutil.which("ffmpeg") or next(
    (p for p in [
        "/home/binxu/miniforge3/envs/research/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ] if Path(p).exists()), "ffmpeg"
)

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

def _real_config_dir() -> Path:
    # Path.home() uses $HOME which may be a session-scoped dir in container envs.
    # Use pwd to get the real home directory regardless of $HOME.
    import pwd, os
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return real_home / ".config"

DEFAULT_TOKEN_PATH = _real_config_dir() / "nanoclaw/youtube_token.json"
DEFAULT_SECRET_PATH = _real_config_dir() / "nanoclaw/youtube_client_secret.json"

HASH_MARKER = "文本哈希："  # marker used to find hash in description


def _load_credentials(token_path: Path = DEFAULT_TOKEN_PATH) -> Credentials:
    data = json.loads(token_path.read_text())
    creds = Credentials(
        token=data.get("token") or data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        data["token"] = creds.token
        token_path.write_text(json.dumps(data), encoding='utf-8')
    return creds


def _get_duration(path: Path) -> float:
    r = subprocess.run([_FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def _trim_trailing_silence(
    mp3_path: Path,
    threshold_db: float = -40.0,
    min_duration: float = 0.5,
) -> Path:
    """
    Trim trailing silence from an MP3. Returns path to trimmed temp file.
    If trimming saves < 0.1s, returns original path unchanged (no temp file).
    """
    import tempfile
    tmp = Path(tempfile.mktemp(suffix=".mp3"))
    silence_filter = (
        f"areverse,"
        f"silenceremove=start_periods=1:start_duration={min_duration}:start_threshold={threshold_db}dB,"
        f"areverse"
    )
    result = subprocess.run(
        [_FFMPEG, "-y", "-i", str(mp3_path),
         "-af", silence_filter,
         "-codec:a", "libmp3lame", "-b:a", "64k", str(tmp)],
        capture_output=True,
    )
    if result.returncode != 0 or not tmp.exists():
        return mp3_path  # fallback: use original

    dur_before = _get_duration(mp3_path)
    dur_after = _get_duration(tmp)
    trimmed = dur_before - dur_after
    if trimmed > 0.1:
        print(f"  [TRIM] Removed {trimmed:.2f}s trailing silence ({dur_before:.1f}s → {dur_after:.1f}s)")
        return tmp
    else:
        tmp.unlink(missing_ok=True)
        return mp3_path


def _mp3_to_mp4(mp3_path: Path) -> Path:
    """Wrap MP3 in a silent black-background MP4 for YouTube upload."""
    mp4_path = mp3_path.with_suffix(".mp4")
    subprocess.run([
        _FFMPEG, "-y",
        "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=1",
        "-i", str(mp3_path),
        "-shortest",
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        str(mp4_path),
    ], check=True, capture_output=True)
    return mp4_path


def _build_description(seg_meta: dict, book_title: str) -> str:
    text_preview = seg_meta.get("text", "")[:80].replace("\n", " ")
    split_ids = ", ".join(seg_meta.get("epub_split_ids", []))
    voice = seg_meta.get("voice_prompt") or "unconditional"
    steps = seg_meta.get("steps", "?")
    text_hash = seg_meta.get("text_hash", "?")
    idx = seg_meta.get("seg_index", "?")
    title = seg_meta.get("title", "")
    return (
        f"《{book_title}》有声书 — VoxCPM2 TTS\n"
        f"段落：{idx:02d} — {title}\n"
        f"来源：{split_ids}\n"
        f"摘要：{text_preview}…\n"
        f"{HASH_MARKER}{text_hash}\n"
        f"声音：{voice} | 步数：{steps}"
    )


class YouTubeUploader:
    def __init__(
        self,
        playlist_id: str,
        token_path: Path = DEFAULT_TOKEN_PATH,
    ):
        self.playlist_id = playlist_id
        creds = _load_credentials(token_path)
        self.youtube = build("youtube", "v3", credentials=creds)
        self._hash_cache: dict | None = None  # lazy-loaded existing hashes

    def _load_existing_hashes(self) -> dict:
        """Fetch all videos in playlist and index by text_hash → video_id."""
        if self._hash_cache is not None:
            return self._hash_cache

        hash_map = {}
        token = None
        while True:
            res = self.youtube.playlistItems().list(
                part="snippet", playlistId=self.playlist_id,
                maxResults=50, pageToken=token,
            ).execute()
            vids = [it["snippet"]["resourceId"]["videoId"] for it in res.get("items", [])]
            if vids:
                det = self.youtube.videos().list(
                    part="snippet", id=",".join(vids)
                ).execute()
                for v in det.get("items", []):
                    desc = v["snippet"].get("description", "")
                    for line in desc.splitlines():
                        if line.startswith(HASH_MARKER):
                            h = line[len(HASH_MARKER):].strip()
                            hash_map[h] = v["id"]
            token = res.get("nextPageToken")
            if not token:
                break

        self._hash_cache = hash_map
        return hash_map

    def _add_to_playlist(self, video_id: str):
        self.youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": self.playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()

    def upload(
        self,
        mp3_path: Path,
        title: str,
        seg_meta: dict,
        book_title: str,
    ) -> dict:
        """
        Upload a segment MP3 to YouTube.

        Returns:
            {"video_id": str, "skipped": bool}
        """
        text_hash = seg_meta.get("text_hash", "")
        existing = self._load_existing_hashes()

        # Deduplication check
        if text_hash and text_hash in existing:
            vid = existing[text_hash]
            print(f"  [SKIP] Already uploaded (hash match): {vid}")
            return {"video_id": vid, "skipped": True}

        description = _build_description(seg_meta, book_title)

        # Trim trailing silence before upload (saves to a temp file, non-destructive)
        mp3_to_encode = _trim_trailing_silence(mp3_path)

        # Convert MP3 → MP4
        mp4_path = _mp3_to_mp4(mp3_to_encode)
        try:
            media = MediaFileUpload(str(mp4_path), mimetype="video/mp4", resumable=True)
            body = {
                "snippet": {
                    "title": title[:100],
                    "description": description,
                    "categoryId": "22",  # People & Blogs
                },
                "status": {"privacyStatus": "unlisted"},
            }
            response = self.youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            ).execute()
            video_id = response["id"]
            self._add_to_playlist(video_id)

            # Update cache
            if text_hash:
                self._hash_cache[text_hash] = video_id

            print(f"  [UPLOAD] {title} → https://youtu.be/{video_id}")
            return {"video_id": video_id, "skipped": False}
        finally:
            mp4_path.unlink(missing_ok=True)
            if mp3_to_encode != mp3_path:
                mp3_to_encode.unlink(missing_ok=True)

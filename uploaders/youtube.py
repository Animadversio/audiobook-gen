"""
YouTube uploader for audiobook segments.

Features:
- Deduplication via text_hash in video description
- Converts MP3 to MP4 (black background) for YouTube upload
- Stores upload metadata back into segment JSON
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

DEFAULT_TOKEN_PATH = Path.home() / ".config/nanoclaw/youtube_token.json"
DEFAULT_SECRET_PATH = Path.home() / ".config/nanoclaw/youtube_client_secret.json"

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
        token_path.write_text(json.dumps(data))
    return creds


def _mp3_to_mp4(mp3_path: Path) -> Path:
    """Wrap MP3 in a silent black-background MP4 for YouTube upload."""
    mp4_path = mp3_path.with_suffix(".mp4")
    subprocess.run([
        "ffmpeg", "-y",
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

        # Convert MP3 → MP4
        mp4_path = _mp3_to_mp4(mp3_path)
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

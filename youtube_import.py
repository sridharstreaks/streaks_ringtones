"""Import individual YouTube / YouTube Music audio links when download is permitted.

No arbitrary URLs, playlists, authentication, cookies, or restriction bypassing.
Every downloaded file lives only in a temporary directory until returned to the app.
"""
from __future__ import annotations

import mimetypes
import re
import shutil
import subprocess
import tempfile
from importlib import metadata
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from audio_engine import AudioError, MAX_SOURCE_SECONDS, MAX_UPLOAD_BYTES

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


def normalize_youtube_url(value: str) -> str:
    """Only accept single YouTube video/track URLs (never playlists or other sites)."""
    value = value.strip()
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or host not in _HOSTS or
                parts.username or parts.password or parts.port is not None):
            raise ValueError("Unsupported URL")
        segments = parts.path.strip("/").split("/")
        if host.endswith("youtu.be"):
            video_id = segments[0] if len(segments) == 1 else ""
        elif parts.path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
        elif len(segments) == 2 and segments[0] in {"shorts", "live", "embed"}:
            video_id = segments[1]
        else:
            video_id = ""
        if not _VIDEO_ID.fullmatch(video_id):
            raise ValueError("Unsupported video ID")
    except (ValueError, AttributeError) as exc:
        raise AudioError("Paste an individual HTTPS YouTube or YouTube Music video/song link. Playlists and other sites are not supported.") from exc
    origin = "music.youtube.com" if host == "music.youtube.com" else "www.youtube.com"
    return f"https://{origin}/watch?v={video_id}"


def youtube_import_diagnostics() -> dict:
    """Safe dependency diagnostics (never exposes user URLs or cookies)."""
    details = {}
    for package in ("yt-dlp", "yt-dlp-ejs"):
        try:
            details[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            details[package] = "missing"
    executable = shutil.which("node")
    if executable:
        try:
            completed = subprocess.run([executable, "--version"], capture_output=True,
                                       text=True, timeout=5, check=True)
            details["node"] = completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            details["node"] = "installed but not working"
    else:
        details["node"] = "missing"
    return details


def classify_download_error(message: str) -> str:
    """Report actionable failure reason without leaking private extractor URLs."""
    value = message.lower()
    if any(x in value for x in ("sign in to confirm", "not a bot", "confirm you're not a bot",
                               "unusual traffic", "captcha")):
        return "YouTube requested account or automated-traffic verification. Shared cloud IPs may be blocked; this app does not bypass verification."
    if any(x in value for x in ("sign in", "login required", "private video", "members-only", "age-restricted")):
        return "This track requires access or sign-in that the app does not have."
    if "no supported javascript runtime" in value or "js runtime" in value:
        return "JavaScript challenge support is missing. Check that Node 22+ and yt-dlp-ejs installed."
    if "signature" in value or "nsig" in value or "ejs" in value:
        return "YouTube's JavaScript challenge could not be handled. Check yt-dlp, yt-dlp-ejs and Node versions."
    if "requested format is not available" in value or "only images are available" in value:
        return "YouTube did not offer a downloadable audio format to this server."
    if "403" in value or "forbidden" in value:
        return "YouTube denied the media request (HTTP 403). The cloud host or video may be restricted."
    if "429" in value or "too many requests" in value:
        return "YouTube is rate-limiting requests from this server (HTTP 429)."
    if "unavailable" in value or "removed" in value or "not available in your country" in value:
        return "YouTube reports that this video is unavailable to the server."
    if "timeout" in value or "timed out" in value or "connection" in value or "network" in value:
        return "The video request failed due to a network error or timeout."
    return "YouTube could not provide an audio stream. See the safe dependency diagnostics below."


def fetch_youtube_audio(url: str) -> dict:
    """Return media bytes and display metadata for a single permitted link.

    Raising AudioError is intentional: the UI can offer local upload when YouTube
    rejects traffic from a cloud IP. No cookies/proxies/account workarounds.
    """
    canonical = normalize_youtube_url(url)
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise AudioError("YouTube import is not installed. Add yt-dlp to requirements.txt.") from exc

    diagnostics = youtube_import_diagnostics()
    if diagnostics["node"] == "missing" or diagnostics["node"] == "installed but not working" or (diagnostics["node"].startswith("v") and int(diagnostics["node"][1:].split(".")[0]) < 22):
        raise AudioError("YouTube support is not configured: Node.js 22+ runtime was not found. Redeploy to install the updated dependencies.")
    if diagnostics["yt-dlp-ejs"] == "missing":
        raise AudioError("YouTube support is not configured: yt-dlp-ejs is missing. Redeploy to install the updated dependencies.")

    class DownloadLogger:
        """Capture yt-dlp warnings to classify failures while hiding internal URLs."""
        def __init__(self):
            self.messages = []
        def debug(self, msg):
            if "WARNING" in str(msg) or "ERROR" in str(msg):
                self.messages.append(str(msg)[-400:])
        def warning(self, msg):
            self.messages.append(str(msg)[-400:])
        def error(self, msg):
            self.messages.append(str(msg)[-400:])

    log = DownloadLogger()

    def accept(info, *, incomplete=False):
        duration = info.get("duration")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            return "Live streams cannot be imported. Use a finished video."
        if duration and float(duration) > MAX_SOURCE_SECONDS:
            return "This video is over 10 minutes. Please choose a shorter one."
        return None

    def limit_progress(status):
        size = int(status.get("downloaded_bytes") or 0)
        if size > MAX_UPLOAD_BYTES:
            raise AudioError("Download exceeds the 60 MB limit. Choose a shorter video.")

    with tempfile.TemporaryDirectory(prefix="ringrise-yt-") as temp:
        options = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(Path(temp) / "source.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            "logger": log,
            "js_runtimes": {"node": {}},
            "no_color": True,
            "socket_timeout": 15,
            "retries": 2,
            "fragment_retries": 2,
            "max_filesize": MAX_UPLOAD_BYTES,
            "match_filter": accept,
            "progress_hooks": [limit_progress],
            "cachedir": False,
            "ignoreerrors": False,
            "overwrites": True,
        }
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(canonical, download=True)
        except AudioError:
            raise
        except (DownloadError, OSError, ValueError) as exc:
            safe_reason = classify_download_error(" ".join(log.messages[-8:] + [str(exc)]))
            raise AudioError(safe_reason + " If access is restricted, use a recording you are permitted to upload.") from exc
        if not info:
            raise AudioError("No audio found for that YouTube link.")
        found = [p for p in Path(temp).glob("source.*")
                 if p.is_file() and p.suffix.lower() in {".m4a", ".mp3", ".webm", ".opus", ".ogg", ".mp4", ".aac"}]
        if len(found) != 1:
            raise AudioError("Could not find a compatible audio stream for this link.")
        path = found[0]
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise AudioError("Downloaded audio is too large (over 60 MB).")
        duration = float(info.get("duration") or 0)
        if duration > MAX_SOURCE_SECONDS:
            raise AudioError("The selected video is longer than 10 minutes.")
        return {
            "data": path.read_bytes(),
            "name": f"YouTube audio{path.suffix.lower()}",
            "title": str(info.get("title") or "YouTube audio")[:160],
            "mime": mimetypes.guess_type(path.name)[0] or "audio/mp4",
            "origin": "YouTube link",
            "url": canonical,
        }

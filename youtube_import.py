"""Bounded, unauthenticated YouTube audio import for content users may download.

The preflight classifies metadata failures separately from CDN/transfer failures.
Only a small set of independently advertised audio formats is attempted.
No login, DRM circumvention, cookies, browser sessions, proxies, or raw media URLs.
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
_FORMAT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,70}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
          "youtu.be", "www.youtu.be"}
_SUFFIXES = {".m4a", ".mp3", ".webm", ".opus", ".ogg", ".mp4", ".aac"}


class AudioImportError(AudioError):
    """Safe report; never contains yt-dlp media URLs, cookies, or full stderr."""

    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {}


def normalize_youtube_url(value: str) -> str:
    """Only allow specific HTTPS YouTube single-video/track links."""
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or host not in _HOSTS or
                parts.username or parts.password or parts.port is not None):
            raise ValueError("invalid URL")
        parts_path = parts.path.strip("/").split("/")
        if host.endswith("youtu.be"):
            video_id = parts_path[0] if len(parts_path) == 1 else ""
        elif parts.path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
        elif len(parts_path) == 2 and parts_path[0] in {"shorts", "live", "embed"}:
            video_id = parts_path[1]
        else:
            video_id = ""
        if not _VIDEO_ID.fullmatch(video_id):
            raise ValueError("invalid video id")
    except (ValueError, AttributeError) as exc:
        raise AudioImportError(
            "Paste an individual HTTPS YouTube or YouTube Music song/video link."
        ) from exc
    origin = "music.youtube.com" if host == "music.youtube.com" else "www.youtube.com"
    return f"https://{origin}/watch?v={video_id}"


def youtube_import_diagnostics() -> dict:
    """Check local extraction dependencies, not YouTube access."""
    details = {}
    for package in ("yt-dlp", "yt-dlp-ejs"):
        try:
            details[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            details[package] = "missing"
    executable = shutil.which("node")
    if executable:
        try:
            completed = subprocess.run(
                [executable, "--version"], capture_output=True,
                text=True, timeout=5, check=True
            )
            details["node"] = completed.stdout.strip()[:30]
        except (OSError, subprocess.SubprocessError):
            details["node"] = "installed but not working"
    else:
        details["node"] = "missing"
    return details


def classify_download_error(message: str) -> str:
    """Stable, privacy-preserving error category derived from yt-dlp output."""
    s = message.lower()
    if any(x in s for x in ("not a bot", "unusual traffic", "captcha", "confirm you're not")):
        return "YouTube requested automated-traffic verification from the server."
    if any(x in s for x in ("sign in", "login required", "private video", "members-only",
                            "age-restricted")):
        return "YouTube requires access or sign-in that RingRise does not have."
    if any(x in s for x in ("no supported javascript runtime", "js runtime", "signature",
                            "nsig", "ejs")):
        return "YouTube's JavaScript challenge could not be handled by the installed runtime."
    if "po token" in s or "po_token" in s or "proof of origin" in s:
        return "YouTube requires playback verification that this server has not received."
    if "403" in s or "forbidden" in s:
        return "HTTP 403: YouTube denied the request to this server."
    if "429" in s or "too many requests" in s:
        return "HTTP 429: YouTube is rate-limiting this server."
    if "requested format is not available" in s or "only images are available" in s:
        return "YouTube did not advertise a suitable downloadable audio format."
    if "unavailable" in s or "removed" in s or "not available in your country" in s:
        return "YouTube reports that this video is unavailable to this server."
    if any(x in s for x in ("timeout", "timed out", "connection", "network")):
        return "Network connection or timeout while contacting YouTube."
    return "YouTube extraction failed; the underlying cause is not identified."


class _SanitizedLogger:
    def __init__(self):
        self.messages: list[str] = []
    def debug(self, msg):
        if "WARNING" in str(msg) or "ERROR" in str(msg):
            self.messages.append(str(msg)[-400:])
    def warning(self, msg):
        self.messages.append(str(msg)[-400:])
    def error(self, msg):
        self.messages.append(str(msg)[-400:])
    def summary(self, error=None):
        return classify_download_error(" ".join(self.messages[-10:] + ([str(error)] if error else [])))


def _runtime_check():
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise AudioImportError("yt-dlp is missing from the deployment.") from exc
    versions = youtube_import_diagnostics()
    node = versions["node"]
    try:
        node_major = int(node.lstrip("v").split(".")[0])
    except ValueError:
        node_major = 0
    if node_major < 22 or versions["yt-dlp-ejs"] == "missing":
        raise AudioImportError(
            "YouTube extraction requires Node.js 22+ and yt-dlp-ejs. "
            "Reboot or redeploy with the updated requirements.txt.",
            {"stage": "dependencies", "versions": versions}
        )
    return YoutubeDL


def _ydl_options(log: _SanitizedLogger, **overrides):
    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "logger": log,
        "js_runtimes": {"node": {}},
        "no_color": True,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "max_filesize": MAX_UPLOAD_BYTES,
        "cachedir": False,
        "ignoreerrors": False,
    }
    options.update(overrides)
    return options


def select_audio_formats(formats: list[dict], limit: int = 2) -> list[dict]:
    """Prefer audio-only M4A then alternate WebM/Opus or other protocol."""
    candidates = []
    for f in formats:
        ident = str(f.get("format_id") or "")
        if (not _FORMAT_ID.fullmatch(ident) or f.get("has_drm") or
                f.get("acodec") in (None, "none") or f.get("vcodec") not in (None, "none")):
            continue
        ext = str(f.get("ext") or "").lower()
        proto = str(f.get("protocol") or "").lower()
        if ext not in {"m4a", "mp3", "webm", "opus", "mp4", "ogg", "aac"}:
            continue
        candidates.append({
            "format_id": ident, "ext": ext, "protocol": proto,
            "abr_kbps": round(float(f.get("abr") or f.get("tbr") or 0), 1),
        })
    candidates.sort(key=lambda f: (f["ext"] in ("m4a", "mp4"),
                                   f["abr_kbps"]), reverse=True)
    result = []
    distinctions = set()
    for f in candidates:
        kind = (f["ext"], f["protocol"])
        if kind in distinctions:
            continue
        distinctions.add(kind)
        result.append(f)
        if len(result) >= limit:
            break
    return result


def _check_info(info: dict, canonical: str) -> dict:
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise AudioImportError("Live streams are not supported.", {"stage": "metadata"})
    duration = float(info.get("duration") or 0)
    if duration > MAX_SOURCE_SECONDS:
        raise AudioImportError("Please select a video shorter than 10 minutes.",
                               {"stage": "metadata", "duration_seconds": duration})
    formats = select_audio_formats(info.get("formats") or [])
    if not formats:
        raise AudioImportError(
            "YouTube returned metadata, but no usable audio-only format.",
            {"stage": "formats", "duration_seconds": duration, "audio_formats": []}
        )
    return {"title": str(info.get("title") or "YouTube audio")[:160],
            "duration_seconds": round(duration, 1), "audio_formats": formats}


def probe_youtube_audio(url: str) -> dict:
    """Inspect accessible formats, without fetching the audio bytes."""
    canonical = normalize_youtube_url(url)
    YoutubeDL = _runtime_check()
    log = _SanitizedLogger()
    try:
        with YoutubeDL(_ydl_options(log, skip_download=True)) as ydl:
            info = ydl.extract_info(canonical, download=False)
    except Exception as exc:
        if isinstance(exc, AudioImportError):
            raise
        reason = log.summary(exc)
        raise AudioImportError(
            f"Metadata stage failed: {reason}",
            {"stage": "metadata", "reason": reason}
        ) from exc
    if not isinstance(info, dict):
        raise AudioImportError("YouTube returned no metadata.", {"stage": "metadata"})
    return _check_info(info, canonical)


def fetch_youtube_audio(url: str) -> dict:
    """Fetch permitted video audio, with one alternate-format retry when offered."""
    canonical = normalize_youtube_url(url)
    report: dict = {"stage": "metadata", "attempts": []}
    probe = probe_youtube_audio(canonical)
    report.update(probe)
    report["stage"] = "media"
    YoutubeDL = _runtime_check()
    candidates = probe["audio_formats"][:2]

    with tempfile.TemporaryDirectory(prefix="ringrise-yt-") as tmp:
        for index, candidate in enumerate(candidates):
            # Never carry over a partial file from a previous attempt.
            for leftover in Path(tmp).glob("source.*"):
                leftover.unlink(missing_ok=True)
            log = _SanitizedLogger()
            options = _ydl_options(
                log,
                format=candidate["format_id"],
                outtmpl=str(Path(tmp) / "source.%(ext)s"),
                overwrites=True,
            )
            try:
                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(canonical, download=True)
                matches = [
                    f for f in Path(tmp).glob("source.*")
                    if f.is_file() and f.suffix.lower() in _SUFFIXES
                ]
                if not matches:
                    raise AudioImportError("No media file was produced.")
                path = matches[0]
                if path.stat().st_size > MAX_UPLOAD_BYTES:
                    raise AudioImportError("Downloaded file exceeds 60 MB.")
                result = {
                    "data": path.read_bytes(),
                    "name": "YouTube audio" + path.suffix.lower(),
                    "title": str((info or {}).get("title") or probe["title"])[:160],
                    "mime": mimetypes.guess_type(path.name)[0] or "audio/mp4",
                    "origin": "YouTube link",
                    "url": canonical,
                    "import_report": {
                        "stage": "complete", "attempts": report["attempts"] +
                        [{"format": candidate, "result": "downloaded"}]
                    },
                }
                return result
            except AudioImportError:
                raise
            except Exception as exc:
                reason = log.summary(exc)
                report["attempts"].append({"format": candidate, "result": "failed",
                                            "reason": reason})
                # A retry is helpful only if the *actual* alternate format was
                # independently offered; authentication and rate limits must stop.
                if (index == len(candidates) - 1 or
                        not ("HTTP 403" in reason or "downloadable audio format" in reason)):
                    break

    if not report["attempts"]:
        raise AudioImportError("No audio formats could be fetched.", report)
    causes = "; ".join(a["reason"] for a in report["attempts"])
    report["stage"] = "media"
    raise AudioImportError(
        f"Audio-transfer stage failed after {len(report['attempts'])} "
        f"format attempt(s): {causes}. A cloud IP restriction or unavailable "
        "stream cannot be fixed by switching browsers. Upload a permitted file instead.",
        report
    )

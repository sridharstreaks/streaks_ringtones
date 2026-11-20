"""Offline ringtone mastering utilities. Uses only local ffmpeg and scipy.

Signal order matters: clean + compress -> loudness/peak matching -> fade curve.
There must be NO normalization after the fade or the opening will become loud again.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import os
import subprocess
import tempfile

import numpy as np
from scipy import signal
from scipy.interpolate import PchipInterpolator
from scipy.ndimage import gaussian_filter, gaussian_filter1d, maximum_filter1d
import pyloudnorm as pyln

SAMPLE_RATE = 44100
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_SOURCE_SECONDS = 10 * 60
MAX_RINGTONE_SECONDS = 60


class AudioError(ValueError):
    """Expected invalid media or unsupported audio operation."""


@dataclass(frozen=True)
class Settings:
    start_seconds: float = 0.0
    duration_seconds: float = 30.0
    opening_db: float = -36.0
    ten_second_db: float = -8.0
    full_at_seconds: float = 20.0
    target_lufs: float = -12.0
    cleanup: str = "Gentle"
    harshness: int = 35
    compression: str = "Balanced"
    tail_fade_seconds: float = 0.55
    speed: float = 1.0
    pitch_semitones: int = 0
    bass_db: float = 0.0
    presence_db: float = 0.0
    channel_mode: str = "Stereo"


def _run(command: list[str], *, payload: bytes | None = None, timeout: int = 90) -> bytes:
    try:
        p = subprocess.run(command, input=payload, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AudioError("FFmpeg / FFprobe is unavailable. Install ffmpeg (see packages.txt).") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("Audio processing timed out. Try a shorter clip.") from exc
    if p.returncode:
        detail = p.stderr.decode("utf-8", errors="replace")[-700:]
        raise AudioError(f"Audio decoder/encoder failed: {detail}")
    return p.stdout


def inspect_audio(path: Path) -> dict:
    import json
    result = _run(["ffprobe", "-v", "error", "-show_entries",
                   "format=duration:stream=codec_type,sample_rate,channels", "-of", "json", str(path)],
                  timeout=20)
    try:
        metadata = json.loads(result)
        streams = [s for s in metadata.get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise AudioError("The uploaded file does not contain an audio stream.")
        duration = float(metadata.get("format", {}).get("duration", 0))
        if not np.isfinite(duration) or duration < 2:
            raise AudioError("Could not detect audio duration.")
        if duration > MAX_SOURCE_SECONDS:
            raise AudioError("Please choose media under 10 minutes.")
        return {"duration": duration,
                "sample_rate": int(streams[0].get("sample_rate", 0)),
                "channels": int(streams[0].get("channels", 0))}
    except (ValueError, TypeError, KeyError) as exc:
        raise AudioError("Could not read audio information.") from exc


def write_upload(data: bytes, suffix: str) -> Path:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise AudioError("Choose a media file smaller than 60 MB.")
    if suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".webm", ".mov", ".mkv"}:
        raise AudioError("Unsupported file extension. Try audio files or MP4/WEBM/MOV/MKV video.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix.lower()) as tmp:
        tmp.write(data)
        return Path(tmp.name)


def decode_selection(path: Path, start: float, duration: float) -> np.ndarray:
    data = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss",
                 f"{start:.4f}", "-i", str(path), "-t", f"{duration:.4f}",
                 "-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE),
                 "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"], timeout=90)
    samples = np.frombuffer(data, dtype="<f4")
    if len(samples) < SAMPLE_RATE * 2 or len(samples) % 2:
        raise AudioError("The selected clip is empty or too short.")
    result = samples.reshape(-1, 2).copy()
    return np.nan_to_num(result, copy=False, nan=0., posinf=0., neginf=0.)


def _peaking_eq(x: np.ndarray, freq: float, db_gain: float, q: float = 0.8) -> np.ndarray:
    # RBJ cookbook peaking equalizer, stable for moderate boosts/cuts.
    A = 10 ** (db_gain / 40)
    w = 2 * np.pi * freq / SAMPLE_RATE
    alpha = np.sin(w) / (2 * q)
    b = np.array([1 + alpha * A, -2 * np.cos(w), 1 - alpha * A])
    a = np.array([1 + alpha / A, -2 * np.cos(w), 1 - alpha / A])
    return signal.lfilter(b / a[0], a / a[0], x, axis=0).astype(np.float32)


def _light_spectral_denoise(x: np.ndarray, strength: float) -> np.ndarray:
    # Conservative min-statistics suppression; works on steady hiss, not source separation.
    channels = []
    for ch in range(x.shape[1]):
        f, t, z = signal.stft(x[:, ch], SAMPLE_RATE, nperseg=2048,
                              noverlap=1536, boundary="zeros", padded=True)
        mag = np.abs(z)
        floor = np.percentile(mag, 12, axis=1, keepdims=True)
        mask = 1.0 - strength * floor / np.maximum(mag, 1e-8)
        mask = np.clip(mask, 0.48 if strength < 1.0 else 0.32, 1.0)
        mask = gaussian_filter(mask, sigma=(0.7, 1.1))
        _, out = signal.istft(z * mask, SAMPLE_RATE, nperseg=2048,
                              noverlap=1536, boundary=True)
        channels.append(out[:len(x)])
    return np.column_stack(channels).astype(np.float32)


def change_speed_pitch(x: np.ndarray, speed: float, semitones: int) -> np.ndarray:
    """Adjust tempo and pitch independently with FFmpeg."""
    if not (0.65 <= speed <= 1.5 and -4 <= semitones <= 4):
        raise AudioError("Speed or pitch control is outside its supported range.")
    if speed == 1.0 and semitones == 0:
        return x
    pitch_factor = 2.0 ** (semitones / 12.0)
    filters = []
    if semitones:
        filters.extend([f"asetrate={SAMPLE_RATE * pitch_factor:.4f}", f"aresample={SAMPLE_RATE}"])
    filters.append(f"atempo={speed / pitch_factor:.6f}")
    payload = np.asarray(x, dtype="<f4", order="C").tobytes()
    raw = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
                "-af", ",".join(filters), "-ar", str(SAMPLE_RATE), "-ac", "2",
                "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"],
               payload=payload, timeout=120)
    samples = np.frombuffer(raw, dtype="<f4")
    if samples.size < SAMPLE_RATE * 2 or samples.size % 2:
        raise AudioError("Speed/pitch processing produced an invalid audio clip.")
    return samples.reshape(-1, 2).copy()


def clean_audio(x: np.ndarray, setting: Settings) -> np.ndarray:
    if (setting.cleanup == "Off" and setting.harshness == 0 and
            setting.bass_db == 0 and setting.presence_db == 0 and
            setting.channel_mode == "Stereo"):
        return x.copy()
    y = x.copy()
    if setting.cleanup != "Off":
        highpass = signal.butter(2, 38, "highpass", fs=SAMPLE_RATE, output="sos")
        y = signal.sosfiltfilt(highpass, y, axis=0).astype(np.float32)
        if setting.cleanup == "Extra":
            y = _light_spectral_denoise(y, 1.15)
        elif setting.cleanup == "Gentle":
            y = _light_spectral_denoise(y, 0.55)
    if setting.harshness:
        y = _peaking_eq(y, 3700, db_gain=-3.3 * setting.harshness / 100, q=0.7)
    if setting.bass_db:
        y = _peaking_eq(y, 140, db_gain=setting.bass_db, q=0.75)
    if setting.presence_db:
        y = _peaking_eq(y, 2400, db_gain=setting.presence_db, q=0.75)
    if setting.channel_mode == "Mono":
        y = np.mean(y, axis=1, keepdims=True).repeat(2, axis=1).astype(np.float32)
    return y


def compress_audio(x: np.ndarray, mode: str) -> np.ndarray:
    if mode == "Off":
        return x
    threshold, ratio = {"Balanced": (-20.0, 2.1), "Strong": (-24.0, 2.9)}[mode]
    hop = 441  # 10 ms; stereo-linked RMS detector
    extra = (-len(x)) % hop
    padded = np.pad(x, ((0, extra), (0, 0)))
    rms = np.sqrt(np.mean(padded.reshape(-1, hop, 2) ** 2, axis=(1, 2)) + 1e-12)
    levels = 20 * np.log10(np.maximum(rms, 1e-9))
    reduction_db = np.maximum(levels - threshold, 0) * (1.0 - 1.0 / ratio)
    # Smooth control, rather than distort by directly following every waveform peak.
    reduction_db = gaussian_filter1d(reduction_db, sigma=14, mode="nearest")
    small_gain = 10 ** (-reduction_db / 20)
    times = np.minimum(np.arange(len(x)) / hop, len(small_gain) - 1)
    gain = np.interp(times, np.arange(len(small_gain)), small_gain)
    return (x * gain[:, None]).astype(np.float32)


def master_loudness(x: np.ndarray, target_lufs: float) -> tuple[np.ndarray, dict]:
    """Master ONLY the pre-fade music using a gentle stereo-linked peak limiter.

    The limiter is applied BEFORE the rising curve. Never normalize the final
    ringtone, otherwise a quiet opening gets amplified back up.
    """
    meter = pyln.Meter(SAMPLE_RATE)
    measured = float(meter.integrated_loudness(x))
    if not np.isfinite(measured):
        raise AudioError("Audio appears silent or has invalid loudness.")

    ceiling = 10 ** (-1.5 / 20)
    y = x.astype(np.float64, copy=True)
    # Iterate the gentle gain-and-limit stage to match the selected LUFS level.
    # Peak reduction is local, not a blanket reduction of the whole music track.
    # Protect against extreme user targets by limiting the number of passes.
    for _ in range(4):
        current = float(meter.integrated_loudness(y))
        delta = np.clip(target_lufs - current, -28, 14)
        if abs(delta) < 0.15:
            break
        y *= 10 ** (delta / 20)
        if delta > 0:
            peak_envelope = maximum_filter1d(np.max(np.abs(y), axis=1), size=881,
                                              mode="nearest")
            peak_envelope = gaussian_filter1d(peak_envelope, sigma=90, mode="nearest")
            attenuation = np.minimum(1.0, ceiling / np.maximum(peak_envelope, 1e-9))
            y *= attenuation[:, None]
        peak = float(np.max(np.abs(y)))
        if peak > ceiling:
            # Rare edge peaks: keep exact sample-peak safety margin.
            y *= ceiling / peak
    resulting = float(meter.integrated_loudness(y))
    return y.astype(np.float32), {"source_lufs": measured,
                                    "steady_lufs": resulting,
                                    "peak_dbfs": float(20 * np.log10(max(1e-9, np.max(np.abs(y)))))}


def gain_curve(n: int, settings: Settings, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Monotone, smooth fade curve. First 125ms fades in from literal silence."""
    if n <= 0:
        return np.empty(0, np.float32)
    dur = n / sample_rate
    full_at = min(max(11.0, settings.full_at_seconds), dur)
    opening = min(float(settings.opening_db), -1.0)
    ten = max(float(settings.ten_second_db), opening)
    if dur <= 10.001:  # Short notification clip: whole curve fits its duration.
        anchors_t = [0, dur]
        anchors_db = [opening, 0]
    else:
        two_db = opening + 0.22 * (ten - opening)
        anchors_t = [0, 2, 10, full_at]
        anchors_db = [opening, two_db, ten, 0]
    # If a user selects an unusually short clip, remove duplicate/end-overrun points.
    points = [(t, val) for t, val in zip(anchors_t, anchors_db) if t <= dur]
    if points[-1][0] < dur:
        points.append((dur, points[-1][1]))
    ts, levels = zip(*points)
    seconds = np.arange(n, dtype=np.float64) / sample_rate
    db = PchipInterpolator(ts, levels, extrapolate=True)(seconds)
    db = np.minimum(db, 0)
    gain = 10 ** (db / 20)
    first_fade = min(0.125, dur / 4)
    if first_fade > 0:
        k = min(n, max(2, int(first_fade * sample_rate)))
        gain[:k] *= np.sin(np.linspace(0, np.pi / 2, k)) ** 2
    return gain.astype(np.float32)


def apply_ringtone_curve(x: np.ndarray, settings: Settings) -> tuple[np.ndarray, np.ndarray]:
    curve = gain_curve(len(x), settings)
    y = x * curve[:, None]
    tail = max(0, min(settings.tail_fade_seconds, len(x) / SAMPLE_RATE / 2))
    if tail:
        k = min(len(y), int(tail * SAMPLE_RATE))
        if k > 1:
            y[-k:] *= np.cos(np.linspace(0, np.pi / 2, k))[:, None] ** 2
    return y.astype(np.float32), curve


def process_audio(x: np.ndarray, settings: Settings) -> tuple[np.ndarray, dict]:
    x = change_speed_pitch(x, settings.speed, settings.pitch_semitones)
    y = clean_audio(x, settings)
    y = compress_audio(y, settings.compression)
    y, stats = master_loudness(y, settings.target_lufs)
    y, curve = apply_ringtone_curve(y, settings)
    stats["final_peak_dbfs"] = round(20 * np.log10(max(float(np.max(np.abs(y))), 1e-9)), 2)
    stats["duration_seconds"] = round(len(y) / SAMPLE_RATE, 2)
    stats["gain_at_2s_db"] = curve_db_at(curve, 2)
    stats["gain_at_10s_db"] = curve_db_at(curve, 10)
    return y, stats


def curve_db_at(curve: np.ndarray, seconds: float) -> float:
    idx = min(len(curve) - 1, int(seconds * SAMPLE_RATE))
    return round(20 * np.log10(max(float(curve[max(0, idx)]), 1e-9)), 1)


def curve_preview(settings: Settings, duration: float) -> tuple[np.ndarray, np.ndarray]:
    # same curve implementation on a low-resolution time base for UI
    ts = np.linspace(0, duration, 401)
    full = min(max(11., settings.full_at_seconds), duration)
    opening = min(float(settings.opening_db), -1.0)
    ten = max(float(settings.ten_second_db), opening)
    if full <= 10.001:
        ats, dbs = [0, full], [opening, 0]
    else:
        ats, dbs = [0, 2, 10, full], [opening, opening + .22 * (ten - opening), ten, 0]
    if ats[-1] < duration:
        ats.append(duration)
        dbs.append(0)
    values = PchipInterpolator(ats, dbs)(ts)
    values = np.maximum(-80, np.minimum(0, values))
    values[0] = -80  # short anti-click fade is applied separately on actual audio
    return ts, values


def encode_audio(x: np.ndarray, kind: str) -> bytes:
    payload = np.asarray(x, dtype="<f4", order="C").tobytes()
    common = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
              "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0", "-vn"]
    if kind == "mp3":
        return _run(common + ["-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3", "pipe:1"],
                    payload=payload)
    if kind == "wav":
        return _run(common + ["-c:a", "pcm_s24le", "-f", "wav", "pipe:1"],
                    payload=payload)
    if kind == "m4a":
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ringtone.m4a"
            _run(common + ["-c:a", "aac", "-b:a", "256k", "-f", "ipod", str(path)],
                 payload=payload)
            return path.read_bytes()
    if kind == "m4r":
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ringtone.m4r"
            _run(common + ["-c:a", "aac", "-b:a", "256k", "-f", "ipod", str(path)],
                 payload=payload)
            return path.read_bytes()
    raise AudioError(f"Unknown format: {kind}")

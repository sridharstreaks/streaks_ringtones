"""RingRise Studio – free, locally-processing Streamlit ringtone editor."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from audio_engine import (
    AudioError, MAX_UPLOAD_BYTES, Settings, apply_ringtone_curve,
    curve_preview, decode_selection, encode_audio, inspect_audio,
    process_audio, write_upload,
)

st.set_page_config(page_title="RingRise Studio · Ringtone Maker", page_icon="🎧",
                   layout="wide", initial_sidebar_state="collapsed")

st.markdown('''<style>
[data-testid="stAppViewContainer"] {background: linear-gradient(135deg, #0d1526 0%, #0d1020 57%, #11182a 100%); color: #eff6ff;}
[data-testid="stHeader"] {background: transparent;}
[data-testid="stSidebar"] {background: #0d1526;}
.block-container {max-width: 1100px; padding-top: 2rem;}
.hero {background: radial-gradient(ellipse at 100% 0%, rgba(71,205,177,.16),transparent 60%),linear-gradient(120deg,#1b2b40,#151b32);border:1px solid #2e4056;border-radius:20px;padding:26px 30px;margin-bottom:20px;}
.hero h1 {color:#f1f9ff;font-size:2.55rem;margin:0 0 6px;font-weight:750;letter-spacing:-.045em;}
.hero .subtitle {color:#abbdd2;font-size:1.05rem;margin:0;line-height:1.65;}
.pill {display:inline-block;background:#143d3a;color:#89e5d1;padding:4px 11px;border-radius:30px;font-size:.77rem;font-weight:700;margin-bottom:10px;letter-spacing:.03em;}
.step {font-size:.9rem;letter-spacing:.025em;color:#96e4d5;font-weight:750;text-transform:uppercase;margin-top:5px;}
.hint {color:#a4b8cb;font-size:.88rem;line-height:1.55;margin-top:5px;}
[data-testid="stMetric"]{background:#18263a;border:1px solid #293e55;border-radius:14px;padding:14px;}
[data-testid="stFileUploader"]{border-radius:16px;}
div.stButton > button[kind="primary"]{background:#22b99b;border-color:#22b99b;color:#061a21;font-weight:750;border-radius:12px;}
.stDownloadButton button{width:100%;border-radius:12px;}
hr{border-color:#2d3b4f!important;}
</style>''', unsafe_allow_html=True)

st.markdown('''<section class="hero"><span class="pill">FREE · NO ACCOUNT · AUDIO STAYS IN THIS SESSION</span>
<h1>🎧 RingRise Studio</h1><p class="subtitle">A ringtone that starts gently and gets easier to hear.
Clean up your audio, separate the <b>opening softness</b> from the <b>overall loudness</b>, and download phone-ready files.</p></section>''', unsafe_allow_html=True)

uploaded = st.file_uploader("Upload your music or sound", type=["mp3", "wav", "m4a", "aac", "flac", "ogg", "opus"],
                            help="Maximum 25 MB. Works with uploaded audio only; no online music downloading.")
if not uploaded:
    st.info("↑ Upload an MP3, WAV, M4A, FLAC, or OGG file to begin. The original music isn't included in this app's code.")
    st.markdown("**Designed around your requests:** quiet first ring · audible by 10 seconds · strong finish · no sudden loudness jump.")
    st.stop()

file_bytes = uploaded.getvalue()
if len(file_bytes) > MAX_UPLOAD_BYTES:
    st.error("That file is over 25 MB. Please upload a smaller file.")
    st.stop()

fingerprint = hashlib.sha256(file_bytes).hexdigest()
if st.session_state.get("file_hash") != fingerprint:
    st.session_state["file_hash"] = fingerprint
    st.session_state.pop("result", None)

with st.spinner("Reading audio information…"):
    src_path = None
    try:
        src_path = write_upload(file_bytes, Path(uploaded.name).suffix)
        info = inspect_audio(src_path)
    except AudioError as exc:
        st.error(str(exc))
        st.stop()
    finally:
        if src_path:
            src_path.unlink(missing_ok=True)

left, right = st.columns([1, 1])
with left:
    st.markdown('<div class="step">01 · Your audio</div>', unsafe_allow_html=True)
    st.audio(file_bytes, format=uploaded.type or "audio/mpeg")
    st.caption(f"{uploaded.name} · {info['duration']:.1f}s · {info['sample_rate']:,} Hz · {len(file_bytes)/1024**2:.2f} MB")
with right:
    st.markdown('<div class="step">02 · Desired sound</div>', unsafe_allow_html=True)
    preset = st.selectbox("Rising-volume preset", ["Extra gentle start", "Balanced", "Quickly audible", "Custom"],
                          help="The start and the final loudness are controlled separately.")
    st.markdown('<p class="hint">Extra gentle starts almost silent and stays smooth; balanced is more audible early; quickly audible reaches high volume sooner.</p>', unsafe_allow_html=True)

presets = {
    "Extra gentle start": (-38, -9, 20),
    "Balanced": (-28, -6, 17),
    "Quickly audible": (-21, -3, 14),
    "Custom": (-36, -8, 20),
}
p_open, p_ten, p_full = presets[preset]

with st.form("ringtone_settings", border=True):
    st.markdown('<div class="step">03 · Build your ringtone</div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        max_start = max(0.0, info["duration"] - 2.0)
        start = st.slider("Start from (seconds)", min_value=0.0, max_value=float(round(max_start, 1)),
                          value=0.0, step=0.5, disabled=max_start < .5)
        max_clip = min(60, max(2, int(info["duration"] - start)))
        clip = st.slider("Ringtone length (seconds)", min_value=min(2, max_clip), max_value=max_clip,
                         value=min(30, max_clip), step=1)
        cleanup = st.select_slider("Audio cleanup", options=["Off", "Gentle", "Extra"], value="Gentle",
                                    help="Gentle is safest for music. Extra adds stronger denoising, which may soften some detail.")
        harshness = st.slider("Reduce harsh high frequencies", 0, 100, 35, 5)
    with c2:
        opening = st.slider("Opening level · quieter ← → louder (dB)", -48, -12, p_open, 1,
                            help="This changes only the opening. -40 dB is much quieter than -20 dB.")
        at_ten = st.slider("Level at 10 seconds (dB below full)", -20, -1, p_ten, 1,
                           help="Controls how easy it is to hear the ringtone around 10 seconds.")
        full_at = st.slider("Reach full volume by (seconds)", 12, 28, p_full, 1)
        loudness = st.slider("Full-volume loudness target (LUFS)", -18, -9, -12, 1,
                             help="Applied BEFORE the fade-in. Can be limited by peaks to avoid distortion.")
        compression = st.selectbox("Sound density", ["Balanced", "Off", "Strong"], index=0,
                                   help="Balanced evens out spikes so the loud section is audible without harsh peaks.")
    do_process = st.form_submit_button("✨ Process ringtone", type="primary", use_container_width=True)

settings = Settings(start_seconds=start, duration_seconds=clip, opening_db=opening,
                    ten_second_db=at_ten, full_at_seconds=full_at,
                    target_lufs=loudness, cleanup=cleanup, harshness=harshness,
                    compression=compression)

if opening > at_ten:
    st.warning("Opening level is higher than the 10-second level. For a steadily rising ringtone, lower the opening level.")

st.markdown("#### Preview of your volume curve")
times, db_curve = curve_preview(settings, clip)
chart = pd.DataFrame({"Seconds": times, "Relative volume (dB)": db_curve}).set_index("Seconds")
st.line_chart(chart, color="#30c6a8", height=205)
st.caption("The curve is applied **after** cleanup and loudness mastering; raising the overall loudness won't undo the gentle opening. The first 125 ms also fades in from silence to avoid a click.")

if do_process:
    if opening > at_ten:
        st.error("Please make the opening level quieter than the 10-second level.")
    elif start + clip > info["duration"] + 0.51:
        st.error("Please reduce the clip length: it extends beyond the uploaded audio.")
    else:
        path = None
        try:
            with st.spinner("Cleaning, mastering, applying your volume curve, and exporting audio…"):
                path = write_upload(file_bytes, Path(uploaded.name).suffix)
                audio = decode_selection(path, start, clip)
                result, stats = process_audio(audio, settings)
                # Create all three phone-ready files. WAV is also the preview file.
                wav = encode_audio(result, "wav")
                mp3 = encode_audio(result, "mp3")
                m4r = encode_audio(result, "m4r")
                st.session_state["result"] = {
                    "fingerprint": fingerprint, "settings": settings,
                    "stats": stats, "wav": wav, "mp3": mp3, "m4r": m4r,
                }
        except (AudioError, ValueError, OSError) as exc:
            st.error(f"Couldn't process the clip: {exc}")
        finally:
            if path:
                path.unlink(missing_ok=True)

result = st.session_state.get("result")
if result and result["fingerprint"] == fingerprint:
    st.divider()
    st.markdown('<div class="step">04 · Your finished ringtone</div>', unsafe_allow_html=True)
    if result["settings"] != settings:
        st.info("Showing the last processed version. Click **Process ringtone** to apply your latest adjustments.")
    st.audio(result["mp3"], format="audio/mpeg")
    stats = result["stats"]
    m1, m2, m3 = st.columns(3)
    m1.metric("Opening vs. full", f"{result['settings'].opening_db:.0f} dB")
    m2.metric("At 10 seconds", f"{stats['gain_at_10s_db']:.1f} dB")
    m3.metric("Final peak", f"{stats['final_peak_dbfs']:.1f} dBFS")
    st.caption(f"Steady-section loudness: {stats['steady_lufs']:.1f} LUFS before the rising-volume curve. Loudness may be below target when peak safety limits it. Output is not normalized after the fade.")
    a, b, c = st.columns(3)
    with a:
        st.download_button("⬇ Android · MP3", result["mp3"], "RingRise_Ringtone_Android.mp3",
                           mime="audio/mpeg", use_container_width=True)
    with b:
        st.download_button("⬇ iPhone · M4R", result["m4r"], "RingRise_Ringtone_iPhone.m4r",
                           mime="audio/mp4", use_container_width=True)
    with c:
        st.download_button("⬇ Master · WAV", result["wav"], "RingRise_Ringtone_Master.wav",
                           mime="audio/wav", use_container_width=True)
    st.success("Ready! Preview it with your phone's built-in speaker and ringtone volume setting.")

st.divider()
st.caption("Privacy: audio is processed by the server for this session; uploads aren't saved as permanent files. Temporary input files are deleted after processing. Don't upload private recordings to an app you don't trust. Audio processing can't restore details already lost by a low-bitrate original. iPhone may require GarageBand or another ringtone-import method to install an M4R file.")

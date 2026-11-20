# 🎧 RingRise Studio

A free-to-deploy, browser-based ringtone editor made with Streamlit and FFmpeg.

Turn your own music into a **gentle-start, loud-finish** ringtone. The volume curve is applied **after loudness mastering**, so making the final ringtone louder **doesn't accidentally boost its opening**.

## Features

- Paste an individual YouTube / YouTube Music link to fetch an accessible audio stream, or upload MP3, WAV, M4A, AAC, FLAC, OGG, OPUS, MP4, WEBM, MOV, or MKV (max 60 MB; source under 10 min).
- Select the start and duration (up to 60 seconds).
- Adjust playback speed (0.65×–1.5×) independently from pitch (±4 semitones).
- Adjust bass, clarity/presence EQ, stereo/mono and end fade.
- Download M4A audio as well as MP3, M4R and WAV.
- Independently adjust opening level, level at 10 seconds, and time to reach full volume.
- Preview the volume curve and listen to the processed ringtone.
- Optional rumble cleanup, gentle denoising, harshness EQ, and compression.
- Loudness target applied **before** the gradual fade, with peak ceiling for safety.
- Download Android MP3 (320 kbps), iPhone M4R (256 kbps AAC), or 24-bit WAV.
- No external API, account, paid service, or database needed.

## Deploy FREE on Streamlit Community Cloud

1. Create a GitHub repository (e.g. `ringrise-studio`).
2. Upload **all the contents of this project folder** to the repo, including `app.py`, `audio_engine.py`, `requirements.txt`, `packages.txt`, and `.streamlit/config.toml`. Do **not** upload personal or copyrighted audio files to the public repository.
3. Sign into https://share.streamlit.io/ with GitHub, then choose **Create app** / **Deploy an app**.
4. Select your repository, choose branch `main`, and set **Main file path** to `app.py`.
5. Click **Deploy**. Community Cloud installs Python dependencies from `requirements.txt` and system FFmpeg from `packages.txt`.
6. Open your deployed `.streamlit.app` URL and upload an audio file.

Free hosting policy, resource limits, and availability can change; see https://docs.streamlit.io/deploy/streamlit-community-cloud.

## Run locally

Install Python 3.11+ and FFmpeg (`ffmpeg` and `ffprobe` on your PATH), then:

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Technical design

Audio is decoded to stereo 44.1 kHz with FFmpeg. A conservative cleanup stage removes sub-bass rumble, optionally suppresses steady noise, and mildly attenuates harsh frequencies. Optional stereo-linked compression evens out spikes. Loudness is raised toward the requested LUFS target, with a -1.5 dBFS sample-peak ceiling; dynamic peaks can prevent hitting the exact LUFS goal. Only **after** that does the app apply a monotonic PCHIP volume curve, a tiny anti-click fade, and a tail fade. **It never normalizes after the curve**.

YouTube import works only on individual accessible videos/songs for which you have permission to download and edit under applicable terms. It uses yt-dlp; downloads can fail when YouTube restricts cloud hosting IP addresses, sign-in is required, or platform access changes. No authentication, DRM bypass, cookies or proxies are provided. No claim is made to restore audio quality lost in a poor recording. The application's server processes user-uploaded audio; temporary files are removed, but files exist in server RAM while the user's session is active. Do not treat public deployments as end-to-end encrypted or as suitable for highly confidential audio.

### Important iPhone note

A `.m4r` file is compatible with the iPhone ringtone format, but installing custom ringtones may require GarageBand, Finder, Apple Music, or another supported import route depending on device/software.

## License

MIT (code only). No sample copyrighted music is included.

## YouTube Music troubleshooting

Paste a single video or song URL, for example `https://music.youtube.com/watch?v=tScimk13hbE`.
Extra tracking parameters such as `&drli=1` are removed automatically. The app
ships `yt-dlp[default]` (including the compatible `yt-dlp-ejs` scripts) and
a supported Node.js runtime from `nodejs-wheel`, so current YouTube JavaScript
challenge extraction is available where the hosting environment permits it.

If a download fails, expand **YouTube import diagnostics** to check the installed
versions. YouTube can still require sign-in or deny requests from cloud hosting,
and these restrictions are not bypassed. No cookies or user credentials are taken.
If it fails on Streamlit Community Cloud, test the same permitted link using
your locally hosted copy of the app and inspect the specific error category.

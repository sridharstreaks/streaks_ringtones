"""Offline unit tests for YouTube link parsing and format/failure classification.

Run: python -m unittest test_youtube_import.py
No YouTube network requests are made.
"""
import unittest
from youtube_import import (normalize_youtube_url, select_audio_formats,
                            classify_download_error, AudioImportError)


class LinkTests(unittest.TestCase):
    def test_music_link_strips_tracking(self):
        self.assertEqual(
            normalize_youtube_url("https://music.youtube.com/watch?v=tScimk13hbE&drli=1"),
            "https://music.youtube.com/watch?v=tScimk13hbE"
        )

    def test_short_link(self):
        self.assertEqual(
            normalize_youtube_url("https://youtu.be/tScimk13hbE?t=5"),
            "https://www.youtube.com/watch?v=tScimk13hbE"
        )

    def test_reject_invalid_links(self):
        for url in ("http://youtube.com/watch?v=tScimk13hbE",
                    "https://youtube.com.evil.example/watch?v=tScimk13hbE",
                    "https://www.youtube.com/playlist?list=123",
                    "https://example.com/", ""):
            with self.subTest(url=url), self.assertRaises(AudioImportError):
                normalize_youtube_url(url)


class FormatTests(unittest.TestCase):
    def test_formats_choose_distinct_audio_streams(self):
        formats = [
            {"format_id": "137", "ext": "mp4", "acodec": "none",
             "vcodec": "avc1", "tbr": 1500},
            {"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2",
             "vcodec": "none", "abr": 128, "protocol": "https"},
            {"format_id": "251", "ext": "webm", "acodec": "opus",
             "vcodec": "none", "abr": 160, "protocol": "https"},
            {"format_id": "250", "ext": "webm", "acodec": "opus",
             "vcodec": "none", "abr": 80, "protocol": "https"},
        ]
        options = select_audio_formats(formats)
        self.assertEqual([x["format_id"] for x in options], ["140", "251"])

    def test_reject_drm(self):
        self.assertEqual(select_audio_formats([
            {"format_id": "99", "ext": "m4a", "acodec": "aac", "vcodec": "none",
             "has_drm": True}]), [])

    def test_error_categories(self):
        self.assertIn("HTTP 403", classify_download_error("HTTP Error 403: Forbidden"))
        self.assertIn("verification", classify_download_error(
            "Sign in to confirm you're not a bot"))
        self.assertIn("rate-limiting", classify_download_error("HTTP Error 429"))
        self.assertIn("playback verification",
                      classify_download_error("PO Token required"))


if __name__ == "__main__":
    unittest.main()

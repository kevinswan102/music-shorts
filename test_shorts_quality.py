import os
import tempfile
import unittest
from unittest import mock

import music_source as ms
import youtube_uploader as yu


class TitleVariesPerShort(unittest.TestCase):
    def test_short_num_changes_title_format(self):
        up = yu.YouTubeUploader.__new__(yu.YouTubeUploader)
        up.uploads_today = 0  # fresh uploader per short, as in generate_short
        titles = {up._generate_title("Song", "Star Drift", "Electronic", upload_num=n).split(" #")[0]
                  for n in (1, 2)}
        self.assertEqual(len(titles), 2)


class RecycleWhenCatalogEmpty(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(); os.close(fd)
        with open(self.path, "w") as f:
            f.write("a\nb\nc\na\n")  # a was re-posted; b is now oldest

    def tearDown(self):
        os.unlink(self.path)

    def _pick(self, **env):
        videos = [{"id": x} for x in "abc"]
        with mock.patch.object(ms, "ARCHIVE_FILE", self.path), \
             mock.patch.object(ms, "_get_release_assets", return_value={f"{x}.mp3": 1 for x in "abc"}), \
             mock.patch.dict(os.environ, env):
            return ms.pick_next_track(videos)

    def test_recycles_oldest_posted(self):
        self.assertEqual(self._pick()["id"], "b")

    def test_recycle_can_be_disabled(self):
        self.assertIsNone(self._pick(RECYCLE_TRACKS="false"))


class NoSaleClaims(unittest.TestCase):
    def test_promo_copy_has_no_sale(self):
        src = open(os.path.join(os.path.dirname(__file__), "generate_short.py")).read().lower()
        self.assertNotIn("buy 2 get 1", src)


if __name__ == "__main__":
    unittest.main()


class VibeOverlay(unittest.TestCase):
    def test_daily_shorts_get_different_short_prompts(self):
        import generate_short as gs
        a, b = gs._vibe_overlay(short_num=1), gs._vibe_overlay(short_num=2)
        self.assertNotEqual(a, b)
        for p in gs._VIBE_PROMPTS:
            self.assertLessEqual(len(p), 2)
            self.assertTrue(all(len(line) <= gs._OVERLAY_MAX_CHARS for line in p), p)

    def test_no_onscreen_ad_in_default_rotation(self):
        import generate_short as gs
        with mock.patch.dict(os.environ, {"OVERLAY_MODE_ROTATION": ""}):
            modes = {gs._pick_overlay_mode(short_num=n) for n in range(1, 11)}
        self.assertNotIn("beatstore", modes)
        self.assertIn("vibe", modes)

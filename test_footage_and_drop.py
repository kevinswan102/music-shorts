import os
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np

import beat_analyzer as ba
import footage_fetcher as ff


class StartsOnTheDrop(unittest.TestCase):
    def test_window_starts_at_loud_section_not_quiet_intro(self):
        sr, hop = 22050, 512
        fd = hop / sr
        n = int(60 / fd)
        env = np.full(n, 0.2)
        drop = int(20 / fd)
        env[drop:] = 1.0          # quiet first 20s, then the drop
        env[:int(2 / fd)] = 1.2   # a short loud blip at 0s should not win
        start, _ = ba._find_best_window(env, sr, 60.0, target_duration=30.0)
        self.assertGreaterEqual(start, 19.0)
        self.assertLessEqual(start, 21.0)

    def test_top_windows_still_non_overlapping(self):
        sr, hop = 22050, 512
        env = np.random.default_rng(1).random(int(120 / (hop / sr)))
        wins = ba._find_top_windows(env, sr, 120.0, target_duration=30.0, n=3)
        wins.sort()
        for (s1, e1), (s2, _) in zip(wins, wins[1:]):
            self.assertLessEqual(e1, s2 + 1e-6)


def _make_clip(path, color):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c={color}:s=108x192:d=2", path], check=True)


class BrightFootage(unittest.TestCase):
    def test_brightness_measure(self):
        with tempfile.TemporaryDirectory() as d:
            light, dark = os.path.join(d, "l.mp4"), os.path.join(d, "d.mp4")
            _make_clip(light, "white"); _make_clip(dark, "black")
            self.assertGreater(ff._clip_brightness(light), 200)
            self.assertLess(ff._clip_brightness(dark), 30)

    def test_dark_clips_only_fill_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i, c in enumerate(["black", "white", "black", "white"]):
                p = os.path.join(d, f"c{i}.mp4"); _make_clip(p, c); paths.append(p)
            vids = [{"id": i} for i in range(4)]
            with mock.patch.object(ff, "_pexels_search", return_value=vids), \
                 mock.patch.object(ff, "MAX_CLIPS_PER_QUERY", 10), \
                 mock.patch.object(ff, "_pexels_download", side_effect=lambda v, output_dir: paths[v["id"]]), \
                 mock.patch.object(ff.random, "shuffle", lambda x: None), \
                 mock.patch.object(ff.time, "sleep", lambda s: None):
                got = ff._fetch_pexels("default", 3, d)
            self.assertEqual(len(got), 3)
            self.assertEqual(got[:2], [paths[1], paths[3]])  # bright first
            self.assertIn(got[2], (paths[0], paths[2]))       # dark only as filler

    def test_bright_people_queries_go_first(self):
        seen = []
        with mock.patch.object(ff, "_pexels_search", side_effect=lambda q, **k: seen.append(q) or []), \
             mock.patch.object(ff.time, "sleep", lambda s: None):
            ff._fetch_pexels("default", 2, tempfile.gettempdir())
        self.assertIn(seen[0], ff.PEXELS_BRIGHT_PEOPLE_KEYWORDS)


class PostingTimes(unittest.TestCase):
    def test_two_daily_shorts_post_afternoon_and_evening_us(self):
        import generate_short as gs
        with mock.patch.dict(os.environ, {"SCHEDULE_UPLOADS": "true"}):
            times = gs._get_publish_schedule(2)
        hours = sorted(int(t[11:13]) for t in times)
        self.assertEqual(hours, [1, 21])


if __name__ == "__main__":
    unittest.main()


class NoChildQueries(unittest.TestCase):
    def test_bright_queries_avoid_terms_that_return_kids(self):
        for q in ff.PEXELS_BRIGHT_PEOPLE_KEYWORDS:
            for bad in ("girl", "boy", "kid", "child"):
                self.assertNotIn(bad, q.lower(), q)


class Variety(unittest.TestCase):
    def test_people_and_eye_catching_queries_alternate(self):
        seen = []
        with mock.patch.object(ff, "_pexels_search", side_effect=lambda q, **k: seen.append(q) or []), \
             mock.patch.object(ff.time, "sleep", lambda s: None):
            ff._fetch_pexels("default", 2, tempfile.gettempdir())
        self.assertIn(seen[0], ff.PEXELS_BRIGHT_PEOPLE_KEYWORDS)
        self.assertIn(seen[1], ff.PEXELS_EYE_CATCHING_KEYWORDS)

    def test_one_query_cannot_fill_the_short(self):
        n = [0]
        def search(q, **k):
            n[0] += 1
            return [{"id": f"{q}-{i}"} for i in range(10)]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "w.mp4"); _make_clip(p, "white")
            with mock.patch.object(ff, "_pexels_search", side_effect=search), \
                 mock.patch.object(ff, "_pexels_download", return_value=p), \
                 mock.patch.object(ff, "_clip_brightness", return_value=200.0), \
                 mock.patch.object(ff.time, "sleep", lambda s: None):
                got = ff._fetch_pexels("default", 6, d)
        self.assertEqual(len(got), 6)
        self.assertGreaterEqual(n[0], 3)

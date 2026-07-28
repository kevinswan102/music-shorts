"""Tests for the cache-only overlay pool (2026-07-28).

The pool exists because live Reddit fetches return nothing from CI most days.
These tests pin the properties that made it worth building: renders never
touch the network, the pool cannot starve, and a 4x/day channel does not
repeat the same line back to back.
"""

import json
import os
import random
import sys
import tempfile
import types
import unittest

os.environ.setdefault("OVERLAY_POOL_PATH", "/nonexistent/never-used.json")

# generate_short pulls in the audio stack transitively. librosa and yt_dlp are
# heavy CI-only installs and none of the overlay code touches them, so stub
# them rather than making text tests depend on an audio toolchain.
for _heavy in ("librosa", "yt_dlp"):
    sys.modules.setdefault(_heavy, types.ModuleType(_heavy))

import generate_short
import overlay_content


class PoolTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = overlay_content.CACHE_PATH
        overlay_content.CACHE_PATH = os.path.join(self._tmp.name, "overlay_pool.json")

    def tearDown(self):
        overlay_content.CACHE_PATH = self._orig
        self._tmp.cleanup()


class SeedPoolTests(PoolTestBase):
    def test_pick_works_with_no_cache_file(self):
        """First ever run has no cache — seeds must carry it."""
        entry = overlay_content.pick()
        self.assertTrue(entry["text"])

    def test_both_topics_are_seeded(self):
        pool = overlay_content.load_pool()
        topics = {e["topic"] for e in pool["entries"]}
        self.assertEqual(topics, {"music", "general"})

    def test_music_topic_returns_music(self):
        self.assertEqual(overlay_content.pick(topic="music")["topic"], "music")

    def test_general_topic_returns_general(self):
        self.assertEqual(overlay_content.pick(topic="general")["topic"], "general")

    def test_every_seed_line_fits_the_overlay(self):
        """A line too long to wrap onto the screen is silently dropped later."""
        for entry in overlay_content.load_pool()["entries"]:
            budget = generate_short._OVERLAY_MAX_LINES - (1 if entry["label"] else 0)
            self.assertTrue(
                generate_short._fit_overlay_text(entry["text"], max_lines=budget),
                f"does not fit: {entry['text']}",
            )

    def test_seeds_are_not_written_to_the_cache_file(self):
        overlay_content.pick()
        with open(overlay_content.CACHE_PATH) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["entries"], [])

    def test_corrupt_cache_falls_back_to_seeds(self):
        with open(overlay_content.CACHE_PATH, "w") as fh:
            fh.write("{not json")
        self.assertTrue(overlay_content.pick()["text"])


class RecentMemoryTests(PoolTestBase):
    def test_consecutive_picks_do_not_repeat(self):
        seen = {overlay_content.pick(topic="music")["text"] for _ in range(8)}
        self.assertEqual(len(seen), 8)

    def test_recent_memory_is_capped(self):
        for _ in range(overlay_content.RECENT_MEMORY + 15):
            overlay_content.pick()
        with open(overlay_content.CACHE_PATH) as fh:
            saved = json.load(fh)
        self.assertLessEqual(len(saved["recent"]), overlay_content.RECENT_MEMORY)

    def test_exhausted_topic_still_returns_a_line(self):
        """Never return nothing just because everything was used recently."""
        for _ in range(200):
            self.assertTrue(overlay_content.pick(topic="music")["text"])


class AddLinesTests(PoolTestBase):
    def test_added_line_becomes_pickable(self):
        overlay_content.add_lines(
            ["Bounce your stems before the session breaks."], topic="music")
        texts = {e["text"] for e in overlay_content.load_pool()["entries"]}
        self.assertIn("Bounce your stems before the session breaks.", texts)

    def test_duplicate_lines_are_not_stored_twice(self):
        line = "Bounce your stems before the session breaks."
        self.assertEqual(overlay_content.add_lines([line], topic="music"), 1)
        self.assertEqual(overlay_content.add_lines([line], topic="music"), 0)

    def test_dedup_ignores_case_and_spacing(self):
        overlay_content.add_lines(["Mix at low volume every single time."])
        self.assertEqual(
            overlay_content.add_lines(["mix  at LOW volume  every single time."]), 0)

    def test_too_short_and_too_long_lines_are_rejected(self):
        self.assertEqual(overlay_content.add_lines(["nope"]), 0)
        self.assertEqual(overlay_content.add_lines(["x" * 200]), 0)

    def test_pool_size_is_bounded(self):
        overlay_content.add_lines(
            [f"Producer tip number {i} about finishing tracks faster." for i in range(600)],
            topic="music")
        with open(overlay_content.CACHE_PATH) as fh:
            saved = json.load(fh)
        self.assertLessEqual(len(saved["entries"]), overlay_content.MAX_POOL_ENTRIES)


class MixTests(PoolTestBase):
    def test_untargeted_picks_mix_both_topics(self):
        """The user asked for a mix, not one topic or the other."""
        rng = random.Random(7)
        topics = {overlay_content.pick(rng=rng)["topic"] for _ in range(30)}
        self.assertEqual(topics, {"music", "general"})


class RenderPathTests(PoolTestBase):
    """The render path must not make network calls for overlay text."""

    def test_overlay_modes_never_hit_the_network(self):

        def _boom(*a, **k):
            raise AssertionError("render path made a network call")

        # Patch requests itself, not generate_short.requests — the old fetch
        # helpers do a local `import requests` inside the function body.
        import requests
        orig_get = requests.get
        requests.get = _boom
        try:
            for mode in ("protip", "fact", "reddit"):
                lines = {
                    "protip": generate_short._pro_tip_overlay,
                    "fact": generate_short._fact_overlay,
                    "reddit": generate_short._random_reddit_overlay,
                }[mode]()
                self.assertTrue(lines, f"{mode} produced no overlay")
        finally:
            requests.get = orig_get

    def test_reddit_is_back_in_the_default_rotation(self):
        os.environ.pop("OVERLAY_MODE_ROTATION", None)
        modes = {generate_short._pick_overlay_mode(short_num=n) for n in range(1, 12)}
        self.assertIn("reddit", modes)

    def test_every_rotation_mode_is_valid(self):
        os.environ.pop("OVERLAY_MODE_ROTATION", None)
        valid = {"protip", "reddit", "fact", "none", "beatstore"}
        for n in range(1, 12):
            self.assertIn(generate_short._pick_overlay_mode(short_num=n), valid)


if __name__ == "__main__":
    unittest.main()

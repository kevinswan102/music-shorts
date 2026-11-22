import os, tempfile, unittest
import numpy as np
import soundfile as sf
import beat_analyzer as ba


def _wav(y, sr=22050):
    f = tempfile.NamedTemporaryFile(suffix='.wav', delete=False); f.close()
    sf.write(f.name, y, sr)
    return f.name


class BeatlessTrackTest(unittest.TestCase):
    def test_beatless_pad_does_not_crash(self):
        sr = 22050
        t = np.arange(0, 60, 1 / sr)
        pad = 0.3 * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.05 * t))
        path = _wav(pad.astype(np.float32))
        try:
            from unittest import mock
            with mock.patch.object(ba.librosa.beat, 'beat_track', return_value=(np.array([0.0]), np.array([], dtype=int))):
                res = ba.analyze_track(path)
        finally:
            os.unlink(path)
        self.assertEqual(res['bpm'], 0.0)
        self.assertGreater(len(res['all_beat_times']), 10)
        self.assertLess(res['best_start'], res['best_end'])

    def test_silent_audio_raises(self):
        path = _wav(np.zeros(22050 * 30, dtype=np.float32))
        try:
            with self.assertRaises(ba.SilentAudioError):
                ba.analyze_track(path)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()

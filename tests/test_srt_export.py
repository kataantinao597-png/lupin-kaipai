import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.exporter import storyboard_to_srt


class SrtExportTests(unittest.TestCase):
    def test_srt_uses_cumulative_shot_times_and_utf8_text(self):
        storyboard = {
            "shots": [
                {"shot_number": 1, "duration_seconds": 1.5, "subtitle": "第一镜"},
                {"shot_number": 2, "duration_seconds": 2, "subtitle": "第二镜"},
            ]
        }
        actual = storyboard_to_srt(storyboard)
        self.assertEqual(
            actual,
            "1\n00:00:00,000 --> 00:00:01,500\n第一镜\n\n"
            "2\n00:00:01,500 --> 00:00:03,500\n第二镜\n",
        )

    def test_srt_rejects_missing_subtitle(self):
        with self.assertRaises(ValueError):
            storyboard_to_srt({"shots": [{"duration_seconds": 2, "subtitle": ""}]})


if __name__ == "__main__":
    unittest.main()

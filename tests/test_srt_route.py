import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import app as app_module
from test_feasibility_fallback import valid_storyboard


class SrtRouteTests(unittest.TestCase):
    def test_export_srt_returns_utf8_subtitle_file(self):
        response = app_module.export_srt(app_module.ExportRequest(storyboard=valid_storyboard()))
        self.assertEqual(response.media_type, "application/x-subrip")
        self.assertTrue(response.path.endswith(".srt"))


if __name__ == "__main__":
    unittest.main()

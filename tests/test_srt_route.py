import pytest

pytestmark = pytest.mark.unit

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# B-14（测试陈旧）：旧 app.export_srt / app.ExportRequest 已随路由拆分
# 迁到 backend/api/v1/exports.py；行为本身未变，测试改调新位置。
from backend.api.v1.exports import ExportRequest, export_srt
from test_feasibility_fallback import valid_storyboard


class SrtRouteTests(unittest.TestCase):
    def test_export_srt_returns_utf8_subtitle_file(self):
        response = export_srt(ExportRequest(storyboard=valid_storyboard()))
        self.assertEqual(response.media_type, "application/x-subrip")
        self.assertTrue(response.path.endswith(".srt"))


if __name__ == "__main__":
    unittest.main()

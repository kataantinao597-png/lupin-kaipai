import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import app as app_module
from backend.exporter import storyboard_to_srt
from test_feasibility_fallback import valid_storyboard


class DeliveryCheckTests(unittest.TestCase):
    def test_delivery_check_is_data_driven_and_blocks_unconfirmed_risks(self):
        storyboard = valid_storyboard()
        storyboard["risk_flags"] = [{"text": "待核实卖点", "level": "高", "reason": "缺少证据", "confirmed": False}]
        result = app_module.delivery_check(app_module.DeliveryCheckRequest(
            storyboard=storyboard,
            product={"product_name": "测试产品"},
            facts=[],
            feasibility=None,
            shoot_state={},
        ))
        self.assertFalse(result["ready"])
        risks = next(item for item in result["items"] if item["id"] == "risks")
        self.assertEqual(risks["status"], "action")
        self.assertIn("人工确认", risks["reason"])

    def test_delivery_check_marks_empty_project_as_action(self):
        result = app_module.delivery_check(app_module.DeliveryCheckRequest(
            storyboard=None, product=None, facts=[], feasibility=None, shoot_state={}
        ))
        self.assertFalse(result["ready"])
        self.assertTrue(any(item["status"] == "action" for item in result["items"]))


if __name__ == "__main__":
    unittest.main()

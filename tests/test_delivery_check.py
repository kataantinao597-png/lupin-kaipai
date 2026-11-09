import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# B-14（测试陈旧）：旧 app.delivery_check / app.DeliveryCheckRequest 已随
# 路由拆分迁到 backend/api/v1/risks.py（delivery_check_route，逻辑在
# services/delivery_service.check_delivery）；行为本身未变，测试改调新位置。
from backend.api.v1.risks import DeliveryCheckRequest, delivery_check_route
from test_feasibility_fallback import valid_storyboard


class DeliveryCheckTests(unittest.TestCase):
    def test_delivery_check_is_data_driven_and_blocks_unconfirmed_risks(self):
        storyboard = valid_storyboard()
        storyboard["risk_flags"] = [{"text": "待核实卖点", "level": "高", "reason": "缺少证据", "confirmed": False}]
        result = delivery_check_route(DeliveryCheckRequest(
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
        result = delivery_check_route(DeliveryCheckRequest(
            storyboard=None, product=None, facts=[], feasibility=None, shoot_state={}
        ))
        self.assertFalse(result["ready"])
        self.assertTrue(any(item["status"] == "action" for item in result["items"]))


if __name__ == "__main__":
    unittest.main()

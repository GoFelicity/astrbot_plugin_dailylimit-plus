"""内置用户条款子插件的拦截与签署流程。"""

import asyncio
import unittest

from support import Event, make_plugin, request

ACCEPT = "我已知晓并同意遵守条款所有内容"


def terms_plugin(**updates):
    config = {"enabled": True, "user_scope_mode": "all", "group_scope_mode": "all"}
    config.update(updates)
    return make_plugin()


class UserTermsTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()
        self.plugin.config["user_terms"] = {
            "enabled": True,
            "user_scope_mode": "all",
            "group_scope_mode": "all",
            "terms_text": "测试条款",
        }

    def test_first_request_prompts_and_blocks(self):
        allowed, event = request(self.plugin, "20", None, text="hello")
        self.assertFalse(allowed)
        self.assertTrue(event.stopped)
        self.assertTrue(any("测试条款" in msg for msg in event.sent))

    def test_accept_before_prompt_does_not_sign(self):
        allowed, event = request(self.plugin, "20", None, text=ACCEPT)
        self.assertFalse(allowed)
        self.assertTrue(any("请先阅读" in msg for msg in event.sent))
        self.assertFalse(self.plugin.user_terms.describe("user:20")["signed"])

    def test_accept_after_prompt_signs_and_unblocks(self):
        request(self.plugin, "20", None, text="hello")
        allowed, _ = request(self.plugin, "20", None, text=ACCEPT)
        self.assertFalse(allowed)  # 本次只回复"已记录"
        allowed, _ = request(self.plugin, "20", None, text="hello")
        self.assertTrue(allowed)

    def test_exempt_user_is_not_checked(self):
        self.plugin.config["user_terms"]["exempt_user_sids"] = ["user:20"]
        allowed, _ = request(self.plugin, "20", None, text="hello")
        self.assertTrue(allowed)

    def test_reject_blocks_and_records(self):
        request(self.plugin, "20", None, text="hello")
        allowed, event = request(self.plugin, "20", None, text="拒绝")
        self.assertFalse(allowed)
        self.assertEqual(self.plugin.user_terms.describe("user:20")["status"], "rejected")

    def test_disabled_user_gets_disabled_message(self):
        self.plugin.config["user_terms"]["disabled_user_sids"] = ["user:20"]
        allowed, event = request(self.plugin, "20", None, text="hello")
        self.assertFalse(allowed)
        self.assertTrue(any("禁用" in msg for msg in event.sent))

    def test_publish_requires_reaccept(self):
        request(self.plugin, "20", None, text="hello")
        request(self.plugin, "20", None, text=ACCEPT)
        self.assertTrue(request(self.plugin, "20", None, text="hello")[0])
        self.plugin.user_terms.publish("新条款正文")
        self.assertFalse(request(self.plugin, "20", None, text="hello")[0])


class TokenStatsTests(unittest.TestCase):
    def test_today_bucket_and_display(self):
        plugin = make_plugin()
        resp = type("R", (), {"usage": type("U", (), {
            "input_other": 100, "input_cached": 20, "output": 30})()})()
        asyncio.run(plugin.on_llm_response(Event("20"), resp))
        today = plugin.token_tracker.get_today("20")
        self.assertEqual(today["total"], 150)
        self.assertEqual(today["calls"], 1)
        line = plugin.message_builder._build_token_line("20")
        self.assertIn("今日 token", line)
        self.assertIn("150", line)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

from app.api.v1.chat import MessageItem
from app.services.grok.services.chat import MessageExtractor, _looks_like_auto_image_prompt
from app.services.grok.services.responses import _coerce_input_to_messages
from app.services.grok.utils.prompt_debug import (
    summarize_chat_messages,
    summarize_prompt_text,
)
from app.services.reverse.app_chat import APP_CHAT_REQUEST_MODE_ID, AppChatReverse


class PromptDiagnosticsTests(unittest.TestCase):
    def test_chinese_prompt_summary_stays_consistent_from_api_to_payload(self):
        prompt = "生成一张图片：gemini娘"
        api_messages = [MessageItem(role="user", content=prompt)]
        extracted_message, _, _ = MessageExtractor.extract(
            [{"role": "user", "content": prompt}]
        )

        with patch(
            "app.services.reverse.app_chat.get_config",
            side_effect=lambda key, default=None: {
                "app.custom_instruction": "",
                "app.disable_memory": False,
                "app.temporary": False,
                "app.auto_enable_420": True,
            }.get(key, default),
        ):
            payload = AppChatReverse.build_payload(
                message=extracted_message,
                model="grok-auto",
                mode="auto",
                request_strategy=APP_CHAT_REQUEST_MODE_ID,
            )

        api_summary = summarize_chat_messages(api_messages)
        extracted_summary = summarize_prompt_text(extracted_message)
        payload_summary = summarize_prompt_text(payload["message"])

        self.assertEqual(api_summary["message_hash"], extracted_summary["message_hash"])
        self.assertEqual(api_summary["message_hash"], payload_summary["message_hash"])
        self.assertGreater(api_summary["non_ascii_count"], 0)
        self.assertGreater(extracted_summary["non_ascii_count"], 0)
        self.assertGreater(payload_summary["non_ascii_count"], 0)
        self.assertTrue(api_summary["has_cjk"])
        self.assertTrue(payload_summary["has_image_keywords"])

    def test_responses_input_summary_preserves_cjk_text(self):
        messages = _coerce_input_to_messages(
            [{"type": "input_text", "text": "生成一张图片：gemini娘"}]
        )

        summary = summarize_chat_messages(messages)

        self.assertEqual(len(messages), 1)
        self.assertTrue(summary["has_cjk"])
        self.assertGreater(summary["non_ascii_count"], 0)
        self.assertTrue(summary["has_image_keywords"])

    def test_quick_image_intent_examples(self):
        positive_prompts = [
            "生成一张图片：gemini娘",
            "请直接调用图片生成，生成一个美女，只返回图片",
            "生成一张美女图片",
        ]
        negative_prompts = [
            "生成一个美女",
            "Gemini 是什么",
            "Google Gemini 和 Grok 有什么区别",
        ]

        for prompt in positive_prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(
                    _looks_like_auto_image_prompt(
                        [{"role": "user", "content": prompt}]
                    )
                )

        for prompt in negative_prompts:
            with self.subTest(prompt=prompt):
                self.assertFalse(
                    _looks_like_auto_image_prompt(
                        [{"role": "user", "content": prompt}]
                    )
                )


if __name__ == "__main__":
    unittest.main()

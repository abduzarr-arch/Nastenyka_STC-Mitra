import unittest

from assistant_context import bounded_history, needs_directory_context


class ContextSelectionTests(unittest.TestCase):
    def test_directory_is_not_loaded_for_regular_question(self):
        self.assertFalse(needs_directory_context("Что известно по расчёту плиты?"))
        self.assertTrue(needs_directory_context("Кто делает расчёт и в какой чат ему написать?"))

    def test_history_keeps_newest_messages_within_budget(self):
        history = [
            {"role": "user", "content": "старый вопрос"},
            {"role": "assistant", "content": "старый ответ"},
            {"role": "user", "content": "новый вопрос"},
        ]
        selected = bounded_history(history, len("старый ответ") + len("новый вопрос"))
        self.assertEqual([item["content"] for item in selected], ["старый ответ", "новый вопрос"])


if __name__ == "__main__":
    unittest.main()

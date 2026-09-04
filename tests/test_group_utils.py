import unittest

from group_utils import _starts_with_group_trigger


class GroupTriggerTests(unittest.TestCase):
    def test_leading_name_is_direct_address(self):
        self.assertTrue(_starts_with_group_trigger("Настенька, дай статус", "настенька"))
        self.assertTrue(_starts_with_group_trigger("  #настенька: помоги", "#настенька"))

    def test_name_inside_conversation_is_not_direct_address(self):
        self.assertFalse(_starts_with_group_trigger("Мы спросим Настеньку позже", "настенька"))
        self.assertFalse(_starts_with_group_trigger("Этот бот вчера отвечал", "бот"))
        self.assertFalse(_starts_with_group_trigger("ботинок нужен для модели", "бот"))


if __name__ == "__main__":
    unittest.main()

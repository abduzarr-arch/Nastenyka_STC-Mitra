import unittest
from datetime import date

from trz import _looks_like_report_request, looks_like_time_entry, parse_time_entry


class TrzParsingTests(unittest.TestCase):
    def test_employee_multiline_template_is_saved_as_entry(self):
        text = '''"28.07.2026" - дата работ
"*Сигнальный" - объект на котором выполнялись работы в отчётный день
"*3" - ТРЗ трудозатраты в часах затраченные за отчётный день
"*Созвон и ознакомление с ИД
Добавление новой стены, добавление части отверстий" - список
работ выполненных за отчётный день'''

        parsed, error = parse_time_entry(text)

        self.assertIsNone(error)
        self.assertTrue(looks_like_time_entry(text))
        self.assertFalse(_looks_like_report_request(text))
        self.assertEqual(parsed["work_date"], date(2026, 7, 28))
        self.assertEqual(parsed["project_name"], "Сигнальный")
        self.assertEqual(parsed["hours"], 3)
        self.assertEqual(
            parsed["task_name"],
            "Созвон и ознакомление с ИД Добавление новой стены, добавление части отверстий",
        )

    def test_regular_report_request_is_not_an_entry(self):
        text = "Покажи ТРЗ за 28.07.2026"
        self.assertFalse(looks_like_time_entry(text))
        self.assertTrue(_looks_like_report_request(text))

    def test_pipe_format_still_works(self):
        parsed, error = parse_time_entry("ТРЗ: Лиговский | расчёт плиты | 6 | 28.07.2026")
        self.assertIsNone(error)
        self.assertEqual(parsed["project_name"], "Лиговский")
        self.assertEqual(parsed["hours"], 6)


if __name__ == "__main__":
    unittest.main()

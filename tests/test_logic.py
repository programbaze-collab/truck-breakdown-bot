"""Тесты чистой логики. Запуск: python -m unittest -v"""
import unittest

from bot.logic import (
    dispatcher_card, driver_summary, merge_missing, next_step, normalize_extraction,
    normalize_name, normalize_phone, normalize_plate, text_hash,
)

FULL = {"id": 7, "category": "wheel", "urgency": "stuck", "location": "М3, 240 км", "cargo": "loaded",
        "raw_text": "Пробил колесо на М3, 240 км, фура гружёная", "status": "new", "photos_done": False}


class NextStep(unittest.TestCase):
    def test_asks_fields_in_order_one_by_one(self):
        r = {}
        self.assertEqual(next_step(r, 0), "category")
        r["category"] = "wheel"
        self.assertEqual(next_step(r, 0), "urgency")
        r["urgency"] = "stuck"
        self.assertEqual(next_step(r, 0), "location")
        r["location"] = "М3"
        self.assertEqual(next_step(r, 0), "cargo")
        r["cargo"] = "empty"
        self.assertEqual(next_step(r, 0), "photos")

    def test_only_missing_field_is_asked(self):
        self.assertEqual(next_step({**FULL, "location": None}, 2), "location")

    def test_photos_min_and_max(self):
        self.assertEqual(next_step(FULL, 0), "photos")
        self.assertEqual(next_step(FULL, 3), "photos")          # можно добавить ещё
        self.assertEqual(next_step({**FULL, "photos_done": True}, 1), "confirm")
        self.assertEqual(next_step({**FULL, "photos_done": True}, 0), "photos")  # «Готово» без фото не пускает
        self.assertEqual(next_step(FULL, 5), "confirm")          # 5 фото: сразу к подтверждению


class Extraction(unittest.TestCase):
    def test_drops_invalid_values(self):
        got = normalize_extraction({"category": "tyre", "urgency": "stuck", "location": "  null ", "cargo": "loaded"})
        self.assertEqual(got, {"category": None, "urgency": "stuck", "location": None, "cargo": "loaded",
                               "is_breakdown": True})

    def test_handles_garbage(self):
        self.assertEqual(normalize_extraction(None),
                         {"category": None, "urgency": None, "location": None, "cargo": None, "is_breakdown": True})

    def test_not_a_breakdown(self):
        self.assertFalse(normalize_extraction({"is_breakdown": False})["is_breakdown"])

    def test_merge_does_not_overwrite(self):
        self.assertEqual(merge_missing({"category": "wheel", "cargo": None},
                                       {"category": "engine", "cargo": "empty", "urgency": None,
                                        "is_breakdown": True}),
                         {"cargo": "empty"})


class Dedup(unittest.TestCase):
    def test_same_text_with_different_spacing_and_case(self):
        self.assertEqual(text_hash("Пробил колесо на М3, 240 км!"), text_hash("пробил  колесо на м3 240 км"))
        self.assertEqual(text_hash("гружёная"), text_hash("груженая"))

    def test_different_text(self):
        self.assertNotEqual(text_hash("Пробил колесо"), text_hash("Не заводится"))


class Registration(unittest.TestCase):
    def test_plate(self):
        self.assertEqual(normalize_plate("a 123 bc 77"), "А123ВС77")   # латиница в кириллицу
        self.assertEqual(normalize_plate("А123ВС-777"), "А123ВС777")
        self.assertIsNone(normalize_plate("абв"))
        self.assertIsNone(normalize_plate("АААААА"))

    def test_name_and_phone(self):
        self.assertEqual(normalize_name("  Иван   Петров "), "Иван Петров")
        self.assertIsNone(normalize_name("1"))
        self.assertEqual(normalize_phone("79001234567"), "+79001234567")


class Cards(unittest.TestCase):
    def test_card_fits_caption_and_escapes_html(self):
        req = {**FULL, "raw_text": "<b>" + "х" * 3000}
        card = dispatcher_card(req, {"full_name": "Иван", "phone": "+7900", "plate": "А123ВС77"},
                               ["https://x/1.jpg"] * 5)
        self.assertLessEqual(len(card), 1024)
        self.assertNotIn("<b>ххх", card)
        self.assertIn("Заявка №7", card)

    def test_summary_mentions_everything(self):
        s = driver_summary(FULL, 2)
        for part in ("Колесо", "Стою на трассе", "М3, 240 км", "Гружёный", "2 шт"):
            self.assertIn(part, s)


if __name__ == "__main__":
    unittest.main()

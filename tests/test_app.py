import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from app import Config, Slot, format_notification, group_slots_by_date, load_state, newly_available, next_weekday_range, parse_slot, relevant_slots, save_state

TZ = ZoneInfo("Europe/Minsk")


def config(path: Path) -> Config:
    return Config("token", "chat", 60, time(18), TZ, path, 20, False)


class MonitorTests(unittest.TestCase):
    def test_parse_available_slot(self):
        item = {
            "id": 42,
            "start": int(datetime(2026, 9, 4, 19, tzinfo=TZ).timestamp()),
            "end": int(datetime(2026, 9, 4, 20, tzinfo=TZ).timestamp()),
            "max_services": 3,
            "prices": [{"price": 2400, "available": True, "quota": 3}],
        }
        slot = parse_slot(item, TZ)
        self.assertTrue(slot.available)
        self.assertEqual((slot.courts, slot.price_minor), (3, 2400))

    def test_grey_slot_is_not_available(self):
        item = {"id": 42, "start": 1, "end": 2, "max_services": 0,
                "prices": [{"price": 2400, "available": False, "quota": 0}]}
        self.assertFalse(parse_slot(item, TZ).available)

    def test_only_evening_and_not_imminent(self):
        now = datetime(2026, 9, 4, 17, 45, tzinfo=TZ)
        slots = [
            Slot(1, datetime(2026, 9, 4, 17, tzinfo=TZ), datetime(2026, 9, 4, 18, tzinfo=TZ), True, 1, 2200),
            Slot(2, datetime(2026, 9, 4, 18, tzinfo=TZ), datetime(2026, 9, 4, 19, tzinfo=TZ), True, 1, 2400),
            Slot(3, datetime(2026, 9, 4, 19, tzinfo=TZ), datetime(2026, 9, 4, 20, tzinfo=TZ), True, 1, 2400),
        ]
        self.assertEqual([s.event_id for s in relevant_slots(slots, config(Path("state")), now)], [3])

    def test_first_run_is_silent_then_transition_notifies(self):
        slot = Slot(1, datetime(2026, 9, 5, 19, tzinfo=TZ), datetime(2026, 9, 5, 20, tzinfo=TZ), True, 1, 2400)
        self.assertEqual(newly_available([slot], {"initialized": False, "slots": {}}, False), [])
        self.assertEqual(newly_available([slot], {"initialized": True, "slots": {"1": False}}, False), [slot])
        self.assertEqual(newly_available([slot], {"initialized": True, "slots": {"1": True}}, False), [])

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(path, {"42": True})
            self.assertTrue(load_state(path)["initialized"])
            self.assertEqual(load_state(path)["slots"], {"42": True})

    def test_notification_groups_slots_by_date_without_price(self):
        slots = [
            Slot(1, datetime(2026, 9, 4, 19, tzinfo=TZ), datetime(2026, 9, 4, 20, tzinfo=TZ), True, 2, 2400),
            Slot(2, datetime(2026, 9, 4, 20, tzinfo=TZ), datetime(2026, 9, 4, 21, tzinfo=TZ), True, 1, 2400),
            Slot(3, datetime(2026, 9, 5, 18, tzinfo=TZ), datetime(2026, 9, 5, 19, tzinfo=TZ), True, 3, 2200),
        ]
        groups = group_slots_by_date(slots)
        self.assertEqual([len(group) for group in groups], [2, 1])
        message = format_notification(groups[0])
        self.assertIn("04.09.2026 — Пятница", message)
        self.assertIn("19:00–20:00 — свободно: <b>2</b>", message)
        self.assertIn("20:00–21:00 — свободно: <b>1</b>", message)
        self.assertNotIn("Цена", message)
        self.assertNotIn("24.00", message)

    def test_next_weekday_range(self):
        self.assertEqual(next_weekday_range(date(2026, 9, 3)), (date(2026, 9, 7), date(2026, 9, 11)))
        self.assertEqual(next_weekday_range(date(2026, 9, 7)), (date(2026, 9, 14), date(2026, 9, 18)))


if __name__ == "__main__":
    unittest.main()

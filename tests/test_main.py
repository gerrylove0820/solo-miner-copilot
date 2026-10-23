import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.main import App
from app.config import load_config


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = os.path.join(self.tmp, "c.yaml")
        with open(p, "w") as fh:
            fh.write("timezone: America/New_York\ntelegram: {owner_chat_id: 4242}\nnetwork: {enabled: false}\nminers:\n  - {name: B1, type: axeos, host: 192.168.1.53}\n")
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        self.app = App(load_config(p), self.tmp, dry_run=True)
        self.tz = ZoneInfo("America/New_York")

    def ts(self, y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=self.tz).timestamp()

    def test_daily_summary_fires_once_at_8am_ny(self):
        # first run at 3 PM: must NOT fire immediately
        self.assertFalse(self.app.maybe_daily_summary(self.ts(2026, 10, 11, 15, 0)))
        self.assertFalse(self.app.maybe_daily_summary(self.ts(2026, 10, 12, 7, 59)))
        self.assertTrue(self.app.maybe_daily_summary(self.ts(2026, 10, 12, 8, 0)))
        self.assertFalse(self.app.maybe_daily_summary(self.ts(2026, 10, 12, 8, 1)))
        self.assertTrue(self.app.maybe_daily_summary(self.ts(2026, 10, 13, 8, 0, )))
        self.assertIn("Daily mining summary", self.app.bot.sent[-1][1])

    def test_first_run_before_8_fires_same_day(self):
        self.assertFalse(self.app.maybe_daily_summary(self.ts(2026, 10, 11, 6, 0)))
        self.assertTrue(self.app.maybe_daily_summary(self.ts(2026, 10, 11, 8, 0)))

    def test_dst_boundary(self):
        # Nov 1 2026 is the DST end in the US; 8 AM local should still fire once
        self.app.maybe_daily_summary(self.ts(2026, 10, 31, 9, 0))
        self.assertTrue(self.app.maybe_daily_summary(self.ts(2026, 11, 1, 8, 0)))


if __name__ == "__main__":
    unittest.main()

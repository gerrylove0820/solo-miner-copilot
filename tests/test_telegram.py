import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.config import load_config
from app.main import App

from app.telegram_bot import TelegramBot, split_message


def upd(chat_id, text, username="u"):
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "username": username}, "text": text}}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_unconfigured_chat_gets_setup_only(self):
        b = TelegramBot(None, self.tmp, dry_run=True)
        handler = Mock(return_value="FLEET DATA")
        b.handlers = {"/status": handler}
        for cmd in ("/start", "/status", "/help", "/summary", "/proxy"):
            with self.assertLogs("telegram", level="INFO") as logs:
                b.handle_update(upd(111, cmd))
            self.assertIn("111", " ".join(logs.output))
            self.assertIn("Your chat ID is 111", b.sent[-1][1])
            self.assertIn("TELEGRAM_CHAT_ID=111", b.sent[-1][1])
        handler.assert_not_called()
        self.assertIsNone(b.owner_chat_id)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "owner_chat.json")))

    def configured_app(self, env):
        path = os.path.join(self.tmp, "config.yaml")
        with open(path, "w") as fh:
            fh.write('telegram: {owner_chat_id: "4242"}\n')
        with patch.dict(os.environ, env, clear=True):
            app = App(load_config(path), self.tmp, dry_run=True)
        self.addCleanup(app.store.db.close)
        self.addCleanup(app.pool.shutdown)
        app.bot.handlers = {"/status": lambda: "STATUS"}
        return app

    def test_config_owner_chat_id(self):
        b = self.configured_app({}).bot
        self.assertEqual(b.owner_chat_id, "4242")
        b.handle_update(upd(111, "/start"))
        b.handle_update(upd(111, "/status"))
        self.assertEqual(b.sent, [])
        b.handle_update(upd(4242, "/status@MyBot"))
        self.assertEqual(b.sent[-1], ("4242", "STATUS"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "owner_chat.json")))

    def test_env_owner_wins_over_config(self):
        b = self.configured_app({"TELEGRAM_CHAT_ID": "999"}).bot
        self.assertEqual(b.owner_chat_id, "999")
        b.handle_update(upd(4242, "/status"))
        self.assertEqual(b.sent, [])
        b.handle_update(upd(999, "/status"))
        self.assertEqual(b.sent[-1], ("999", "STATUS"))

    def test_guest_without_owner_does_not_claim(self):
        b = TelegramBot(None, self.tmp, dry_run=True)
        b.guest = Mock()
        b.guest.welcome.return_value = "GUEST WELCOME"
        b.guest.command.return_value = "GUEST STATUS"
        b.handlers = {"/status": Mock(return_value="FLEET DATA")}
        b.handle_update(upd(111, "/start"))
        self.assertIn("Your chat ID is 111", b.sent[-1][1])   # setup hint, not owner
        b.guest.welcome.assert_not_called()
        b.handle_update(upd(111, "/status"))
        b.handlers["/status"].assert_not_called()
        b.guest.command.assert_called_once_with("111", "/status", [], is_owner=False)
        self.assertIsNone(b.owner_chat_id)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "owner_chat.json")))

    def test_fixed_chat_id(self):
        b = TelegramBot(None, self.tmp, fixed_chat_id="999", dry_run=True)
        b.handle_update(upd(111, "/start"))
        self.assertEqual(b.sent, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "owner_chat.json")))
        b.handle_update(upd(999, "/help"))
        self.assertEqual(b.sent[-1][0], "999")

    def test_send_without_owner_not_dry(self):
        b = TelegramBot("123:fake", self.tmp, dry_run=False)
        self.assertFalse(b.send("hello"))   # dropped, no network call

    def test_split(self):
        parts = split_message("\n".join(["x" * 100] * 100), limit=1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual(sum(p.count("x") for p in parts), 10000)


if __name__ == "__main__":
    unittest.main()

"""Start mock miners and run the real app loop in DRY-RUN mode for N seconds."""
import os
import sys
import tempfile
import threading

from tests import mock_miners
from app import main as appmain

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 25
mock_miners.start()
data = tempfile.mkdtemp(prefix="smc-dry-")
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.environ["TELEGRAM_CHAT_ID"] = "4242"
os.environ["DRY_RUN"] = "1"
cfg_path = os.path.join(os.path.dirname(__file__), "config.mock.yaml")
cfg = appmain.load_config(cfg_path)
app = appmain.App(cfg, data, dry_run=True)
# simulate the owner pressing /start, then /status
app.bot.handle_update({"update_id": 1, "message": {"chat": {"id": 4242, "username": "alice"}, "text": "/start"}})
threading.Timer(secs, app.stop.set).start()
app.run()
app.bot.handle_update({"update_id": 2, "message": {"chat": {"id": 4242}, "text": "/status"}})
app.bot.handle_update({"update_id": 3, "message": {"chat": {"id": 4242}, "text": "/summary"}})
app.bot.handle_update({"update_id": 4, "message": {"chat": {"id": 999}, "text": "/status"}})  # ignored
print(f"\nDRY RUN DONE: {len(app.bot.sent)} messages 'sent'; data dir {data}")

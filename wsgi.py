import os

from main import create_app
from data import telegram_bridge


app = create_app(os.environ.get("SKILLWOOD_DB_PATH", "db/blogs.db"))
telegram_bridge.schedule_media_cache_trim()
application = app

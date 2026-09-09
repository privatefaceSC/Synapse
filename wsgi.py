import os

from main import create_app


app = create_app(os.environ.get("SKILLWOOD_DB_PATH", "db/blogs.db"))

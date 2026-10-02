import os

SECRET_KEY = "linkly-dev-secret-key-2024"
DB_PATH = os.environ.get("LINKLY_DB", "linkly.db")
BASE_URL = os.environ.get("LINKLY_BASE_URL", "http://localhost:8080")
WEBHOOK_URL = os.environ.get("LINKLY_WEBHOOK")

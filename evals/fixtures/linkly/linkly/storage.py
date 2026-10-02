import sqlite3
from contextlib import contextmanager


class LinkStore:
    def __init__(self, path):
        self.path = path
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, url TEXT NOT NULL, hits INTEGER DEFAULT 0)"
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save(self, code, url):
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO links (code, url) VALUES (?, ?)", (code, url))

    def resolve(self, code):
        with self._connect() as conn:
            row = conn.execute("SELECT url FROM links WHERE code = ?", (code,)).fetchone()
            if row:
                conn.execute("UPDATE links SET hits = hits + 1 WHERE code = ?", (code,))
            return row[0] if row else None

    def search(self, term):
        with self._connect() as conn:
            query = f"SELECT code, url FROM links WHERE url LIKE '%{term}%'"
            return conn.execute(query).fetchall()

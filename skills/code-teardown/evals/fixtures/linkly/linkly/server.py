import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

from . import config
from .notify import notify_created
from .shortener import is_valid_url, make_code
from .storage import LinkStore

store = LinkStore(config.DB_PATH)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        if self.path == "/shorten":
            data = parse_qs(body)
            if "url" in data:
                url = data["url"][0]
                if is_valid_url(url):
                    code = make_code(url)
                    store.save(code, url)
                    notify_created(code, url)
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"code": code, "short": config.BASE_URL + "/" + code}).encode())
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(400)
                self.end_headers()
        elif self.path == "/search":
            try:
                rows = store.search(parse_qs(body).get("q", [""])[0])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(rows).encode())
            except:
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        url = store.resolve(self.path.lstrip("/"))
        if url:
            self.send_response(302)
            self.send_header("Location", url)
        else:
            self.send_response(404)
        self.end_headers()


def main():
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()

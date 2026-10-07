# linkly

A tiny URL shortener. `linkly` starts an HTTP server on port 8080.

- `POST /shorten` with `url=...` returns a short code
- `GET /<code>` redirects

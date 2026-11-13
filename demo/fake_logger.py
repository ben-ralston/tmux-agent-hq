#!/usr/bin/env python3
"""Stand-in for the unified logger's read API, used only by the demo instance.

Serves ``GET /logs`` on 127.0.0.1 from ``demo/data.py`` so the Errors tab has
rows to show. Accepts and discards ``POST /logs``. Honours the query
parameters the app sends: ``app``, ``level``, ``since``, ``since_id``,
``limit`` and ``order``.

    python3 demo/fake_logger.py [port]
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import data

DEFAULT_PORT = 9913


def rows() -> list[dict]:
    ordered = sorted(data.LOG_ROWS, key=lambda row: -row[2])  # oldest first
    return [
        {
            "id": 1000 + index,
            "app": app,
            "level": level,
            "ts": data.iso(minutes_ago),
            "source": source,
            "message": message,
            "json_extra": json.dumps(extra) if extra is not None else None,
        }
        for index, (app, level, minutes_ago, source, message, extra) in enumerate(ordered)
    ]


def since_cutoff(value: str) -> str:
    """``since`` is a window such as ``24h`` or ``7d``, or an ISO timestamp."""
    minutes = {"m": 1, "h": 60, "d": 24 * 60}
    if value[:-1].isdigit() and value[-1] in minutes:
        return data.iso(int(value[:-1]) * minutes[value[-1]])
    return value


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        parsed = urlparse(self.path)
        if parsed.path != "/logs":
            self._send(404, {"error": "not found"})
            return
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        selected = rows()
        if "app" in query:
            selected = [row for row in selected if row["app"] == query["app"]]
        if "level" in query:
            levels = set(query["level"].split(","))
            selected = [row for row in selected if row["level"] in levels]
        if "since" in query:
            cutoff = since_cutoff(query["since"])
            selected = [row for row in selected if row["ts"] >= cutoff]
        if "since_id" in query:
            selected = [row for row in selected if row["id"] > int(query["since_id"])]
        if query.get("order") != "asc":
            selected.reverse()
        selected = selected[: int(query.get("limit", "100"))]
        max_id = max((row["id"] for row in selected), default=None)
        self._send(200, {"logs": selected, "count": len(selected), "max_id": max_id})

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send(200, {"ok": True})

    def log_message(self, *_args) -> None:
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

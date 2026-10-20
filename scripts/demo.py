#!/usr/bin/env python3
"""Run the dashboard with fictional, in-memory data and no hardware access."""
import argparse
import copy
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("h2d_demo_source", ROOT / "air/h2d_logger.py")
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)  # Imports definitions only; never calls the logger's main().
STATE = {
    "printer": {
        "online": True, "gcode_state": "RUNNING", "stg_cur": "0", "mc_percent": 64,
        "mc_remaining_time": 52, "layer_num": 186, "subtask_name": "Demo enclosure · ASA + HIPS",
        "chamber_temp": 58, "chamber_target": 60, "bed_temp": 100, "bed_target": 100,
        "nozzle_right_temp": 265, "nozzle_right_target": 265, "nozzle_left_temp": 245,
        "nozzle_left_target": 245, "filaments": ["ASA", "HIPS"],
    },
    "fan": {
        "job": "demo", "speed": 5, "is_on": True, "mode": "auto", "manual_seconds_left": 0,
        "purge_seconds_left": 0, "busy": False,
    },
    "settings": copy.deepcopy(source.DEFAULT_SETTINGS), "server_time": "Demo data",
}
HTML = source.DASHBOARD_HTML.replace(
    "document.getElementById('host-badge').textContent = location.host;",
    "document.getElementById('host-badge').textContent = 'DEMO · simulated data';",
).replace("<!-- CSV_LOGS_TABLE -->", '<tr><td>status-2026-01-15.csv</td><td>42.0 KB · demo</td></tr>')


class Handler(BaseHTTPRequestHandler):
    def send(self, data, kind="application/json", status=200):
        body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/status":
            self.send(STATE)
        elif path == "/":
            self.send(HTML, "text/html; charset=utf-8")
        else:
            self.send({"error": "Not found"}, status=404)

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 8192:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            if path == "/api/settings":
                STATE["settings"].update({k: v for k, v in data.items() if k in STATE["settings"]})
            elif path == "/api/fan/manual":
                fan = STATE["fan"]
                auto = data.get("mode") == "auto"
                fan["mode"] = "auto" if auto else "manual"
                fan["is_on"] = auto or data.get("action") == "on"
                fan["speed"] = STATE["settings"]["print_speed"] if auto else max(1, min(10, int(data.get("speed", 5)))) if fan["is_on"] else 0
                fan["manual_seconds_left"] = max(0, int(data.get("duration_minutes", 0))) * 60
            else:
                self.send({"error": "Not found"}, status=404)
                return
            self.send({"ok": True, **STATE})
        except (ValueError, TypeError):
            self.send({"error": "Invalid demo request"}, status=400)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"Demo: http://localhost:{args.port} (simulated data; no hardware access)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

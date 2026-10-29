#!/usr/bin/env python3
"""Afterlight live workbench.

Scans local Claude Code / Codex session logs: sessions written to in the last 30 minutes
are "active", the rest go to the "library". The browser receives changes over SSE,
refreshed about every 2 seconds. Listens on 127.0.0.1 only.

  python3 server.py              # http://127.0.0.1:8770
  python3 server.py --port 9000 --active-minutes 60
"""
import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import afterlight as at

HERE = os.path.dirname(os.path.abspath(__file__))
ACTIVE_SEC = 30 * 60
LIBRARY_LIMIT = 300
TICK = 2.0

_lock = threading.Lock()
_meta = {}    # path -> (size, mtime, meta)
_parsed = {}  # path -> (activity_mtime, size, data)
_paths = {}   # id -> path


def _summary(path, now):
    st = os.stat(path)
    mt = at.activity_mtime(path)
    with _lock:
        hit = _meta.get(path)
    if not hit or hit[0] != st.st_size or hit[1] != st.st_mtime:
        hit = (st.st_size, st.st_mtime, at.quick_meta(path))
        with _lock:
            _meta[path] = hit
    m = dict(hit[2])
    m.pop("path", None)
    m.update(mtime=mt, size=st.st_size, status="active" if now - mt < ACTIVE_SEC else "library")
    with _lock:
        _paths[m["id"]] = path
    return m


def snapshot():
    """mtime only, no content reads: used by the SSE poll."""
    now = time.time()
    out = {}
    for fp, _ in at.all_session_files():
        try:
            mt = at.activity_mtime(fp)
        except OSError:
            continue
        if now - mt < ACTIVE_SEC:
            out[at.session_id(fp)] = (fp, mt)
    return out


def list_sessions(q=None):
    now = time.time()
    files = []
    for fp, _ in at.all_session_files():
        try:
            files.append((at.activity_mtime(fp), fp))
        except OSError:
            pass
    files.sort(reverse=True)
    active, library = [], []
    q = (q or "").strip().lower()
    for mt, fp in files:
        if now - mt < ACTIVE_SEC:
            active.append(_summary(fp, now))
        elif q or len(library) < LIBRARY_LIMIT:
            s = _summary(fp, now)
            if q and q not in " ".join(str(s.get(k) or "") for k in ("title", "prompt", "cwd", "id")).lower():
                continue
            library.append(s)
    return {"now": now, "activeMinutes": ACTIVE_SEC // 60, "active": active, "library": library[:LIBRARY_LIMIT],
            "libraryTotal": sum(1 for mt, _ in files if now - mt >= ACTIVE_SEC)}


def session_data(sid):
    with _lock:
        path = _paths.get(sid)
    if not path:
        list_sessions()
        with _lock:
            path = _paths.get(sid)
    if not path or not os.path.exists(path):
        return None
    mt, size = at.activity_mtime(path), os.path.getsize(path)
    with _lock:
        hit = _parsed.get(path)
    if not hit or hit[0] != mt or hit[1] != size:
        hit = (mt, size, at.detect_and_parse(path))
        with _lock:
            _parsed[path] = hit
    return {"id": sid, "mtime": mt, "status": "active" if time.time() - mt < ACTIVE_SEC else "library", "data": hit[2]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, open(os.path.join(HERE, "viewer.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path == "/api/sessions":
            return self._send(200, list_sessions(parse_qs(u.query).get("q", [""])[0]))
        if u.path.startswith("/api/session/"):
            d = session_data(u.path.rsplit("/", 1)[-1])
            return self._send(200, d) if d else self._send(404, {"error": "not found"})
        if u.path == "/api/stream":
            return self._stream()
        self._send(404, {"error": "not found"})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        prev, last_beat = snapshot(), time.time()
        try:
            while True:
                time.sleep(TICK)
                cur = snapshot()
                if set(cur) != set(prev):
                    self._emit("sessions", {"active": sorted(cur)})
                changed = [sid for sid, (_, mt) in cur.items() if sid in prev and prev[sid][1] != mt]
                if changed:
                    self._emit("changed", {"ids": changed})
                if time.time() - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                prev = cur
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _emit(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()


def main():
    global ACTIVE_SEC
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--active-minutes", type=int, default=30)
    a = ap.parse_args()
    ACTIVE_SEC = a.active_minutes * 60
    threading.Thread(target=list_sessions, daemon=True).start()  # warm the metadata cache
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    print(f"Afterlight → http://127.0.0.1:{a.port}  (active window: {a.active_minutes} min)")
    srv.serve_forever()


if __name__ == "__main__":
    main()

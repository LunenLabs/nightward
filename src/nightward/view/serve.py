"""Local HTTP server for the generated dashboard.

The page loads its data via ``fetch('./data.json')``, which browsers block under
the ``file://`` scheme (CORS). Serving over http://127.0.0.1 is therefore the
intended local-view path — and it matches the solo-dev persona's demand for an
instant, deploy-free look the moment a boundary breaches.
"""
from __future__ import annotations

import functools
import http.server
import socketserver
import webbrowser
from pathlib import Path

from ..errors import NightwardError


def serve(directory: Path | str, port: int = 8000, open_browser: bool = True) -> None:
    directory = str(Path(directory))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    # Bind loopback only — this is a personal dashboard, never a public listener.
    try:
        httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    except OSError as exc:  # port taken, privileged port, ...
        raise NightwardError(
            f"cannot serve on 127.0.0.1:{port} ({exc.strerror or exc}); "
            f"pick another with --port, or build only with --no-serve"
        ) from exc
    with httpd:
        url = f"http://127.0.0.1:{port}/"
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # pragma: no cover - headless / no browser
                pass
        print(f"serving {directory} at {url}  (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover - interactive
            print("\nstopped")

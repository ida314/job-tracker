"""Put the Agentation toolbar on a running `jobtracker serve`, without changing the app.

A local proxy: requests pass through to the upstream server, and HTML responses gain one
script tag plus the two CSP allowances that tag needs. Everything else is forwarded as is,
so buttons on the page do exactly what they do upstream, including their writes. Point it
at a preview, not at a `serve` holding your real files. See README.md.
"""
from __future__ import annotations

import argparse
import http.client
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BUNDLE = Path(__file__).resolve().parent / "dist" / "overlay.js"
SCRIPT_PATH = "/__agentation/overlay.js"
# Per-connection and per-message headers, which this proxy has to set for itself.
NOT_FORWARDED = {"connection", "keep-alive", "proxy-connection", "te", "trailer",
                 "transfer-encoding", "upgrade", "content-length", "host", "accept-encoding"}


def inject(html: str, endpoint: str) -> str:
    """Add the script tag. Not every page closes its body (`/jobboards` does not)."""
    tag = f'<script src="{SCRIPT_PATH}" data-endpoint="{endpoint}"></script>'
    head, closing, tail = html.rpartition("</body>")
    return f"{head}{tag}{closing}{tail}" if closing else html + tag


def widen_csp(policy: str, endpoint: str) -> str:
    """Allow the bundle and the annotation server. Other directives stay exactly as served."""
    extra = {"script-src": "'self'", "connect-src": endpoint}
    directives = []
    for directive in (part.strip() for part in policy.split(";")):
        name = directive.split(" ", 1)[0]
        directives.append(f"{directive} {extra[name]}" if name in extra else directive)
    return "; ".join(directives)


def make_handler(upstream: str, endpoint: str) -> type[BaseHTTPRequestHandler]:
    target = urlsplit(upstream)

    class Handler(BaseHTTPRequestHandler):
        def _relay(self) -> None:
            if self.path == SCRIPT_PATH:
                self._send(200, [("Content-Type", "text/javascript")], BUNDLE.read_bytes())
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in NOT_FORWARDED}
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=300)
            try:
                conn.request(self.command, self.path, body, headers)
                response = conn.getresponse()
                data = response.read()
                out = response.getheaders()
                status = response.status
            except OSError as exc:
                message = f"overlay: upstream {upstream} is not answering: {exc}\n"
                self._send(502, [("Content-Type", "text/plain")], message.encode())
                return
            finally:
                conn.close()
            if (response.getheader("Content-Type") or "").startswith("text/html"):
                page = data.decode("utf-8", "replace")
                data = inject(page, endpoint).encode("utf-8")
            self._send(status, out, data)

        def _send(self, status: int, headers: list, data: bytes) -> None:
            self.send_response(status)
            for name, value in headers:
                lowered = name.lower()
                if lowered in NOT_FORWARDED or lowered in {"server", "date"}:
                    continue
                if lowered == "content-security-policy":
                    value = widen_csp(value, endpoint)
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_POST = do_HEAD = _relay

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", default="http://127.0.0.1:8791",
                        help="the running server; use the URL `preview.py check` printed")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--endpoint", default="http://localhost:4747",
                        help="the agentation-mcp HTTP server")
    args = parser.parse_args()
    if not BUNDLE.exists():
        here = BUNDLE.parent.parent
        sys.exit(f"overlay: {BUNDLE} is missing. Run: npm --prefix {here} ci "
                 f"&& npm --prefix {here} run build")
    # Loopback only: anyone who can reach this port can click the page's buttons.
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(args.upstream.rstrip("/"), args.endpoint))
    print(f"overlay: http://127.0.0.1:{args.port} -> {args.upstream}, "
          f"annotations to {args.endpoint}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

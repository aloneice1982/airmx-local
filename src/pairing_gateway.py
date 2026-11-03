#!/usr/bin/env python3
"""Loopback-only pairing UI; exposes no keys, Wi-Fi credentials or write API."""
import argparse
import json
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=25890)
    parser.add_argument('--backend-port', type=int, default=25886)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent / 'pairing'
    static = {'/': ('index.html', 'text/html; charset=utf-8'),
              '/index.html': ('index.html', 'text/html; charset=utf-8'),
              '/main.mjs': ('main.mjs', 'application/javascript; charset=utf-8'),
              '/pairing-core.mjs': ('pairing-core.mjs', 'application/javascript; charset=utf-8'),
              '/app.css': ('app.css', 'text/css; charset=utf-8'),
              '/UPSTREAM-NOTE.md': ('UPSTREAM-NOTE.md', 'text/plain; charset=utf-8'),
              '/LICENSE.md': ('LICENSE.md', 'text/plain; charset=utf-8')}
    allowed_hosts = {'localhost:%d' % args.port, '127.0.0.1:%d' % args.port}
    # Explicitly bypass ambient HTTP proxy settings for the loopback SSH tunnel.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def backend(path):
        with opener.open('http://127.0.0.1:%d%s' % (args.backend_port, path), timeout=3) as res:
            return json.loads(res.read(65536))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def reply(self, status, body, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header('Permissions-Policy', 'bluetooth=(self)')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('Host') not in allowed_hosts:
                self.reply(403, b'{}')
                return
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + host for host in allowed_hosts}:
                self.reply(403, b'{}')
                return
            if self.path == '/api/readiness':
                ready = False
                try:
                    state = backend('/health')
                    result = subprocess.run(
                        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3',
                         'root@192.0.2.1',
                         'nft list table ip airmx_recovery >/dev/null 2>&1'],
                        capture_output=True, timeout=5, check=False)
                    ready = state.get('ready') is True and result.returncode == 0
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                self.reply(200, json.dumps({'ready': ready}).encode())
            elif self.path == '/api/status':
                try:
                    state = backend('/status')
                    # Forward only selected non-secret metadata; never key material.
                    safe = {k: state.get(k) for k in
                            ('deviceId', 'registered', 'fan_connected', 'sensor_connected', 'status',
                             'sensor', 'last_status_request')}
                    self.reply(200, json.dumps(safe).encode())
                except (OSError, ValueError):
                    self.reply(503, b'{}')
            elif self.path in static:
                name, content_type = static[self.path]
                self.reply(200, (root / name).read_bytes(), content_type)
            else:
                self.reply(404, b'{}')

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('Pairing page: http://localhost:%d' % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

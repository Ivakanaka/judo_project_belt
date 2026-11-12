"""Prototype server with expiring snapshots and an optional read-only YDB check."""
import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ydb_connection import log_connection_check

ROOT = Path(__file__).resolve().parent
TTL = 24 * 60 * 60
MAX_BODY = 2 * 1024 * 1024
BELTS = ['Белый', 'Жёлтый', 'Оранжевый', 'Зелёный', 'Синий', 'Коричневый', 'Чёрный']


class SnapshotStore:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS shares (key TEXT PRIMARY KEY, expires REAL, data TEXT)')

    def connect(self):
        return sqlite3.connect(self.path)

    def create(self, snapshot):
        token = secrets.token_urlsafe(32)
        expires = time.time() + TTL
        with self.connect() as db:
            db.execute('DELETE FROM shares WHERE expires <= ?', (time.time(),))
            if db.execute('SELECT count(*) FROM shares').fetchone()[0] >= 1000:
                raise OverflowError('Snapshot limit reached')
            db.execute('INSERT INTO shares VALUES (?, ?, ?)',
                       (hashlib.sha256(token.encode()).hexdigest(), expires, json.dumps(snapshot)))
        return token, expires

    def read(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM shares WHERE expires <= ?', (time.time(),))
            row = db.execute('SELECT data FROM shares WHERE key = ? AND expires > ?',
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return json.loads(row[0]) if row else None


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Invalid snapshot')

    def text(obj, key, limit, default=None):
        value = obj.get(key, default)
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError('Invalid field')
        return value

    snapshot = {key: text(data, key, limit) for key, limit in [('date', 200), ('start', 20), ('venue', 1000)]}
    rows = data.get('entries')
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError('Invalid entries')
    snapshot['entries'] = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get('age')) is not int or not 1 <= row['age'] <= 120:
            raise ValueError('Invalid entry')
        entry = {key: text(row, key, limit, '') for key, limit in [('name', 120), ('belt', 30), ('pay', 100), ('result', 300)]}
        if not entry['name'].strip() or entry['belt'] not in BELTS:
            raise ValueError('Invalid entry')
        entry['age'] = row['age']
        snapshot['entries'].append(entry)
    logo = text(data, 'logo', MAX_BODY, '')
    if logo:
        prefix = 'data:image/png;base64,'
        if not logo.startswith(prefix):
            raise ValueError('Invalid logo')
        raw = base64.b64decode(logo[len(prefix):], validate=True)
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Invalid logo')
    snapshot['logo'] = logo
    return snapshot


def render_snapshot(snapshot):
    esc = lambda value: html.escape(str(value), quote=True)
    present = {entry['belt'] for entry in snapshot['entries']}
    belt_names = ', '.join(belt for belt in BELTS if belt in present) or '—'
    rows = ''.join('<tr>' + ''.join(f'<td>{esc(value)}</td>' for value in
                   [i, entry['name'], entry['age'], entry['belt'], entry['pay'], entry['result']]) + '</tr>'
                   for i, entry in enumerate(snapshot['entries'], 1))
    logo = f'<img src="{esc(snapshot["logo"])}" alt="Логотип клуба">' if snapshot['logo'] else ''
    template = (ROOT / 'print.html').read_text()
    replacements = {'LOGO': logo, 'DATE': esc(snapshot['date']), 'START': esc(snapshot['start']),
                    'VENUE': esc(snapshot['venue']), 'COUNT': str(len(snapshot['entries'])),
                    'BELTS': esc(belt_names), 'ROWS': rows}
    # A single pass keeps user text from being interpreted as a template marker.
    return re.sub(r'\{\{(\w+)\}\}', lambda match: replacements[match[1]], template)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log bearer URLs or participant data.

    def send(self, status, body, content_type='text/html; charset=utf-8'):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Robots-Tag', 'noindex, nofollow, noarchive, nosnippet, noimageindex')
        self.send_header('Content-Security-Policy', "default-src 'none'; img-src 'self' blob: data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; media-src blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                         if self.path.startswith('/share/') else
                         "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; media-src 'self' blob: https:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            return self.send(200, (ROOT / 'index.html').read_bytes())
        if re.fullmatch(r'/share/[A-Za-z0-9_-]{43}', path):
            snapshot = self.server.store.read(path.rsplit('/', 1)[1])
            if snapshot:
                return self.send(200, render_snapshot(snapshot))
            return self.send(410, '<!doctype html><meta charset="utf-8"><title>Ссылка недоступна</title><h1>Срок действия ссылки истёк или ссылка недоступна</h1><p>Попросите сотрудника создать новую ссылку на ведомость.</p>')
        self.send(404, 'Страница не найдена')

    def do_POST(self):
        if self.path != '/api/shares':
            return self.send(405, 'Метод недоступен')
        # No cross-origin snapshot creation; the existing employee UI is a demo.
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://' + self.headers.get('Host', ''), 'https://' + self.headers.get('Host', '')):
            return self.send(403, 'Недопустимый источник')
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY or self.headers.get_content_type() != 'application/json':
                return self.send(400, 'Недопустимый запрос')
            snapshot = validate(json.loads(self.rfile.read(length)))
            token, expires = self.server.store.create(snapshot)
        except (ValueError, TypeError, KeyError):
            return self.send(400, 'Недопустимые данные ведомости')
        except OverflowError:
            return self.send(429, 'Слишком много ссылок')
        self.send(201, json.dumps({'path': '/share/' + token,
                  'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}), 'application/json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', '--host', dest='bind', default='127.0.0.1')
    parser.add_argument('--strictPort', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8000)))
    parser.add_argument('--database', default=str(ROOT / 'work' / 'shares.sqlite3'))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.store = SnapshotStore(args.database)
    print(f'Judo server ready on port {args.port}', flush=True)
    # Do not block the prototype or HTTP startup while checking the external database.
    threading.Thread(target=log_connection_check, name='ydb-check', daemon=True).start()
    server.serve_forever()

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Handler, SnapshotStore, ThreadingHTTPServer, render_snapshot, validate


class SharesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'test.sqlite3')
        self.store = SnapshotStore(self.path)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.store = self.store
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.snapshot = {'date': 'Аттестация · 28 ноября', 'start': '11:00', 'venue': 'Зал клуба', 'logo': '',
                         'entries': [{'name': 'Тестовый Участник', 'age': 10, 'belt': 'Жёлтый', 'pay': 'Да', 'result': 'Сдал'}]}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, data=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        client.request(method, path, json.dumps(data) if data is not None else None,
                       headers or {'Content-Type': 'application/json'})
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def create(self):
        status, _, body = self.request('POST', '/api/shares', self.snapshot)
        self.assertEqual(status, 201)
        return json.loads(body)['path']

    def test_snapshot_survives_restart_and_cannot_be_edited(self):
        path = self.create()
        self.server.store = SnapshotStore(self.path)
        self.snapshot['entries'][0]['name'] = 'Изменено'
        status, headers, body = self.request('GET', path)
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn('Тестовый Участник', text)
        self.assertNotIn('Изменено', text)
        self.assertIn('Сдал', text)
        self.assertIn('Пояса для сдачи: Жёлтый', text)
        self.assertNotIn('Синий', text)
        self.assertNotIn('<input', text)
        self.assertNotIn('<select', text)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request('POST', path, self.snapshot)[0], 405)

    def test_expiry_is_enforced_by_server_and_record_removed(self):
        path = self.create()
        with self.store.connect() as db:
            db.execute('UPDATE shares SET expires = ?', (time.time() - 1,))
        self.assertEqual(self.request('GET', path)[0], 410)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shares').fetchone()[0], 0)
        self.assertEqual(self.request('GET', '/share/' + 'a' * 43)[0], 410)

    def test_validation_and_origin(self):
        self.snapshot['entries'][0]['age'] = '<script>'
        self.assertEqual(self.request('POST', '/api/shares', self.snapshot)[0], 400)
        self.assertEqual(self.request('POST', '/api/shares', {},
                                     {'Content-Type': 'application/json', 'Origin': 'https://other.test'})[0], 403)
        self.assertEqual(self.request('GET', '/work/shares.sqlite3')[0], 404)

    def test_brown_belt_is_accepted_and_printed_in_belt_order(self):
        self.snapshot['entries'] = [
            {**self.snapshot['entries'][0], 'belt': belt}
            for belt in ['Чёрный', 'Коричневый', 'Синий']
        ]
        path = self.create()
        status, _, body = self.request('GET', path)
        self.assertEqual(status, 200)
        self.assertIn('Пояса для сдачи: Синий, Коричневый, Чёрный', body.decode())

    def test_html_escaped_and_no_template_injection(self):
        self.snapshot['entries'][0]['name'] = '<script>alert(1)</script>{{ROWS}}'
        text = render_snapshot(validate(self.snapshot))
        self.assertNotIn('<script>alert(1)</script>', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('{{ROWS}}', text)


if __name__ == '__main__':
    unittest.main()

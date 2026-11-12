import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ydb_connection import check_connection, log_connection_check


class YdbConnectionTest(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        self.env = {
            'YDB_ENDPOINT': 'grpcs://database.example:2135',
            'YDB_DATABASE': '/test/database',
            'YDB_SERVICE_ACCOUNT_KEY_JSON': json.dumps({
                'id': 'test-key-id', 'service_account_id': 'test-service-account',
                'private_key': self.private_key}),
        }

    def test_absent_partial_and_invalid_config(self):
        self.assertEqual(check_connection({}).code, 'NOT_CONFIGURED')
        self.assertEqual(check_connection({'YDB_ENDPOINT': self.env['YDB_ENDPOINT']}).code,
                         'CONFIG_INCOMPLETE')
        for endpoint in ['grpc://database.example:2135', 'grpcs://user:secret@database.example:2135',
                         'grpcs://database.example:notaport', 'grpcs://database.example:2135/path']:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(check_connection({**self.env, 'YDB_ENDPOINT': endpoint}).code,
                                 'INVALID_CONFIG')

    def test_malformed_or_incomplete_key_json(self):
        for content in ['{invalid', '[]', '{}', json.dumps({'private_key': self.private_key})]:
            with self.subTest(content_type=type(content).__name__):
                result = check_connection({**self.env, 'YDB_SERVICE_ACCOUNT_KEY_JSON': content})
                self.assertEqual(result.code, 'INVALID_KEY_JSON')
                self.assertNotIn(self.private_key, result.message)

    def test_real_sdk_accepts_authorized_key_without_writing_file(self):
        import ydb.iam
        credentials = ydb.iam.ServiceAccountCredentials.from_content(
            self.env['YDB_SERVICE_ACCOUNT_KEY_JSON'])
        self.assertIsInstance(credentials, ydb.iam.ServiceAccountCredentials)
        with patch('ydb.Driver', side_effect=AssertionError('Unexpected network call')):
            invalid = {**self.env, 'YDB_SERVICE_ACCOUNT_KEY_JSON': json.dumps({
                'id': 'key', 'service_account_id': 'account', 'private_key': 'not-a-key'})}
            self.assertEqual(check_connection(invalid).code, 'INVALID_KEY')

    def test_success_runs_only_select_and_closes_resources(self):
        import ydb
        with patch('ydb.Driver') as driver, patch('ydb.QuerySessionPool') as pool:
            pool.return_value.__enter__.return_value.execute_with_retries.return_value = [
                SimpleNamespace(rows=[{'connection_ok': 1}])]
            result = check_connection(self.env)
            self.assertEqual(result.code, 'CONNECTED')
            driver.return_value.__enter__.return_value.wait.assert_called_once_with(
                timeout=15, fail_fast=True)
            execute = pool.return_value.__enter__.return_value.execute_with_retries
            self.assertEqual(execute.call_args.args, ('SELECT 1 AS connection_ok;',))
            self.assertIsInstance(execute.call_args.kwargs['settings'], ydb.BaseRequestSettings)
            driver.return_value.__exit__.assert_called_once()
            pool.return_value.__exit__.assert_called_once()

    def test_connection_failure_never_logs_exception_or_secret(self):
        import ydb
        for error, code in [(TimeoutError(self.private_key), 'TIMEOUT'),
                            (ydb.Unauthorized(self.private_key), 'AUTH_FAILED'),
                            (RuntimeError(self.private_key), 'CONNECTION_FAILED')]:
            with self.subTest(code=code), patch.dict('os.environ', self.env, clear=True), \
                    patch('ydb.Driver', side_effect=error), contextlib.redirect_stdout(io.StringIO()) as output:
                result = log_connection_check()
                self.assertEqual(result.code, code)
                self.assertNotIn('PRIVATE KEY', output.getvalue())
                self.assertIn('YDB check: ' + code, output.getvalue())

    def test_unexpected_query_result_is_not_success(self):
        with patch('ydb.Driver'), patch('ydb.QuerySessionPool') as pool:
            pool.return_value.__enter__.return_value.execute_with_retries.return_value = []
            self.assertEqual(check_connection(self.env).code, 'QUERY_FAILED')


if __name__ == '__main__':
    unittest.main()

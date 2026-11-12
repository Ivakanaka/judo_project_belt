"""Read-only YDB connection check. Credentials stay in server memory."""
import json
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


VARIABLES = ('YDB_ENDPOINT', 'YDB_DATABASE', 'YDB_SERVICE_ACCOUNT_KEY_JSON')


@dataclass(frozen=True)
class CheckResult:
    code: str
    message: str


def check_connection(environ=None):
    """Execute SELECT 1 only; return fixed messages, never SDK exception text."""
    environ = os.environ if environ is None else environ
    values = [environ.get(name, '').strip() for name in VARIABLES]
    if not any(values):
        return CheckResult('NOT_CONFIGURED', 'YDB variables are absent; prototype mode.')
    if not all(values):
        return CheckResult('CONFIG_INCOMPLETE', 'Set all three YDB environment variables.')
    endpoint, database, key_content = values
    try:
        address = urlsplit(endpoint)
        valid_endpoint = (address.scheme == 'grpcs' and address.hostname and address.port
                          and not address.username and not address.password
                          and not address.path and not address.query and not address.fragment)
    except ValueError:
        valid_endpoint = False
    if not valid_endpoint or not database.startswith('/') or any(c.isspace() for c in database):
        return CheckResult('INVALID_CONFIG', 'Check YDB_ENDPOINT and YDB_DATABASE in Layero.')
    try:
        key = json.loads(key_content)
    except ValueError:
        return CheckResult('INVALID_KEY_JSON', 'Paste the complete authorized-key JSON into Layero.')
    if not isinstance(key, dict) or not all(
            isinstance(key.get(name), str) and key[name].strip()
            for name in ('id', 'service_account_id', 'private_key')):
        return CheckResult('INVALID_KEY_JSON', 'Authorized-key JSON needs id, service_account_id and private_key.')

    # SDK diagnostics may include authentication error details. Emit only our fixed messages.
    logging.getLogger('ydb').setLevel(logging.CRITICAL + 1)
    try:
        import ydb
        import ydb.iam
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        return CheckResult('SDK_MISSING', 'Install requirements.txt and rebuild the deployment.')
    try:
        load_pem_private_key(key['private_key'].encode(), password=None)
        credentials = ydb.iam.ServiceAccountCredentials.from_content(key_content)
    except Exception:
        return CheckResult('INVALID_KEY', 'Check the authorized-key JSON and its private key in Layero.')
    try:
        with ydb.Driver(endpoint=endpoint, database=database,
                        credentials=credentials,
                        root_certificates=ydb.load_ydb_root_certificate()) as driver:
            driver.wait(timeout=15, fail_fast=True)
            with ydb.QuerySessionPool(driver, size=1) as pool:
                result = pool.execute_with_retries(
                    'SELECT 1 AS connection_ok;',
                    retry_settings=ydb.RetrySettings(max_retries=0, max_session_acquire_timeout=10),
                    settings=ydb.BaseRequestSettings().with_timeout(10))
                if not result or not result[0].rows or result[0].rows[0]['connection_ok'] != 1:
                    return CheckResult('QUERY_FAILED', 'YDB did not return the expected SELECT 1 result.')
    except (ydb.Unauthenticated, ydb.Unauthorized):
        return CheckResult('AUTH_FAILED', 'Check the service-account key and its role on this database.')
    except (TimeoutError, ydb.Timeout):
        return CheckResult('TIMEOUT', 'Check the endpoint, database path and outbound YDB/IAM connectivity.')
    except Exception:
        return CheckResult('CONNECTION_FAILED', 'Check endpoint, database path, IAM access and outbound connectivity.')
    return CheckResult('CONNECTED', 'SELECT 1 succeeded. Registration storage is not enabled yet.')


def log_connection_check():
    result = check_connection()
    print(f'YDB check: {result.code} - {result.message}', flush=True)
    return result


if __name__ == '__main__':
    result = log_connection_check()
    raise SystemExit(0 if result.code == 'CONNECTED' else 1)

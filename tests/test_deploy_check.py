"""部署检查自身必须只读；旧库不能在检查过程中被悄悄迁移。"""
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tools import deploy_check


class DeploymentCheckTests(unittest.TestCase):
    @staticmethod
    def create_schema(conn):
        for table, columns in deploy_check.REQUIRED_COLUMNS.items():
            conn.execute(f'CREATE TABLE {table} (' + ','.join(
                c + (' INTEGER DEFAULT 0' if c.startswith('credit_') and table == 'users' else ' INTEGER')
                for c in columns) + ')')

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            result = deploy_check.check_database(folder)
            self.assertEqual(result[0]['status'], 'FAIL')
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_old_schema_is_reported_without_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.db'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY)')
            before = path.read_bytes()
            result = deploy_check.check_database(folder)
            self.assertEqual(next(r for r in result if r['id'] == 'schema_users')['status'], 'FAIL')
            self.assertEqual(path.read_bytes(), before)

    def test_current_schema_and_negative_balances(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.db'
            with sqlite3.connect(path) as conn:
                self.create_schema(conn)
                conn.execute('INSERT INTO users(balance_cents,reserved_cents,spent_cents) VALUES(0,0,0)')
            first = deploy_check.check_database(folder)
            self.assertFalse(any(r['status'] == 'FAIL' for r in first))
            with sqlite3.connect(path) as conn:
                conn.execute('UPDATE users SET balance_cents=-3')
            second = deploy_check.check_database(folder)
            self.assertEqual(next(r for r in second if r['id'] == 'balances')['status'], 'FAIL')

    def test_credit_migration_tables_and_columns_are_required_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.db'
            with sqlite3.connect(path) as conn:
                self.create_schema(conn)
                for table in ('credit_ledger', 'referral_identity_signals', 'referral_visits', 'share_credit_rewards'):
                    conn.execute(f'DROP TABLE {table}')
                conn.execute('DROP TABLE quota_reservations')
                conn.execute('CREATE TABLE quota_reservations(user_id,free_units,price_cents,status)')
            before = path.read_bytes()
            result = {r['id']: r for r in deploy_check.check_database(folder)}
            for table in ('credit_ledger', 'referral_identity_signals', 'referral_visits',
                          'share_credit_rewards', 'quota_reservations'):
                self.assertEqual(result['schema_' + table]['status'], 'FAIL')
            self.assertIn('credit_units', result['schema_quota_reservations']['detail'])
            self.assertIn('referral_visit_id', result['schema_quota_reservations']['detail'])
            self.assertEqual(path.read_bytes(), before)

    def test_credit_counters_reject_negative_null_and_noninteger_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.db'
            with sqlite3.connect(path) as conn:
                self.create_schema(conn)
                conn.execute('INSERT INTO users(balance_cents,reserved_cents,spent_cents) VALUES(0,0,0)')
            for column in ('credit_balance', 'credit_reserved', 'credit_spent', 'credit_version'):
                for value in (-1, None, 0.5, 'broken'):
                    with self.subTest(column=column, value=value):
                        with sqlite3.connect(path) as conn:
                            conn.execute(f'UPDATE users SET {column}=?', (value,))
                        before = path.read_bytes()
                        result = {r['id']: r for r in deploy_check.check_database(folder)}
                        self.assertEqual(result['credits']['status'], 'FAIL')
                        self.assertEqual(path.read_bytes(), before)
                    with sqlite3.connect(path) as conn:
                        conn.execute(f'UPDATE users SET {column}=0')
            result = {r['id']: r for r in deploy_check.check_database(folder)}
            self.assertEqual(result['credits']['status'], 'PASS')

    def test_daily_limits_allow_null_zero_and_cap_and_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.db'
            with sqlite3.connect(path) as conn:
                self.create_schema(conn)
                conn.execute('INSERT INTO users(balance_cents,reserved_cents,spent_cents) VALUES(0,0,0)')
            for value, status in ((None, 'PASS'), (0, 'PASS'), (10000, 'PASS'),
                                  (-1, 'FAIL'), (10001, 'FAIL'), (0.5, 'FAIL'), ('broken', 'FAIL')):
                with self.subTest(value=value):
                    with sqlite3.connect(path) as conn:
                        conn.execute('UPDATE users SET daily_limit=?', (value,))
                    result = {r['id']: r for r in deploy_check.check_database(folder)}
                    self.assertEqual(result['user_daily_limits']['status'], status)

    def test_http_checks_both_runtime_and_frontend_headers(self):
        def response(url, timeout):
            result = io.BytesIO(json.dumps({'ok': True, 'version': '1.29.2'}).encode()
                                if url.full_url.endswith('/healthz') else b'<html>1.29.2</html>')
            result.headers = {'X-App-Version': '1.29.2', 'Cache-Control': 'no-store'}
            return result
        with mock.patch.object(deploy_check.request, 'urlopen', side_effect=response):
            self.assertTrue(all(r['status'] == 'PASS' for r in deploy_check.check_http('http://testserver', '1.29.2')))
            self.assertTrue(all(r['status'] == 'FAIL' for r in deploy_check.check_http('http://testserver', 'older')))

    def test_transport_errors_are_neutral(self):
        with mock.patch.object(deploy_check.request, 'urlopen', side_effect=RuntimeError('private credential')):
            result = deploy_check.check_http('http://testserver', '1')
        self.assertTrue(all(r['status'] == 'FAIL' for r in result))
        self.assertNotIn('private', json.dumps(result))

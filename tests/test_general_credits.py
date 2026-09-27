import json
import os
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from fastapi.testclient import TestClient
from tests.test_security_reliability import server, make_request


class GeneralCreditTests(unittest.TestCase):
    uid = 988101

    def setUp(self):
        with server._db_lock:
            conn = server._db()
            for table in ('usage_daily', 'quota_reservations', 'atc_jobs', 'atc_cache',
                          'shares', 'share_submissions', 'blocked_share_sources',
                          'wallet_ledger', 'credit_ledger'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute('DELETE FROM users WHERE id=?', (self.uid,))
            conn.execute('INSERT INTO users(id,email,created_at,balance_cents,credit_balance,daily_limit) '
                         'VALUES(?,?,?,30,4,2)',
                         (self.uid, 'credits@example.test', int(time.time())))
            conn.execute("DELETE FROM app_settings WHERE k IN "
                         "('web_parse_price_cents','transcript_price_cents','free_user_daily')")
            conn.commit()
            conn.close()
        self.user_patch = mock.patch.object(server, 'current_user', return_value={
            'id': self.uid, 'email': 'credits@example.test', 'created_at': 1})
        self.user_patch.start()
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        self.user_patch.stop()
        server.db_exec("DELETE FROM app_settings WHERE k='free_user_daily'")

    def user(self):
        return dict(server.db_exec('SELECT * FROM users WHERE id=?', (self.uid,), 'one'))

    def balances(self):
        row = self.user()
        return tuple(row[k] for k in ('credit_balance', 'credit_reserved', 'credit_spent',
                                      'balance_cents', 'reserved_cents', 'spent_cents'))

    def reserve(self, count=1, partial=False):
        return server.reserve_quota(make_request('/api/parse'), count, partial, 'parse')

    def admin(self, method, suffix, body=None):
        with mock.patch.object(server, '_require_admin'):
            call = getattr(self.client, method)
            kwargs = {'json': body} if body is not None else {}
            return call(f'/api/admin/users/{self.uid}/{suffix}', **kwargs)

    def credit_body(self, **overrides):
        body = {'mode': 'add', 'units': 3, 'expected_version': self.user()['credit_version'],
                'request_id': 'credit-admin-test', 'note': 'test', 'source': 'admin'}
        return {**body, **overrides}

    def test_free_then_credits_then_wallet_with_atomic_reservation(self):
        reservation = self.reserve(8)
        self.assertEqual((reservation['free_units'], reservation['credit_units'],
                          reservation['reserved_cents']), (2, 4, 6))
        self.assertEqual(self.balances(), (0, 4, 0, 24, 6, 0))
        server.settle_quota(reservation, 8)
        self.assertEqual(self.balances(), (0, 0, 4, 24, 0, 6))
        self.assertEqual(server.quota_status(make_request()), (2, 2, 0))

    def test_partial_batch_keeps_free_then_credit_successes_and_refunds_wallet(self):
        reservation = self.reserve(8, partial=True)
        server.settle_quota(reservation, 3)
        server.settle_quota(reservation, 8)
        self.assertEqual(self.balances(), (3, 0, 1, 30, 0, 0))
        self.assertEqual(server.quota_status(make_request()), (2, 2, 0))

    def test_partial_capacity_and_full_failure_refund_all_three_pools(self):
        server.db_exec('UPDATE users SET balance_cents=3 WHERE id=?', (self.uid,))
        reservation = self.reserve(10, partial=True)
        self.assertFalse(reservation['ok'])
        self.assertEqual(reservation['reserved'], 7)
        server.release_quota(reservation)
        server.release_quota(reservation)
        self.assertEqual(self.balances(), (4, 0, 0, 3, 0, 0))
        self.assertEqual(server.quota_status(make_request()), (2, 0, 2))

    def test_failure_within_free_portion_returns_all_credits(self):
        reservation = self.reserve(8)
        server.settle_quota(reservation, 1)
        self.assertEqual(self.balances(), (4, 0, 0, 30, 0, 0))
        self.assertEqual(server.quota_status(make_request()), (2, 1, 1))

    def test_concurrent_requests_do_not_overspend_credits(self):
        server.db_exec('UPDATE users SET daily_limit=0,balance_cents=0 WHERE id=?', (self.uid,))
        with ThreadPoolExecutor(max_workers=12) as pool:
            reservations = list(pool.map(lambda _: self.reserve(), range(30)))
        self.assertEqual(sum(r['ok'] for r in reservations), 4)
        self.assertEqual(self.balances(), (0, 4, 0, 0, 0, 0))
        for reservation in reservations:
            server.settle_quota(reservation, 1)
        self.assertEqual(self.balances(), (0, 0, 4, 0, 0, 0))

    def test_next_day_restores_daily_free_without_resetting_credits(self):
        reservation = self.reserve(3)
        server.settle_quota(reservation, 3)
        tomorrow = server._today() + 1
        with mock.patch.object(server, '_today', return_value=tomorrow):
            self.assertEqual(server.quota_status(make_request()), (2, 0, 2))
            next_reservation = self.reserve(1)
            self.assertEqual(next_reservation['credit_units'], 0)
            server.settle_quota(next_reservation, 1)
            self.assertEqual(self.balances(), (3, 0, 1, 30, 0, 0))

    def test_admin_refund_restores_successful_free_credit_wallet_exactly_once(self):
        reservation = self.reserve(8)
        server.settle_quota(reservation, 7)
        with server._db_lock:
            conn = server._db()
            conn.execute('BEGIN IMMEDIATE')
            self.assertEqual(server._admin_refund_quota_in_conn(conn, reservation['id']), 0)
            self.assertIsNone(server._admin_refund_quota_in_conn(conn, reservation['id']))
            conn.commit()
            conn.close()
        self.assertEqual(self.balances(), (4, 0, 0, 30, 0, 0))
        self.assertEqual(server.quota_status(make_request()), (2, 0, 2))

    def test_stale_reservations_refund_credit_hold(self):
        reservation = self.reserve(8)
        server.db_exec('UPDATE quota_reservations SET lease_until=0 WHERE id=?', (reservation['id'],))
        self.assertEqual(server._refund_stale_quota_reservations(), 1)
        self.assertEqual(server._refund_stale_quota_reservations(), 0)
        self.assertEqual(self.balances(), (4, 0, 0, 30, 0, 0))

    def test_credits_and_override_persist_and_refund_after_process_restart(self):
        reservation = self.reserve(8)
        code = '''
import json,sys,server
data=json.load(sys.stdin)
before=server._web_billing_status(data['uid'])
server.release_quota({'id':data['reservation']})
server.release_quota({'id':data['reservation']})
print(json.dumps({'before':before,'after':server._web_billing_status(data['uid'])}))
'''
        child = subprocess.run([sys.executable, '-c', code],
            input=json.dumps({'uid': self.uid, 'reservation': reservation['id']}),
            text=True, capture_output=True, check=True, timeout=20,
            env=dict(os.environ, DATA_DIR=str(server.DATA_DIR), MIHOMO_OFF='1'))
        result = json.loads(child.stdout)
        self.assertEqual(result['before']['credits']['reserved'], 4)
        self.assertEqual(result['after']['credits']['balance'], 4)
        self.assertEqual(result['after']['credits']['reserved'], 0)
        self.assertEqual(result['after']['user_daily'], 2)

    def test_transcript_reservations_never_spend_general_credits(self):
        with server._db_lock:
            conn = server._db()
            conn.execute('BEGIN IMMEDIATE')
            reservation = server._reserve_quota_in_conn(conn, server._today(),
                [f'atc:user:{self.uid}'], 0, 1, endpoint='atc_transcript')
            conn.commit()
            conn.close()
        self.assertEqual((reservation['free_units'], reservation['credit_units'],
                          reservation['reserved_cents']), (0, 0, 3))
        server.settle_quota(reservation, 1)
        self.assertEqual(self.balances(), (4, 0, 0, 27, 0, 3))

    def test_zero_override_uses_credits_immediately_and_none_restores_global(self):
        result = self.admin('patch', 'quota', {'daily_limit': 0, 'expected_version': 0})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['daily_effective'], 0)
        reservation = self.reserve()
        server.settle_quota(reservation, 1)
        self.assertEqual(self.balances(), (3, 0, 1, 30, 0, 0))
        server.set_app_setting('free_user_daily', '12')
        self.assertEqual(server.free_user_daily(self.uid), 0)
        result = self.admin('patch', 'quota', {'daily_limit': None, 'expected_version': 1})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['daily_effective'], 12)

    def test_daily_edit_preserves_today_usage_and_inflight_free_units(self):
        reservation = self.reserve(2)
        result = self.admin('patch', 'quota', {'daily_limit': 0, 'expected_version': 0})
        self.assertEqual(result.status_code, 200)
        self.assertEqual((result.json()['free_used'], result.json()['free_remaining']), (2, 0))
        server.settle_quota(reservation, 2)
        self.assertEqual(self.balances(), (4, 0, 0, 30, 0, 0))

    def test_reservation_rechecks_individual_and_global_limits_in_its_transaction(self):
        with server._db_lock:
            conn = server._db()
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('UPDATE users SET daily_limit=0 WHERE id=?', (self.uid,))
            reservation = server._reserve_quota_in_conn(conn, server._today(),
                [f'user:{self.uid}'], 100, 1, endpoint='parse')
            conn.commit()
            conn.close()
        self.assertEqual((reservation['limit'], reservation['free_units'], reservation['credit_units']), (0, 0, 1))

    def test_daily_editor_checks_version_and_validates_integer_or_null(self):
        self.assertEqual(self.admin('patch', 'quota', {'daily_limit': 3, 'expected_version': 0}).status_code, 200)
        self.assertEqual(self.admin('patch', 'quota', {'daily_limit': 4, 'expected_version': 0}).status_code, 409)
        for value in (-1, 10001, True, '3', 1.5):
            with self.subTest(value=value):
                response = self.admin('patch', 'quota', {'daily_limit': value, 'expected_version': 1})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(server.free_user_daily(self.uid), 3)

    def test_admin_credit_adjustment_is_idempotent_and_rejects_stale_changes(self):
        payload = self.credit_body()
        first = self.admin('post', 'credits', payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.admin('post', 'credits', payload).status_code, 200)
        self.assertEqual(self.user()['credit_balance'], 7)
        self.assertEqual(self.admin('post', 'credits', {**payload, 'units': 8}).status_code, 409)
        self.assertEqual(self.admin('post', 'credits', {**payload, 'request_id': 'stale-credit'}).status_code, 409)

    def test_admin_purchase_is_recorded_and_requires_positive_add(self):
        result = self.admin('post', 'credits', self.credit_body(source='purchase'))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['ledger'][0]['event'], 'purchase')
        self.assertEqual(result.json()['balance'], 7)
        for mode, units in (('set', 0), ('set', 12), ('add', 0)):
            response = self.admin('post', 'credits', self.credit_body(
                request_id='bad-purchase', source='purchase', mode=mode, units=units))
            self.assertEqual(response.status_code, 400)

    def test_admin_available_credit_edit_cannot_erase_pending_refund(self):
        reservation = self.reserve(4)
        result = self.admin('post', 'credits', self.credit_body(mode='set', units=0))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['reserved'], 2)
        server.release_quota(reservation)
        self.assertEqual((self.user()['credit_balance'], self.user()['credit_reserved']), (2, 0))

    def test_parallel_replayed_admin_purchase_applies_once(self):
        body = server.CreditEditBody(**self.credit_body(source='purchase'))
        with mock.patch.object(server, '_require_admin'), ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: server.admin_edit_credits(self.uid, body, make_request()), range(8)))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(self.user()['credit_balance'], 7)
        self.assertEqual(server.db_exec('SELECT COUNT(*) FROM credit_ledger', (), 'one')[0], 1)

    def test_credit_helper_replay_is_noop_and_mismatched_replay_does_not_mutate(self):
        with server._db_lock:
            conn = server._db()
            conn.execute('BEGIN IMMEDIATE')
            self.assertTrue(server._credit_change(conn, self.uid, 'share_reward', 'same-reward', balance=1))
            self.assertFalse(server._credit_change(conn, self.uid, 'share_reward', 'same-reward', balance=1))
            with self.assertRaises(server.ApiError):
                server._credit_change(conn, self.uid, 'share_reward', 'same-reward', balance=2)
            with self.assertRaises(server.ApiError):
                server._credit_change(conn, self.uid, 'reserve', 'bad-hold', balance=-6, reserved=6)
            conn.commit()
            conn.close()
        self.assertEqual((self.user()['credit_balance'], self.user()['credit_reserved']), (5, 0))

    def test_invalid_credit_amounts_and_unauthorized_changes_do_not_mutate(self):
        for value in (-1, 1000000001, True, '3', 0.5):
            with self.subTest(value=value):
                response = self.admin('post', 'credits', self.credit_body(units=value))
                self.assertEqual(response.status_code, 422)
        for suffix in ('quota', 'credits'):
            self.assertEqual(self.client.get(f'/api/admin/users/{self.uid}/{suffix}').status_code, 401)
        self.assertEqual(self.client.patch(f'/api/admin/users/{self.uid}/quota',
            json={'daily_limit': 0, 'expected_version': 0}).status_code, 401)
        self.assertEqual(self.client.post(f'/api/admin/users/{self.uid}/credits', json=self.credit_body()).status_code, 401)
        self.assertEqual(self.user()['credit_balance'], 4)

    def test_quota_auth_and_admin_list_show_separate_daily_and_credit_totals(self):
        reservation = self.reserve(3)
        server.settle_quota(reservation, 3)
        quota = self.client.get('/api/quota').json()
        self.assertEqual((quota['limit'], quota['used'], quota['remaining']), (2, 2, 0))
        self.assertEqual((quota['credits']['balance'], quota['credits']['spent']), (3, 1))
        self.assertEqual(quota['credits']['name'], '通用额度')
        me = self.client.get('/api/auth/me').json()
        self.assertEqual(me['credits'], quota['credits'])
        with mock.patch.object(server, '_require_admin'):
            users = self.client.get('/api/admin/users').json()['users']
        user = next(u for u in users if u['id'] == self.uid)
        self.assertEqual((user['daily_limit'], user['daily_effective'], user['free_used'],
                          user['free_remaining'], user['credit_balance']), (2, 2, 2, 0, 3))
        with mock.patch.object(server, 'current_user', return_value=None):
            anonymous = self.client.get('/api/quota').json()
        self.assertIsNone(anonymous['credits'])
        self.assertEqual(anonymous['limit'], server.FREE_ANON_DAILY)

    def test_parse_failure_and_batch_partial_success_restore_credits(self):
        server.db_exec('UPDATE users SET daily_limit=0,balance_cents=0 WHERE id=?', (self.uid,))
        with mock.patch.object(server, '_parse_cached', side_effect=server.ApiError(502, 'test failure')):
            failed = self.client.post('/api/parse', json={'text': 'https://v.douyin.com/creditFailure/'})
        self.assertEqual(failed.status_code, 502)
        self.assertEqual(self.user()['credit_balance'], 4)
        with mock.patch.object(server, '_parse_cached', side_effect=[{'title': 'ok'}, server.ApiError(502, 'failed')]):
            result = self.client.post('/api/parse/batch', json={
                'text': 'https://v.douyin.com/creditA/ https://v.douyin.com/creditB/'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([item['ok'] for item in result.json()['results']], [True, False])
        self.assertEqual(self.balances(), (3, 0, 1, 0, 0, 0))

    def test_async_share_idempotency_reserves_credits_once_then_refunds(self):
        server.db_exec('UPDATE users SET daily_limit=0,balance_cents=0 WHERE id=?', (self.uid,))
        headers = {'Idempotency-Key': 'credit-share-test'}
        with mock.patch.object(server, '_share_origin', return_value='https://example.test'):
            a = self.client.post('/api/shares', headers=headers, json={'text': 'https://v.douyin.com/creditShare/'})
            b = self.client.post('/api/shares', headers=headers, json={'text': 'https://v.douyin.com/creditShare/'})
        self.assertEqual(a.status_code, 202, a.text)
        self.assertEqual(b.status_code, 202, b.text)
        self.assertEqual(self.balances(), (3, 1, 0, 0, 0, 0))
        row = server.db_exec('SELECT quota_reservation_id FROM shares', (), 'one')
        server.release_quota({'id': row['quota_reservation_id']})
        self.assertEqual(self.balances(), (4, 0, 0, 0, 0, 0))


if __name__ == '__main__':
    unittest.main()

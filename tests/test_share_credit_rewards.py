"""分享奖励的真实会话归因、交易幂等与防重复回归；不调用任何上游。"""
import json
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from unittest import mock

from tests.test_security_reliability import server, TestClient, make_request


class ShareCreditRewardTests(unittest.TestCase):
    item_id = '7683504400719073115'

    def setUp(self):
        self.clients = []
        self.user_ids = []
        self.share_ids = []
        self.ip_patch = mock.patch.object(server, '_client_ip', side_effect=lambda request:
            request.headers.get('x-test-network', '8.250.250.250'))
        self.ip_patch.start()
        self.owner = self.actor('8.11.1.10')
        self.sid = self.share(self.owner)

    def tearDown(self):
        for client, _, _ in self.clients:
            client.close()
        self.ip_patch.stop()
        # 仅清理由本组创建的账号；风控事实表没有外键级联时也不污染其他用例。
        with server._db_lock:
            conn = server._db()
            for sid in self.share_ids:
                conn.execute('DELETE FROM shares WHERE id=?', (sid,))
                conn.execute('DELETE FROM share_events WHERE sid=?', (sid,))
            for uid in self.user_ids:
                conn.execute('DELETE FROM share_credit_rewards WHERE owner_user_id=? OR visitor_user_id=?', (uid, uid))
                conn.execute('DELETE FROM referral_visits WHERE owner_user_id=? OR visitor_user_id=?', (uid, uid))
                conn.execute('DELETE FROM referral_identity_signals WHERE user_id=?', (uid,))
                conn.execute('DELETE FROM user_sessions WHERE user_id=?', (uid,))
                conn.execute('DELETE FROM quota_reservations WHERE user_id=?', (uid,))
                conn.execute('DELETE FROM credit_ledger WHERE user_id=?', (uid,))
                conn.execute('DELETE FROM wallet_ledger WHERE user_id=?', (uid,))
                conn.execute('DELETE FROM users WHERE id=?', (uid,))
                conn.execute('DELETE FROM usage_daily WHERE subject=?', (f'user:{uid}',))
            conn.commit()
            conn.close()

    def request(self, actor, path='/api/parse'):
        client, _, ip = actor
        cookies = '; '.join(f'{key}={value}' for key, value in client.cookies.items())
        return make_request(path, client_ip=ip,
                            headers={'Cookie': cookies, 'X-Test-Network': ip})

    def actor(self, ip, login=True, cookies=None):
        client = TestClient(server.app, headers={'X-Test-Network': ip})
        if cookies:
            client.cookies.update(cookies)
        client.get('/')
        uid = None
        actor = (client, uid, ip)
        if login:
            actor = self.login(actor, self.new_user())
        self.clients.append(actor)
        return actor

    def new_user(self):
        uid = server.db_exec(
            'INSERT INTO users(email,created_at,last_login,balance_cents) VALUES(?,?,?,?)',
            (f'referral-{uuid.uuid4().hex}@example.test', int(time.time()) - 86400,
             int(time.time()), 1000))
        self.user_ids.append(uid)
        return uid

    def login(self, actor, uid):
        client, _, ip = actor
        response = server._issue_session(uid, self.request(actor, '/api/auth/login'))
        for header in response.headers.getlist('set-cookie'):
            parsed = SimpleCookie()
            parsed.load(header)
            for name, cookie in parsed.items():
                client.cookies.set(name, cookie.value, domain='testserver.local', path='/')
        return client, uid, ip

    def replace_cookie(self, actor, name, value):
        actor[0].cookies.set(name, value, domain='testserver.local', path='/')

    def data(self, item_id=None):
        return {'kind': 'video', 'item_id': item_id or self.item_id,
                'title': '分享奖励测试作品', 'platform': 'douyin', 'source': 'douyin_direct',
                'video': {'source': 'douyin_direct', 'filename': 'test.mp4'},
                'images': []}

    def share(self, owner, item_id=None):
        view = server._share_create(self.request(owner, '/api/share'), self.data(item_id))
        sid = view['sid']
        self.share_ids.append(sid)
        return sid

    def balance(self, actor=None):
        actor = actor or self.owner
        row = server.db_exec('SELECT credit_balance FROM users WHERE id=?', (actor[1],), 'one')
        return int(row['credit_balance'])

    def visit(self, visitor, sid=None):
        result = visitor[0].get('/s/' + (sid or self.sid))
        self.assertEqual(result.status_code, 200, result.text[:300])
        return result

    def parse(self, visitor, success=True):
        kwargs = ({'return_value': self.data()} if success else
                  {'side_effect': server.ApiError(502, 'test parse failed')})
        with mock.patch.object(server, '_parse_cached', **kwargs):
            result = visitor[0].post('/api/parse', json={'text': 'https://v.douyin.com/test/'})
        self.assertEqual(result.status_code, 200 if success else 502, result.text[:300])
        return result

    def test_distinct_logged_in_visitor_success_credits_owner_once(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        self.assertEqual(self.balance(), 0)
        self.parse(visitor)
        self.assertEqual(self.balance(), 1)
        self.assertEqual(self.balance(visitor), 0)
        self.parse(visitor)
        self.assertEqual(self.balance(), 1)
        ledger = server.db_exec('SELECT * FROM credit_ledger WHERE user_id=?',
                                (self.owner[1],), 'all')
        self.assertEqual(sum(row['balance_delta'] for row in ledger), 1)

    def test_failed_parse_refunds_visitor_without_reward_then_success_can_qualify(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        before = server.quota_status(self.request(visitor))
        self.parse(visitor, success=False)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(server.quota_status(self.request(visitor)), before)
        self.parse(visitor)
        self.assertEqual(self.balance(), 1)

    def test_anonymous_usage_and_unattributed_logged_in_usage_do_not_reward(self):
        anonymous = self.actor('8.12.1.10', login=False)
        self.visit(anonymous)
        self.parse(anonymous)
        self.parse(self.actor('8.13.1.10'))
        self.assertEqual(self.balance(), 0)

    def test_self_visit_and_same_network_other_account_do_not_reward(self):
        self.visit(self.owner)
        self.parse(self.owner)
        neighbor = self.actor('8.11.1.200')
        self.visit(neighbor)
        self.parse(neighbor)
        self.assertEqual(self.balance(), 0)

    def test_page_fetch_polling_and_client_events_never_award_credit(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        for _ in range(3):
            visitor[0].get('/api/share/' + self.sid)
            visitor[0].get('/api/shares/' + self.sid)
        for kind in ('page_view', 'play', 'download', 'cta_click'):
            visitor[0].post('/api/share/' + self.sid + '/event', json={
                'kind': kind, 'page_view_id': str(uuid.uuid4())})
        self.assertEqual(self.balance(), 0)

    def test_five_reward_cap_is_shared_by_recreated_links_for_same_work(self):
        duplicate_sid = self.share(self.owner)
        for index in range(7):
            visitor = self.actor(f'8.12.{index + 1}.10')
            self.visit(visitor, self.sid if index % 2 else duplicate_sid)
            self.parse(visitor)
        self.assertEqual(self.balance(), 5)

    def test_same_visitor_cannot_award_same_owner_again_on_another_work(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        self.parse(visitor)
        other_sid = self.share(self.owner, '7683504400719073999')
        self.visit(visitor, other_sid)
        self.parse(visitor)
        self.assertEqual(self.balance(), 1)

    def test_same_network_new_accounts_cannot_repeat_conversion(self):
        first = self.actor('8.12.1.10')
        second = self.actor('8.12.1.200')
        self.visit(first)
        self.parse(first)
        self.visit(second)
        self.parse(second)
        self.assertEqual(self.balance(), 1)

    def test_invalid_share_state_at_settlement_cannot_award(self):
        for state in ('takedown', 'expired'):
            visitor = self.actor(f'8.12.{len(self.clients)}.10')
            sid = self.share(self.owner, str(int(self.item_id) + len(self.clients)))
            self.visit(visitor, sid)
            reservation = server.reserve_quota(self.request(visitor), 1, endpoint='parse')
            self.assertTrue(reservation['ok'])
            if state == 'takedown':
                server.db_exec("UPDATE shares SET status='takedown' WHERE id=?", (sid,))
            else:
                server.db_exec('UPDATE shares SET expires_at=? WHERE id=?', (int(time.time()) - 1, sid))
            server.settle_quota(reservation, 1)
        self.assertEqual(self.balance(), 0)

    def test_concurrent_settlement_cannot_exceed_five_or_duplicate_rewards(self):
        reservations = []
        for index in range(8):
            visitor = self.actor(f'8.12.{index + 1}.10')
            self.visit(visitor)
            reservations.append(server.reserve_quota(self.request(visitor), 1, endpoint='parse'))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda reservation: server.settle_quota(reservation, 1), reservations * 2))
        self.assertEqual(self.balance(), 5)

    def test_batch_successes_reward_one_conversion_only(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        reservation = server.reserve_quota(self.request(visitor), 3, endpoint='parse_batch')
        server.settle_quota(reservation, 3)
        self.assertEqual(self.balance(), 1)

    def test_transcript_reservation_does_not_qualify_as_parse_conversion(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        reservation = server.reserve_quota(self.request(visitor), 1, endpoint='atc_transcript')
        server.settle_quota(reservation, 1)
        self.assertEqual(self.balance(), 0)

    def test_deleting_original_share_does_not_reset_reward_cap(self):
        for index in range(5):
            visitor = self.actor(f'8.12.{index + 1}.10')
            self.visit(visitor)
            self.parse(visitor)
        server.db_exec('DELETE FROM shares WHERE id=?', (self.sid,))
        replacement = self.share(self.owner)
        visitor = self.actor('8.13.1.10')
        self.visit(visitor, replacement)
        self.parse(visitor)
        self.assertEqual(self.balance(), 5)

    def test_anonymous_visit_can_bind_to_first_login_then_qualify(self):
        visitor = self.actor('8.12.1.10', login=False)
        self.visit(visitor)
        visitor = self.login(visitor, self.new_user())
        self.parse(visitor)
        self.assertEqual(self.balance(), 1)

    def test_bound_visit_cannot_be_reassigned_by_switching_accounts(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        visitor = self.login(visitor, self.new_user())
        self.parse(visitor)
        self.assertEqual(self.balance(), 0)

    def test_same_browser_owner_cookie_cannot_qualify_even_on_new_network(self):
        visitor = self.actor('8.12.1.10', cookies={
            'reward_browser': self.owner[0].cookies.get('reward_browser')})
        self.visit(visitor)
        self.parse(visitor)
        self.assertEqual(self.balance(), 0)

    def test_tampered_browser_or_referral_cookie_cannot_award(self):
        for name in ('reward_browser', 'share_referral'):
            visitor = self.actor(f'8.12.{len(self.clients)}.10')
            self.visit(visitor)
            cookie = visitor[0].cookies.get(name)
            self.assertTrue(cookie)
            self.replace_cookie(visitor, name, cookie + 'x')
            self.parse(visitor)
        self.assertEqual(self.balance(), 0)

    def test_referral_cookie_copied_to_other_device_does_not_award(self):
        visitor = self.actor('8.12.1.10')
        thief = self.actor('8.13.1.10')
        self.visit(visitor)
        self.replace_cookie(thief, 'share_referral', visitor[0].cookies.get('share_referral'))
        self.parse(thief)
        self.assertEqual(self.balance(), 0)

    def test_attribution_network_change_and_expired_visit_do_not_award(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        visitor[0].headers['X-Test-Network'] = '8.13.1.10'
        self.parse(visitor)
        visitor[0].headers['X-Test-Network'] = '8.12.1.10'
        server.db_exec('UPDATE referral_visits SET expires_at=? WHERE visitor_user_id=?',
                       (int(time.time()) - 1, visitor[1]))
        self.parse(visitor)
        self.assertEqual(self.balance(), 0)

    def test_ipv6_network_and_ipv4_mapped_addresses_cannot_bypass_network_check(self):
        mapped_owner = self.actor('::ffff:8.11.1.200')
        self.visit(mapped_owner)
        self.parse(mapped_owner)
        self.assertEqual(self.balance(), 0)
        first = self.actor('2606:4700:1234:1234::1')
        second = self.actor('2606:4700:1234:1234::ffff')
        self.visit(first)
        self.parse(first)
        self.visit(second)
        self.parse(second)
        self.assertEqual(self.balance(), 1)

    def test_disabled_owner_at_settlement_prevents_award(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        reservation = server.reserve_quota(self.request(visitor), 1, endpoint='parse')
        server.db_exec('UPDATE users SET disabled=1 WHERE id=?', (self.owner[1],))
        server.settle_quota(reservation, 1)
        self.assertEqual(self.balance(), 0)

    def test_financial_reward_deduplication_survives_retention_cleanup(self):
        for index in range(5):
            visitor = self.actor(f'8.12.{index + 1}.10')
            self.visit(visitor)
            self.parse(visitor)
        old = int(time.time()) - (server.DATA_RETENTION_DAYS + 2) * 86400
        server.db_exec('UPDATE share_credit_rewards SET created=? WHERE owner_user_id=?',
                       (old, self.owner[1]))
        server.db_exec('UPDATE credit_ledger SET ts=? WHERE user_id=?', (old, self.owner[1]))
        server._cleanup_retained_data(force=True)
        visitor = self.actor('8.13.1.10')
        self.visit(visitor)
        self.parse(visitor)
        self.assertEqual(self.balance(), 5)

    def test_reward_records_store_hmac_summaries_instead_of_ip_or_browser_token(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        self.parse(visitor)
        raw = json.dumps([dict(row) for row in server.db_exec(
            'SELECT * FROM share_credit_rewards WHERE owner_user_id=?', (self.owner[1],), 'all')])
        self.assertNotIn('8.12.1.10', raw)
        self.assertNotIn('8.11.1.10', raw)
        self.assertNotIn(visitor[0].cookies.get('reward_browser'), raw)
        self.assertNotIn(visitor[0].cookies.get('share_referral'), raw)

    def test_reward_browser_and_attribution_cookies_are_httponly(self):
        visitor = self.actor('8.12.1.10', login=False)
        self.assertTrue(visitor[0].cookies.get('reward_browser'))
        response = self.visit(visitor)
        headers = [header.lower() for header in response.headers.get_list('set-cookie')]
        referral = next(header for header in headers if header.startswith('share_referral='))
        self.assertIn('httponly', referral)
        self.assertIn('samesite=lax', referral)

    def test_cached_share_creation_does_not_reward_without_new_quota_use(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        response = visitor[0].post('/api/share', json={'item_id': self.item_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.share_ids.append(response.json()['sid'])
        self.assertEqual(self.balance(), 0)

    def test_async_share_success_uses_persisted_attribution_and_rewards_once(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        with mock.patch.object(server, '_wake_share_parse_workers'):
            response = visitor[0].post('/api/shares', json={
                'text': 'https://v.douyin.com/RewardTest/'},
                headers={'Idempotency-Key': 'reward-async-' + uuid.uuid4().hex})
        self.assertEqual(response.status_code, 202, response.text)
        sid = response.json()['data']['sid']
        self.share_ids.append(sid)
        self.assertEqual(self.balance(), 0)
        item = server._claim_share_parse('referral-test-worker')
        self.assertEqual(item['id'], sid)
        data = self.data()
        data['video']['proxy_url'] = '/api/douyin/video/test-media'
        self.assertTrue(server._finish_share_parse_success(item, data))
        self.assertEqual(self.balance(), 1)
        self.assertFalse(server._finish_share_parse_success(item, data))
        self.assertEqual(self.balance(), 1)

    def test_homepage_referral_entry_can_attribute_success(self):
        visitor = self.actor('8.12.1.10')
        result = visitor[0].get('/', params={'ref': self.sid})
        self.assertEqual(result.status_code, 200)
        self.parse(visitor)
        self.assertEqual(self.balance(), 1)

    def test_cap_is_per_work_instead_of_global_owner_balance(self):
        for index in range(5):
            visitor = self.actor(f'8.12.{index + 1}.10')
            self.visit(visitor)
            self.parse(visitor)
        other = self.share(self.owner, '7683504400719073999')
        visitor = self.actor('8.13.1.10')
        self.visit(visitor, other)
        self.parse(visitor)
        self.assertEqual(self.balance(), 6)

    def test_missing_owner_history_and_invalid_client_ip_fail_reward_eligibility(self):
        unknown = self.actor('not-an-ip')
        self.visit(unknown)
        self.parse(unknown)
        server.db_exec('DELETE FROM referral_identity_signals WHERE user_id=?', (self.owner[1],))
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        self.parse(visitor)
        self.assertEqual(self.balance(), 0)

    def test_malformed_non_ascii_referral_signature_is_ignored_safely(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        request = self.request(visitor)
        visit_id = server._referral_visit_id(request)
        for suffix in ('', 'é' * 64, 'a' * 10000):
            malformed = make_request(headers={'Cookie': 'share_referral=' + visit_id + '.' + suffix})
            self.assertEqual(server._referral_visit_id(malformed), '')

    def test_changed_work_identity_before_settlement_does_not_award(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        reservation = server.reserve_quota(self.request(visitor), 1, endpoint='parse')
        server.db_exec('UPDATE shares SET item_id=? WHERE id=?',
                       ('7683504400719073999', self.sid))
        server.settle_quota(reservation, 1)
        self.assertEqual(self.balance(), 0)

    def test_reward_failure_rolls_back_conversion_and_can_retry_atomically(self):
        visitor = self.actor('8.12.1.10')
        self.visit(visitor)
        reservation = server.reserve_quota(self.request(visitor), 1, endpoint='parse')
        with mock.patch.object(server, '_credit_change', side_effect=RuntimeError('injected ledger failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected ledger failure'):
                server.settle_quota(reservation, 1)
        self.assertEqual(self.balance(), 0)
        row = server.db_exec('SELECT status FROM quota_reservations WHERE id=?', (reservation['id'],), 'one')
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(server.db_exec('SELECT COUNT(*) n FROM share_credit_rewards WHERE owner_user_id=?',
                                       (self.owner[1],), 'one')['n'], 0)
        server.settle_quota(reservation, 1)
        self.assertEqual(self.balance(), 1)


if __name__ == '__main__':
    unittest.main()

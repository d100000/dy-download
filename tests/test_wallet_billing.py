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


class WalletBillingTests(unittest.TestCase):
    uid = 988001

    def setUp(self):
        with server._db_lock:
            conn = server._db()
            for table in ('usage_daily', 'quota_reservations', 'atc_jobs', 'atc_cache',
                          'shares', 'share_submissions', 'blocked_share_sources', 'wallet_ledger'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute('DELETE FROM users WHERE id=?', (self.uid,))
            conn.execute('INSERT INTO users(id,email,created_at,balance_cents) VALUES(?,?,?,?)',
                         (self.uid, 'wallet-test@example.test', int(time.time()), 30))
            conn.execute("DELETE FROM app_settings WHERE k IN ('web_parse_price_cents','transcript_price_cents','free_user_daily')")
            conn.commit()
            conn.close()
        self.user_patch = mock.patch.object(server, 'current_user', return_value={
            'id': self.uid, 'email': 'wallet-test@example.test', 'created_at': 1})
        self.user_patch.start()
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        self.user_patch.stop()
        server.db_exec("DELETE FROM app_settings WHERE k='free_user_daily'")

    def wallet(self):
        return dict(server.db_exec('SELECT * FROM users WHERE id=?', (self.uid,), 'one'))

    def exhaust(self, text=False):
        subject = ('atc:' if text else '') + f'user:{self.uid}'
        limit = server._atc_cfg()['transcript_daily'] if text else server.free_user_daily()
        server.db_exec('INSERT OR REPLACE INTO usage_daily(day,subject,count) VALUES(?,?,?)',
                       (server._today(), subject, limit))

    def reserve(self, n=1, partial=False):
        return server.reserve_quota(make_request('/api/parse'), n, partial, 'parse')

    def admin_post(self, path, data):
        with mock.patch.object(server, '_require_admin'):
            return self.client.post(path, json=data)

    def test_daily_free_then_paid_and_no_negative_balance(self):
        free = self.reserve(server.FREE_USER_DAILY)
        self.assertTrue(free['ok'])
        self.assertEqual(free['reserved_cents'], 0)
        server.settle_quota(free, server.FREE_USER_DAILY)
        paid = self.reserve()
        self.assertEqual((self.wallet()['balance_cents'], self.wallet()['reserved_cents']), (27, 3))
        server.settle_quota(paid, 1)
        self.assertEqual((self.wallet()['reserved_cents'], self.wallet()['spent_cents']), (0, 3))
        self.assertEqual(server.quota_status(make_request())[1], server.FREE_USER_DAILY)

    def test_concurrent_reservation_cannot_overspend(self):
        self.exhaust()
        with ThreadPoolExecutor(max_workers=20) as pool:
            reservations = list(pool.map(lambda _: self.reserve(), range(30)))
        self.assertEqual(sum(r['ok'] for r in reservations), 10)
        self.assertEqual((self.wallet()['balance_cents'], self.wallet()['reserved_cents']), (0, 30))
        for r in reservations:
            server.settle_quota(r, 1)
        self.assertEqual((self.wallet()['balance_cents'], self.wallet()['spent_cents']), (0, 30))

    def test_partial_batch_consumes_free_successes_first_and_refunds_failures(self):
        self.exhaust()
        server.db_exec('UPDATE usage_daily SET count=count-2')
        r = self.reserve(5, True)
        self.assertEqual((r['free_units'], r['reserved_cents']), (2, 9))
        server.settle_quota(r, 3)
        server.settle_quota(r, 5)  # 同一结算不可重复收费
        self.assertEqual((self.wallet()['balance_cents'], self.wallet()['spent_cents']), (27, 3))
        self.assertEqual(server.quota_status(make_request())[2], 0)

    def test_price_is_snapshotted_and_refund_is_idempotent(self):
        self.exhaust()
        r = self.reserve(2)
        server.set_app_setting('web_parse_price_cents', '20')
        server.settle_quota(r, 1)
        self.assertEqual(self.wallet()['spent_cents'], 3)
        with server._db_lock:
            conn = server._db()
            conn.execute('BEGIN IMMEDIATE')
            server._admin_refund_quota_in_conn(conn, r['id'])
            server._admin_refund_quota_in_conn(conn, r['id'])
            conn.commit(); conn.close()
        self.assertEqual((self.wallet()['balance_cents'], self.wallet()['spent_cents']), (30, 0))

    def test_zero_price_is_free_after_daily_allowance(self):
        self.exhaust()
        server.set_app_setting('web_parse_price_cents', '0')
        server.db_exec('UPDATE users SET balance_cents=0 WHERE id=?', (self.uid,))
        r = self.reserve(50)
        self.assertTrue(r['ok'])
        server.settle_quota(r, 50)
        self.assertEqual(self.wallet()['spent_cents'], 0)

    def test_anonymous_user_still_has_only_daily_free_quota(self):
        with mock.patch.object(server, 'current_user', return_value=None):
            server.set_app_setting('web_parse_price_cents', '0')
            self.assertTrue(self.reserve(server.FREE_ANON_DAILY)['ok'])
            self.assertFalse(self.reserve()['ok'])

    def test_failed_parse_refunds_and_success_charges_http(self):
        self.exhaust()
        with mock.patch.object(server, '_parse_cached', side_effect=server.ApiError(502, 'test failure')):
            self.assertEqual(self.client.post('/api/parse', json={'text': 'https://v.douyin.com/test/'}).status_code, 502)
        self.assertEqual(self.wallet()['balance_cents'], 30)
        with mock.patch.object(server, '_parse_cached', return_value={'title': 'ok'}):
            self.assertEqual(self.client.post('/api/parse', json={'text': 'https://v.douyin.com/test/'}).status_code, 200)
        self.assertEqual(self.wallet()['balance_cents'], 27)

    def test_insufficient_funds_rejects_before_upstream(self):
        self.exhaust()
        server.db_exec('UPDATE users SET balance_cents=2 WHERE id=?', (self.uid,))
        with mock.patch.object(server, '_parse_cached') as parse:
            r = self.client.post('/api/parse', json={'text': 'https://v.douyin.com/test/'})
        self.assertEqual(r.status_code, 402)
        parse.assert_not_called()
        self.assertEqual(self.wallet()['balance_cents'], 2)

    def test_batch_endpoint_partial_success_and_over_budget(self):
        self.exhaust()
        server.db_exec('UPDATE users SET balance_cents=6 WHERE id=?', (self.uid,))
        with mock.patch.object(server, '_parse_cached', side_effect=[{'title':'ok'}, server.ApiError(502,'failed')]):
            r = self.client.post('/api/parse/batch', json={'text': '\n'.join(
                f'https://v.douyin.com/test{i}/' for i in range(3))})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x['ok'] for x in r.json()['results']], [True, False, False])
        self.assertEqual((self.wallet()['balance_cents'], self.wallet()['spent_cents']), (3, 3))

    def test_admin_endpoints_require_admin_even_when_user_logged_in(self):
        self.assertEqual(self.client.get(f'/api/admin/users/{self.uid}/wallet').status_code, 401)
        self.assertEqual(self.client.get('/api/admin/billing-prices').status_code, 401)
        r=self.client.post(f'/api/admin/users/{self.uid}/wallet',json={
            'mode':'add','cents':100,'expected_version':0,'request_id':'unauthorized'})
        self.assertEqual(r.status_code,401)
        self.assertEqual(self.wallet()['balance_cents'],30)

    def test_admin_credit_retry_edit_conflict_and_set(self):
        url=f'/api/admin/users/{self.uid}/wallet'
        payload={'mode':'add','cents':100,'expected_version':0,'request_id':'same-credit'}
        self.assertEqual(self.admin_post(url,payload).status_code,200)
        self.assertEqual(self.admin_post(url,payload).status_code,200)
        self.assertEqual(self.wallet()['balance_cents'],130)
        self.assertEqual(self.admin_post(url,{**payload,'cents':200}).status_code,409)
        self.assertEqual(self.admin_post(url,{**payload,'request_id':'stale-edit'}).status_code,409)
        self.assertEqual(self.admin_post(url,{'mode':'set','cents':5,'expected_version':1,'request_id':'set-balance'}).status_code,200)
        self.assertEqual(self.wallet()['balance_cents'],5)

    def test_admin_edit_preserves_frozen_task_refund(self):
        self.exhaust();r=self.reserve()
        result=self.admin_post(f'/api/admin/users/{self.uid}/wallet',{
            'mode':'set','cents':0,'expected_version':self.wallet()['wallet_version'],'request_id':'zero-available'})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.wallet()['reserved_cents'],3)
        server.release_quota(r)
        self.assertEqual((self.wallet()['balance_cents'],self.wallet()['reserved_cents']),(3,0))

    def test_invalid_money_and_price_never_mutate_balance(self):
        url=f'/api/admin/users/{self.uid}/wallet'
        for amount in (-1, 0.3, True, '3', 1000000001):
            with self.subTest(amount=amount):
                self.assertEqual(self.admin_post(url,{'mode':'add','cents':amount,'expected_version':0,'request_id':'bad-money'}).status_code,422)
        self.assertEqual(self.wallet()['balance_cents'],30)
        self.assertEqual(self.admin_post('/api/admin/billing-prices',{'parse_price_cents':3,'transcript_price_cents':-1}).status_code,422)
        self.assertEqual(self.admin_post('/api/admin/billing-prices',{'parse_price_cents':3,'transcript_price_cents':8}).status_code,200)
        self.assertEqual(server._web_billing_status()['transcript_price_cents'],8)

    def test_orphan_reservation_refunded_after_restart_timeout(self):
        self.exhaust();r=self.reserve()
        server.db_exec('UPDATE quota_reservations SET lease_until=0 WHERE id=?',(r['id'],))
        self.assertEqual(server._refund_stale_quota_reservations(),1)
        self.assertEqual(server._refund_stale_quota_reservations(),0)
        self.assertEqual(self.wallet()['balance_cents'],30)

    def enable_transcript(self):
        for k,v in {'atc_enabled':'1','atc_api_key':'test-key','atc_api_secret':'test-secret',
                    'atc_transcript_enabled':'1','atc_transcript_daily':'1'}.items():
            server.set_app_setting(k,v)
        self.exhaust(text=True)

    def test_transcript_reservation_settles_only_at_terminal_state(self):
        self.enable_transcript()
        server.set_app_setting('transcript_price_cents','7')
        r=self.client.post('/api/transcript',json={'item_id':'7670572727590577894'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual((self.wallet()['balance_cents'],self.wallet()['reserved_cents'],self.wallet()['spent_cents']),(23,7,0))
        again=self.client.post('/api/transcript',json={'item_id':'7670572727590577894'})
        self.assertEqual(again.json()['state'],'processing')
        self.assertEqual(self.wallet()['balance_cents'],23)
        job=server._atc_claim_pending('wallet-test')
        self.assertTrue(server._atc_claim_update(job,'done'))
        self.assertFalse(server._atc_claim_update(job,'done'))
        self.assertEqual((self.wallet()['reserved_cents'],self.wallet()['spent_cents']),(0,7))

    def test_transcript_failure_and_startup_timeout_refund(self):
        self.enable_transcript()
        self.client.post('/api/transcript',json={'item_id':'7670572727590577894'})
        job=server._atc_claim_pending('wallet-test')
        server._atc_claim_update(job,'failed',error='test')
        self.assertEqual(self.wallet()['balance_cents'],30)
        self.client.post('/api/transcript',json={'item_id':'7670572727590577895'})
        server.db_exec('UPDATE atc_jobs SET created=0')
        server._prepare_atc_jobs()
        self.assertEqual((self.wallet()['balance_cents'],self.wallet()['reserved_cents']),(30,0))

    def test_active_transcript_is_not_refunded_by_quota_sweeper(self):
        self.enable_transcript()
        self.client.post('/api/transcript',json={'item_id':'7670572727590577894'})
        server.db_exec('UPDATE quota_reservations SET lease_until=0')
        self.assertEqual(server._refund_stale_quota_reservations(),0)
        self.assertEqual(self.wallet()['reserved_cents'],3)

    def test_disabled_service_still_refunds_timed_out_transcript_in_cleanup(self):
        self.enable_transcript()
        self.client.post('/api/transcript',json={'item_id':'7670572727590577894'})
        server.set_app_setting('atc_enabled','0')
        server.db_exec('UPDATE atc_jobs SET created=0')
        server._atc_cleanup()
        self.assertEqual((self.wallet()['balance_cents'],self.wallet()['reserved_cents']),(30,0))

    def test_transcript_cache_does_not_charge(self):
        self.enable_transcript()
        server._atc_save_result('7670572727590577894',{'textContent':'cached words'},include_text=True)
        result=self.client.post('/api/transcript',json={'item_id':'7670572727590577894'})
        self.assertEqual(result.json()['state'],'ready')
        self.assertEqual(self.wallet()['balance_cents'],30)

    def test_async_share_idempotency_reserves_once_and_refunds(self):
        self.exhaust()
        headers={'Idempotency-Key':'wallet-share-test'}
        with mock.patch.object(server,'_share_origin',return_value='https://example.test'):
            a=self.client.post('/api/shares',headers=headers,json={'text':'https://v.douyin.com/walletTest/'})
            b=self.client.post('/api/shares',headers=headers,json={'text':'https://v.douyin.com/walletTest/'})
        self.assertEqual(a.status_code,202,a.text)
        self.assertEqual(b.status_code,202,b.text)
        self.assertEqual(self.wallet()['balance_cents'],27)
        row=server.db_exec('SELECT quota_reservation_id FROM shares',(),'one')
        with server._db_lock:
            conn=server._db();conn.execute('BEGIN IMMEDIATE')
            server._settle_quota_in_conn(conn,row[0],0)
            conn.commit();conn.close()
        self.assertEqual(self.wallet()['balance_cents'],30)

    def test_legacy_reservation_and_quota_response(self):
        server.db_exec("INSERT INTO quota_reservations(id,day,subjects,units,status) VALUES('legacy',?,?,1,'pending')",
                       (server._today(),json.dumps([f'user:{self.uid}'])))
        server.release_quota({'id':'legacy'})
        self.assertEqual(self.wallet()['balance_cents'],30)
        q=self.client.get('/api/quota').json()
        self.assertEqual(q['billing']['wallet']['balance_cents'],30)
        self.assertEqual(q['billing']['parse_price_cents'],3)

    def test_wallet_and_pending_refund_survive_a_new_process(self):
        self.exhaust();r=self.reserve()
        code='''import json,sys,server
data=json.loads(sys.stdin.read())
before=server._web_billing_status(data['uid'])['wallet']
server.release_quota({'id':data['reservation']})
server.release_quota({'id':data['reservation']})
after=server._web_billing_status(data['uid'])['wallet']
print(json.dumps({'before':before,'after':after}))
'''
        child=subprocess.run([sys.executable,'-c',code],
            input=json.dumps({'uid':self.uid,'reservation':r['id']}),text=True,capture_output=True,
            env=dict(os.environ,DATA_DIR=str(server.DATA_DIR),MIHOMO_OFF='1'),timeout=20,check=True)
        result=json.loads(child.stdout)
        self.assertEqual(result['before']['balance_cents'],27)
        self.assertEqual(result['before']['reserved_cents'],3)
        self.assertEqual(result['after']['balance_cents'],30)
        self.assertEqual(result['after']['reserved_cents'],0)

    def test_simultaneous_identical_admin_credit_applied_once(self):
        body=server.WalletEditBody(mode='add',cents=100,expected_version=0,request_id='parallel-credit')
        with mock.patch.object(server,'_require_admin'),ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:server.admin_edit_wallet(self.uid,body,make_request()),range(8)))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(self.wallet()['balance_cents'],130)
        self.assertEqual(server.db_exec('SELECT COUNT(*) FROM wallet_ledger',(),'one')[0],1)

    def set_daily(self, daily):
        result=self.admin_post('/api/admin/billing-prices',{
            'parse_price_cents':3,'transcript_price_cents':3,'user_daily':daily})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['user_daily'],daily)

    def test_admin_daily_limit_applies_immediately_and_preserves_usage(self):
        r=self.reserve(3);server.settle_quota(r,3)
        self.set_daily(20)
        quota=self.client.get('/api/quota').json()
        self.assertEqual((quota['limit'],quota['used'],quota['remaining'],quota['user_daily']),(20,3,17,20))
        with mock.patch.object(server,'_require_admin'):
            users=self.client.get('/api/admin/users').json()['users']
        user=next(u for u in users if u['id']==self.uid)
        self.assertEqual(user['free_remaining'],17)
        self.set_daily(2)
        self.assertEqual(server.quota_status(make_request()),(2,3,0))
        paid=self.reserve();server.settle_quota(paid,1)
        self.assertEqual(self.wallet()['balance_cents'],27)

    def test_zero_daily_limit_uses_balance_from_first_request(self):
        self.set_daily(0)
        with mock.patch.object(server,'_parse_cached',return_value={'title':'test'}):
            result=self.client.post('/api/parse',json={'text':'https://v.douyin.com/test/'})
        self.assertEqual(result.status_code,200)
        self.assertEqual((self.wallet()['balance_cents'],self.wallet()['spent_cents']),(27,3))
        self.assertEqual(server.quota_status(make_request()),(0,0,0))

    def test_limit_change_does_not_reprice_inflight_free_reservation(self):
        self.set_daily(2)
        r=self.reserve(2)
        self.set_daily(0)
        server.settle_quota(r,2)
        self.assertEqual(self.wallet()['balance_cents'],30)
        self.assertEqual(self.wallet()['spent_cents'],0)

    def test_user_limit_does_not_change_anonymous_or_transcript_allowance(self):
        text_limit=server._atc_cfg()['transcript_daily']
        self.set_daily(25)
        with mock.patch.object(server,'current_user',return_value=None):
            quota=self.client.get('/api/quota').json()
            self.assertEqual(quota['limit'],server.FREE_ANON_DAILY)
            self.assertEqual(quota['user_daily'],25)
        self.assertEqual(server._atc_transcript_status(self.uid)[0],text_limit)

    def test_invalid_daily_limit_rejected_and_old_price_client_preserves_setting(self):
        self.set_daily(20)
        for daily in (-1,1.5,True,'10',10001):
            with self.subTest(daily=daily):
                result=self.admin_post('/api/admin/billing-prices',{
                    'parse_price_cents':20,'transcript_price_cents':20,'user_daily':daily})
                self.assertEqual(result.status_code,422)
                self.assertEqual(server.free_user_daily(),20)
                self.assertEqual(server._web_billing_status()['parse_price_cents'],3)
        self.assertEqual(self.admin_post('/api/admin/billing-prices',{
            'parse_price_cents':4,'transcript_price_cents':5}).status_code,200)
        self.assertEqual(server.free_user_daily(),20)

    def test_daily_limit_persists_in_another_process_and_defaults_to_environment(self):
        with mock.patch.object(server,'FREE_USER_DAILY',12):
            self.assertEqual(server.free_user_daily(),12)
        self.set_daily(31)
        child=subprocess.run([sys.executable,'-c','import server; print(server.free_user_daily())'],
            text=True,capture_output=True,env=dict(os.environ,DATA_DIR=str(server.DATA_DIR),MIHOMO_OFF='1'),
            timeout=20,check=True)
        self.assertEqual(child.stdout.strip(),'31')

    def test_batch_and_async_share_use_configured_daily_limit(self):
        self.set_daily(1)
        with mock.patch.object(server,'_parse_cached',return_value={'title':'test'}):
            result=self.client.post('/api/parse/batch',json={
                'text':'https://v.douyin.com/test1/ https://v.douyin.com/test2/'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.wallet()['spent_cents'],3)
        self.assertEqual(result.json()['quota']['limit'],1)
        with mock.patch.object(server,'_share_origin',return_value='https://example.test'):
            share=self.client.post('/api/shares',json={'text':'https://v.douyin.com/dailyLimit/'})
        self.assertEqual(share.status_code,202,share.text)
        self.assertEqual(self.wallet()['reserved_cents'],3)


if __name__ == '__main__':
    unittest.main()

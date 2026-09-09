"""注册/登录的真实 HTTP 门禁、跨进程会话与前端缓存契约。"""
import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from tests.test_security_reliability import server, make_request


class AuthFlowTests(unittest.TestCase):
    def setUp(self):
        server._captcha_hits.clear()
        server._auth_hits.clear()
        self.client = TestClient(server.app)
        self.email = f'auth-check-{time.time_ns()}@example.test'
        self.password = 'test-password-only'

    def tearDown(self):
        row = server.db_exec('SELECT id FROM users WHERE email=?', (self.email,), 'one')
        if row:
            server.db_exec('DELETE FROM user_sessions WHERE user_id=?', (row['id'],))
            server.db_exec('DELETE FROM users WHERE id=?', (row['id'],))
        self.client.close()

    def math_grant(self):
        question = self.client.get('/api/auth/math').json()
        a, op, b, *_ = question['question'].split()
        answer = int(a) + int(b) if op == '+' else int(a) - int(b)
        result = self.client.post('/api/auth/math/verify', json={
            'cid': question['cid'], 'answer': str(answer)})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['math_token']

    def slider_pass(self):
        response = self.client.get('/api/auth/captcha', headers={'X-Auth-Math': self.math_grant()})
        self.assertEqual(response.status_code, 200, response.text)
        cap = response.json()
        # 在受控测试库里取得答案并回拨签发时间，HTTP 验证逻辑、轨迹与 PoW 都不替换。
        x, y, issued, ip = server._captchas[cap['cid']]
        server._captchas[cap['cid']] = (x, y, issued - 1, ip)
        nonce = 0
        while not server._pow_ok(cap['cid'], str(nonce)):
            nonce += 1
        result = self.client.post('/api/auth/captcha/verify', json={
            'cid': cap['cid'], 'x': x, 'nonce': str(nonce),
            'trajectory': [{'t': i * 100, 'x': x * fraction} for i, fraction in enumerate(
                (0.02, 0.10, 0.24, 0.45, 0.68, 0.84, 0.95, 1))]})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['pass_token']

    def auth(self, action, token=None):
        return self.client.post('/api/auth/' + action, json={
            'email': self.email, 'password': self.password,
            'pass_token': token if token is not None else self.slider_pass()})

    def test_register_session_survives_another_process_and_logout_revokes_it(self):
        result = self.auth('register')
        self.assertEqual(result.status_code, 200, result.text)
        cookie = result.headers['set-cookie'].lower()
        self.assertIn('httponly', cookie)
        self.assertIn('samesite=lax', cookie)
        self.assertIn('max-age=2592000', cookie)
        token = self.client.cookies.get('sess')
        row = server.db_exec('SELECT * FROM user_sessions WHERE token_hash=?',
                             (hashlib.sha256(token.encode()).hexdigest(),), 'one')
        self.assertIsNotNone(row)
        self.assertNotIn(token, json.dumps(dict(row)))
        code = '''import json, sys
from starlette.requests import Request
import server
token=json.load(sys.stdin)['token']
req=Request({'type':'http','headers':[(b'cookie',('sess='+token).encode())]})
user=server.current_user(req)
print(json.dumps({'email':user['email'] if user else None}))
'''
        env = dict(os.environ, DATA_DIR=str(server.DATA_DIR), MIHOMO_OFF='1')
        child = subprocess.run([sys.executable, '-c', code], input=json.dumps({'token': token}),
                               text=True, capture_output=True, env=env, timeout=20, check=True)
        self.assertEqual(json.loads(child.stdout)['email'], self.email)
        self.assertEqual(self.client.post('/api/auth/logout').status_code, 200)
        self.assertIsNone(self.client.get('/api/auth/me').json()['user'])
        replay = self.client.get('/api/auth/me', headers={'Cookie': 'sess=' + token})
        self.assertIsNone(replay.json()['user'])

    def test_login_rotates_current_session_and_rejects_reused_slider_pass(self):
        self.assertEqual(self.auth('register').status_code, 200)
        old = self.client.cookies.get('sess')
        proof = self.slider_pass()
        self.assertEqual(self.auth('login', proof).status_code, 200)
        self.assertNotEqual(old, self.client.cookies.get('sess'))
        self.assertIsNone(server.current_user(make_request(headers={'Cookie': 'sess=' + old})))
        self.assertEqual(self.auth('login', proof).status_code, 400)

    def test_session_expiry_disabled_account_and_missing_proof(self):
        self.assertEqual(self.auth('register', '').status_code, 400)
        self.assertEqual(self.auth('login', '').status_code, 400)
        self.assertEqual(self.auth('register').status_code, 200)
        with mock.patch.object(server.time, 'time', return_value=time.time() + server.USER_SESSION_TTL + 1):
            self.assertIsNone(self.client.get('/api/auth/me').json()['user'])
        server.db_exec('UPDATE users SET disabled=1 WHERE email=?', (self.email,))
        self.assertIsNone(self.client.get('/api/auth/me').json()['user'])

    def test_disabling_then_reenabling_user_does_not_restore_old_sessions(self):
        self.assertEqual(self.auth('register').status_code, 200)
        uid = self.client.get('/api/auth/me').json()['user']['id']
        admin_token = server._new_session()
        try:
            headers = {'Cookie': 'admin_session=' + admin_token}
            for disabled in (True, False):
                response = self.client.post(f'/api/admin/users/{uid}/toggle',
                                            json={'disabled': disabled}, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(self.client.get('/api/auth/me').json()['user'])
        finally:
            server._sessions.pop(admin_token, None)

    def test_arithmetic_is_one_time_and_cannot_be_bypassed(self):
        self.assertEqual(self.client.get('/api/auth/captcha').status_code, 403)
        self.assertEqual(self.client.get('/api/auth/captcha', headers={'X-Auth-Math': 'forged'}).status_code, 403)
        q = self.client.get('/api/auth/math').json()
        self.assertEqual(set(q), {'cid', 'question', 'expires_at'})
        wrong = self.client.post('/api/auth/math/verify', json={'cid': q['cid'], 'answer': '99'})
        self.assertEqual(wrong.status_code, 400)
        self.assertNotIn(q['cid'], server._math_challenges)
        replay = self.client.post('/api/auth/math/verify', json={'cid': q['cid'], 'answer': '0'})
        self.assertEqual(replay.status_code, 400)
        proof = self.math_grant()
        self.assertEqual(self.client.get('/api/auth/captcha', headers={'X-Auth-Math': proof}).status_code, 200)
        self.assertEqual(self.auth('register', proof).status_code, 400)

    def test_arithmetic_expiry_ip_binding_and_slider_budget(self):
        request = make_request()
        q = server._make_math_challenge(request)
        with self.assertRaises(server.ApiError):
            server._verify_math_challenge(q['cid'], '0', make_request(client_ip='203.0.113.11'))
        q = server._make_math_challenge(request)
        answer = server._math_challenges[q['cid']][0]
        with mock.patch.object(server.time, 'time', return_value=time.time() + 301):
            with self.assertRaises(server.ApiError):
                server._verify_math_challenge(q['cid'], str(answer), request)
        proof = self.math_grant()
        with self.assertRaises(server.ApiError):
            server._require_math_grant(make_request(headers={'X-Auth-Math': proof}))
        exp, ip, _ = server._math_grants[proof]
        server._math_grants[proof] = (exp, ip, 1)
        self.assertEqual(self.client.get('/api/auth/captcha', headers={'X-Auth-Math': proof}).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/captcha', headers={'X-Auth-Math': proof}).status_code, 403)


class FrontendVersionTests(unittest.TestCase):
    def test_all_pages_have_consistent_versions_and_no_persistent_html_cache(self):
        with TestClient(server.app) as client:
            for path in ('/', '/?lang=en', '/api-docs', '/transcript', '/api-console', '/admin_d', '/s/version-check-missing'):
                response = client.get(path)
                self.assertIn(f'content="{server.FRONTEND_VERSION}"', response.text)
                self.assertEqual(response.text.count('name="app-version"'), 1)
                self.assertEqual(response.headers['x-frontend-version'], server.FRONTEND_VERSION)
                self.assertEqual(response.headers['cache-control'], 'private, no-store')
            home = client.get('/').text
            self.assertIn('/platform-logos/tiktok.svg?v=' + server.FRONTEND_VERSION, home)
            self.assertIn('/og.png?v=' + server.FRONTEND_VERSION, home)

    def test_only_current_version_assets_get_immutable_cache(self):
        with TestClient(server.app) as client:
            for path in ('/platform-logos/tiktok.svg', '/og.svg', '/og.png'):
                for suffix in ('', '?v=old'):
                    self.assertEqual(client.get(path + suffix).headers['cache-control'], 'no-cache')
                response = client.get(path + '?v=' + server.FRONTEND_VERSION)
                self.assertEqual(response.status_code, 200)
                self.assertIn('immutable', response.headers['cache-control'])


if __name__ == '__main__':
    unittest.main()

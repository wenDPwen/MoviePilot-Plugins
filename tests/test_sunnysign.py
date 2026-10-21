"""Offline SunnyPT protocol tests; no real credentials or network requests."""

import datetime
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import test_framehdrsign as host


path = Path(__file__).resolve().parents[1] / 'plugins.v2/sunnysign/__init__.py'
spec = importlib.util.spec_from_file_location('sunny_test', path)
plugin = importlib.util.module_from_spec(spec)
modules = {name: host.types.ModuleType(name) for name in (
    'app', 'app.core', 'app.core.config', 'app.log', 'app.plugins', 'app.schemas', 'app.schemas.types',
)}
modules['app.core.config'].settings = host.types.SimpleNamespace(TZ='Asia/Shanghai', PROXY={})
modules['app.log'].logger = host.Mock()
modules['app.plugins']._PluginBase = host.PluginBase
modules['app.schemas.types'].NotificationType = host.types.SimpleNamespace(Plugin='plugin')
with patch.dict(sys.modules, modules):
    spec.loader.exec_module(plugin)


TOKEN = 'fixture-access-token'


def status(checked=True):
    return {'checked_in': checked, 'points': 23 if checked else 0,
            'days': 1, 'total_days': 529, 'makeup_cards': 0}


class SunnyTests(unittest.TestCase):
    def setUp(self):
        self.sign = plugin.SunnySign()
        self.sign.init_plugin({'enabled': True, 'cookie': 'session=fixture-cookie', 'retry': 0})
        self.web = self.sign._session()
        self.api = self.sign._session(web=False)
        self.addCleanup(self.web.close)
        self.addCleanup(self.api.close)
        self.addCleanup(self.sign.stop_service)

    def credentials(self):
        self.sign._config.update(username='fixture-user', password='fixture-password')

    def run_with(self, responses):
        with patch.object(self.sign, '_request', side_effect=responses) as call:
            result = self.sign._run(self.web, self.api)
        return result, call

    def test_cookie_priority_already_signed_no_password_or_post(self):
        self.credentials()
        result, call = self.run_with([{'accessToken': TOKEN}, status(), {'records': []}])
        self.assertEqual(result['status'], '已签到')
        self.assertEqual(result['award'], 23)
        self.assertTrue(all(c.args[1] == 'GET' for c in call.call_args_list))
        self.assertNotIn(TOKEN, json.dumps(self.sign.storage))

    def test_web_cookie_and_api_bearer_are_isolated(self):
        with patch.object(self.web, 'request', return_value=host.response({'data': {'accessToken': TOKEN}})) as web:
            self.assertEqual(self.sign._token(self.web), TOKEN)
        with patch.object(self.api, 'request', return_value=host.response({'code': 0, 'data': status()})) as api:
            self.sign._status(self.api, TOKEN)
        self.assertEqual(web.call_args.args[1], 'https://sunnypt.top/api/auth/session')
        self.assertNotIn('Authorization', web.call_args.kwargs['headers'])
        self.assertEqual(api.call_args.args[1], 'https://api.sunnypt.top/api/v1/attendance/status')
        self.assertEqual(api.call_args.kwargs['headers'], {'Authorization': 'Bearer ' + TOKEN})
        self.assertFalse(api.call_args.kwargs['allow_redirects'])
        self.assertFalse(list(self.api.cookies))
        self.assertFalse(self.web.trust_env)
        self.assertEqual(self.web.cookies.get('session'), 'fixture-cookie')

    def test_password_only_login_backfills_cookie_then_restart_reuses_it(self):
        self.credentials()
        self.sign._config['cookie'] = ''
        self.web.cookies.clear()

        def reply(method, url, **kwargs):
            if method == 'POST':
                self.assertEqual(url, 'https://sunnypt.top/api/auth/session/login')
                self.assertEqual(kwargs['json'], {'username': 'fixture-user', 'password': 'fixture-password',
                                                  'code': '', 'rememberMe': True})
                self.web.cookies.set('session', 'new-fixture-cookie', domain='sunnypt.top', path='/', secure=True)
                return host.response({'data': {'username': 'fixture-user'}})
            return host.response({'data': {'accessToken': TOKEN}})

        with patch.object(self.web, 'request', side_effect=reply):
            self.assertEqual(self.sign._login(self.web), TOKEN)
        self.assertEqual(self.sign.saved_config['cookie'], 'session=new-fixture-cookie')
        self.assertFalse(self.sign.get_data('login_blocked'))
        restarted = plugin.SunnySign()
        restarted.storage = self.sign.storage.copy()
        restarted.init_plugin(self.sign.saved_config)
        self.addCleanup(restarted.stop_service)
        with restarted._session() as web, restarted._session(web=False) as api:
            with patch.object(restarted, '_request', side_effect=[{'accessToken': TOKEN}, status(), {'records': []}]) as call:
                self.assertTrue(restarted._run(web, api)['success'])
            self.assertEqual(web.cookies.get('session'), 'new-fixture-cookie')
            self.assertTrue(all(c.args[1] == 'GET' for c in call.call_args_list))

    def test_missing_cookie_uses_password_once(self):
        self.credentials()
        with patch.object(self.sign, '_token', side_effect=plugin.AuthError('guest')), \
                patch.object(self.sign, '_login', return_value=TOKEN) as login, \
                patch.object(self.sign, '_request', side_effect=[status(), {'records': []}]):
            self.assertTrue(self.sign._run(self.web, self.api)['success'])
        login.assert_called_once_with(self.web, False)

    def test_api_auth_failure_falls_back_once(self):
        self.credentials()
        with patch.object(self.sign, '_login', return_value=TOKEN) as login:
            result, _ = self.run_with([{'accessToken': 'old'}, plugin.AuthError('expired'), status(), {'records': []}])
        self.assertTrue(result['success'])
        login.assert_called_once()

    def test_api_auth_failure_after_login_does_not_login_again(self):
        self.credentials()
        with patch.object(self.sign, '_token', side_effect=plugin.AuthError('guest')), \
                patch.object(self.sign, '_login', return_value=TOKEN) as login, \
                patch.object(self.sign, '_read_status', side_effect=plugin.AuthError('expired')):
            with self.assertRaises(plugin.AuthError):
                self.sign._run(self.web, self.api)
        login.assert_called_once()

    def test_failed_password_blocks_until_manual_retry(self):
        self.credentials()
        with patch.object(self.web, 'request', return_value=host.response({'error': 'fixture-password'}, status=401)) as call:
            with self.assertRaises(plugin.AuthError) as exc:
                self.sign._login(self.web)
            self.assertNotIn('fixture-password', str(exc.exception))
            with self.assertRaises(plugin.AuthError):
                self.sign._login(self.web)
            self.assertEqual(call.call_count, 1)
            with self.assertRaises(plugin.AuthError):
                self.sign._login(self.web, manual=True)
            self.assertEqual(call.call_count, 2)

    def test_login_timeout_also_blocks_future_automatic_submissions(self):
        self.credentials()
        with patch.object(self.web, 'request', side_effect=requests.Timeout('fixture-password')) as call:
            for _ in range(2):
                with self.assertRaises(plugin.AuthError):
                    self.sign._login(self.web)
        self.assertEqual(call.call_count, 1)

    def test_changed_credentials_unlock_failed_login(self):
        self.credentials()
        self.sign.save_data('login_blocked', self.sign._fingerprint())
        self.sign._config['password'] = 'corrected-fixture'
        with patch.object(self.web, 'request', return_value=host.response({'error': '2FA required'}, status=400)) as call:
            with self.assertRaises(plugin.AuthError):
                self.sign._login(self.web)
        self.assertEqual(call.call_count, 1)

    def test_login_requires_confirmed_session_and_new_cookie(self):
        self.credentials()
        for response in (None, {'accessToken': TOKEN}):
            with self.subTest(response=response), patch.object(self.sign, '_request', side_effect=[{}, response]):
                with self.assertRaises(plugin.AuthError):
                    self.sign._login(self.web, manual=True)
        self.assertEqual(self.sign._config['cookie'], 'session=fixture-cookie')

    def test_cookie_rotation_preserves_current_history_not_other_accounts(self):
        old = self.sign._fingerprint()
        self.sign.save_data('account', {'fingerprint': old, 'points': 23})
        self.sign.save_data('history', [{'fingerprint': old}, {'fingerprint': 'other'}])
        self.web.cookies.set('session', 'rotated', domain='sunnypt.top', path='/', secure=True)
        self.sign._save_session(self.web)
        current = self.sign._fingerprint()
        self.assertNotEqual(old, current)
        self.assertEqual(self.sign.get_data('account')['fingerprint'], current)
        self.assertEqual([r['fingerprint'] for r in self.sign.get_data('history')], [current, 'other'])

    def test_manual_cookie_change_does_not_restore_stale_jar(self):
        self.sign._save_session(self.web)
        self.sign._config['cookie'] = 'session=manual-cookie'
        with self.sign._session() as web:
            self.assertEqual(web.cookies.get('session'), 'manual-cookie')

    def test_post_timeout_reconciles_without_replay(self):
        result, call = self.run_with([{'accessToken': TOKEN}, status(False), plugin.SignError('timeout'), status(), {'records': []}])
        self.assertEqual(result['status'], '成功')
        self.assertEqual(sum(c.args[1] == 'POST' for c in call.call_args_list), 1)

    def test_unverified_post_never_reported_as_success(self):
        result, call = self.run_with([{'accessToken': TOKEN}, status(False), {}, plugin.SignError('timeout')])
        self.assertEqual(result['status'], '待确认')
        self.assertFalse(result['success'])
        self.assertEqual(sum(c.args[1] == 'POST' for c in call.call_args_list), 1)

    def test_still_unsigned_is_failure(self):
        result, _ = self.run_with([{'accessToken': TOKEN}, status(False), {}, status(False), {'records': []}])
        self.assertEqual(result['status'], '失败')
        self.assertIsNone(result['award'])

    def test_monthly_failure_does_not_discard_confirmed_signin(self):
        result, _ = self.run_with([{'accessToken': TOKEN}, status(), plugin.SignError('calendar')])
        self.assertTrue(result['success'])
        self.assertIn('calendar_warning', self.sign.get_data('account'))
        self.assertIn(self.sign._now().strftime('%Y-%m-%d'), self.sign.get_data('account')['records'])

    def test_monthly_only_trusts_valid_dates_and_not_arbitrary_fields(self):
        date = self.sign._now().strftime('%Y-%m-01')
        records = [{'date': date, 'points': 'private', 'token': TOKEN}, {'date': 'bad'},
                   {'date': '1900-01-01'}, None]
        self.run_with([{'accessToken': TOKEN}, status(), {'records': records}])
        account = self.sign.get_data('account')
        self.assertIn(date, account['records'])
        self.assertNotIn('1900-01-01', account['records'])
        self.assertNotIn('private', json.dumps(account))
        self.assertNotIn(TOKEN, json.dumps(account))

    def test_read_retry_never_retries_auth_failure(self):
        self.sign._config['retry'] = 3
        with patch.object(self.sign, '_status', side_effect=plugin.AuthError('expired')) as call:
            with self.assertRaises(plugin.AuthError):
                self.sign._read_status(self.api, TOKEN)
        self.assertEqual(call.call_count, 1)

    def test_read_only_retry(self):
        self.sign._config['retry'] = 1
        with patch.object(self.sign, '_status', side_effect=[plugin.SignError('timeout'), status()]) as call, \
                patch.object(self.sign._stop, 'wait', return_value=False):
            self.assertTrue(self.sign._read_status(self.api, TOKEN)['checked_in'])
        self.assertEqual(call.call_count, 2)

    def test_redirect_and_foreign_target_blocked(self):
        with patch.object(self.web, 'request', return_value=host.response(status=307, location='https://example.com')) as call:
            with self.assertRaises(plugin.SignError):
                self.sign._request(self.web, 'POST', '/api/auth/session/login', json={'password': 'fixture'})
            self.assertFalse(call.call_args.kwargs['allow_redirects'])
            with self.assertRaises(plugin.SignError):
                self.sign._request(self.web, 'POST', 'https://example.com/api/auth/session/login')
            self.assertEqual(call.call_count, 1)

    def test_makeup_and_wrong_method_never_requested(self):
        with patch.object(self.api, 'request') as call:
            for method, path in [('POST', '/api/v1/attendance/makeup'), ('GET', '/api/v1/attendance/check-in')]:
                with self.assertRaises(plugin.SignError):
                    self.sign._request(self.api, method, path, token=TOKEN)
            call.assert_not_called()

    def test_api_refuses_cookie_and_discards_set_cookie(self):
        self.api.cookies.set('bad', 'value')
        with self.assertRaises(plugin.SignError):
            self.sign._status(self.api, TOKEN)
        self.api.cookies.clear()

        def reply(*args, **kwargs):
            self.api.cookies.set('unexpected', 'value')
            return host.response({'code': 0, 'data': status()})

        with patch.object(self.api, 'request', side_effect=reply):
            self.sign._status(self.api, TOKEN)
        self.assertFalse(list(self.api.cookies))

    def test_invalid_envelope_and_json_rejected(self):
        responses = [host.response({'code': False, 'data': status()}), host.response({'data': status()}),
                     host.response({'code': '0', 'data': status()}), host.response([]),
                     host.response(text='<html>challenge</html>'), host.response(status=503)]
        for response in responses:
            with self.subTest(response=response), patch.object(self.api, 'request', return_value=response):
                with self.assertRaises(plugin.SignError):
                    self.sign._status(self.api, TOKEN)

    def test_api_business_auth_code_detected(self):
        with patch.object(self.api, 'request', return_value=host.response({'code': 401000, 'msg': 'private'})):
            with self.assertRaises(plugin.AuthError):
                self.sign._status(self.api, TOKEN)

    def test_invalid_status_fields_cannot_trigger_checkin(self):
        for data in ({'checked_in': 0}, {'checked_in': 'true'}, {'checked_in': True, 'points': float('nan')},
                     {'checked_in': True, 'points': -1}, {'checked_in': True, 'days': True}):
            with self.subTest(data=data), patch.object(self.sign, '_request', side_effect=[{'accessToken': TOKEN}, data]) as call:
                with self.assertRaises(plugin.SignError):
                    self.sign._run(self.web, self.api)
                self.assertTrue(all(c.args[1] == 'GET' for c in call.call_args_list))

    def test_page_and_notifications_contain_no_credentials(self):
        self.credentials()
        with patch.object(self.sign, '_request', side_effect=[{'accessToken': TOKEN}, status(), {'records': []}]):
            self.assertTrue(self.sign.check(manual=True)['success'])
        visible = json.dumps([self.sign.get_page(), self.sign.notifications, self.sign.get_data('history')], ensure_ascii=False)
        for secret in (TOKEN, 'fixture-cookie', 'fixture-user', 'fixture-password'):
            self.assertNotIn(secret, visible)
        self.assertIn('#e8f5ec', visible)
        self.assertIn('mdi-check-circle', visible)

    def test_form_defaults_and_invalid_cron(self):
        form, defaults = self.sign.get_form()
        for field in ('username', 'password', 'cookie'):
            self.assertIn(field, json.dumps(form))
            self.assertEqual(defaults[field], '')
        self.sign.init_plugin({'enabled': True, 'cron': 'bad'})
        self.assertFalse(self.sign.get_service())

    def test_midnight_is_pending_not_success(self):
        today = self.sign._now()
        tomorrow = today + datetime.timedelta(days=1)
        with patch.object(self.sign, '_snapshot'), patch.object(self.sign, '_now', side_effect=[today, tomorrow]):
            result, _ = self.run_with([{'accessToken': TOKEN}, status(False), {}, status()])
        self.assertEqual(result['status'], '待确认')


if __name__ == '__main__':
    unittest.main()

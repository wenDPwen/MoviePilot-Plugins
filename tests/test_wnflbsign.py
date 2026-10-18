import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import test_framehdrsign as host


path = Path(__file__).resolve().parents[1] / 'plugins.v2/wnflbsign/__init__.py'
spec = importlib.util.spec_from_file_location('wnflb_test', path, submodule_search_locations=[str(path.parent)])
plugin = importlib.util.module_from_spec(spec)
modules = {name: host.types.ModuleType(name) for name in (
    'app', 'app.core', 'app.core.config', 'app.log', 'app.plugins', 'app.schemas', 'app.schemas.types',
)}
modules['app.core.config'].settings = host.types.SimpleNamespace(TZ='Asia/Shanghai', PROXY={})
modules['app.log'].logger = host.Mock()
modules['app.plugins']._PluginBase = host.PluginBase
modules['app.schemas.types'].NotificationType = host.types.SimpleNamespace(Plugin='plugin')
with patch.dict(sys.modules, {**modules, 'wnflb_test': plugin}):
    spec.loader.exec_module(plugin)


def page(checked=False, uid='123'):
    return '''<script>var discuz_uid = '%s';var fx_chk_menu=%s;
function fx_checkin(){showWindow('fx_checkin', 'plugin.php?id=fx_checkin:checkin&formhash=abcdef12&abcdef12');}</script>
<a href="member.php?mod=logging&amp;action=logout&amp;formhash=abcdef12">退出</a>
<img id="fx_checkin_b" alt="%s">
<div id="fx_checkin_menub">已连续签到:<i>14</i>天，累计签到:<i>17</i>天</div>
<ul class="creditl"><li><em> 福利: </em>34</li><li><em> 金币: </em>0</li><li><em>积分:</em>17</li></ul>''' % (
        uid, 'true' if checked else 'false', '已签到' if checked else '签到')


def calendar():
    now = plugin.WnflbSign._now()
    data = {'d': now.strftime('%Y%m'), str(now.day): {'l': 10, 'm': '2', 't': '0930'}}
    return '<root><![CDATA[%s]]></root>' % json.dumps(data)


LOGIN = '''<form id="loginform_AbC" action="member.php?mod=logging&amp;action=login&amp;loginsubmit=yes&amp;loginhash=AbC">
<input name="formhash" value="abcdef12"><input name="username"><input name="password" type="password">
<input name="referer" value="https://www.wnflb2023.com/forum.php"><input name="answer"></form>'''


def response(text='', status=200):
    return host.response(text=text, status=status)


class WnflbTests(unittest.TestCase):
    def setUp(self):
        self.sign = plugin.WnflbSign()
        self.sign.init_plugin({'enabled': True, 'cookie': 'discuz_auth=fixture', 'retry': 0})
        self.session = requests.Session()
        self.addCleanup(self.session.close)
        self.addCleanup(self.sign.stop_service)

    def test_status_and_separate_credit_units(self):
        data = plugin.parse_status(page(True))
        self.assertTrue(data['has_checked_today'])
        self.assertEqual((data['points'], data['score'], data['coins']), (34, 17, 0))
        self.assertEqual((data['continuous_days'], data['total_days']), (14, 17))

    def test_guest_detected(self):
        self.assertIsNone(plugin.parse_status(page(uid='0')))

    def test_unknown_page_not_treated_as_guest(self):
        with self.assertRaises(ValueError):
            plugin.parse_status('<html>please complete verification</html>')

    def test_unsigned_requires_formhash(self):
        with self.assertRaises(ValueError):
            plugin.parse_status(page().replace('abcdef12', 'invalid'))

    def test_calendar_xml_parsed_without_eval(self):
        data = plugin.parse_calendar(calendar(), self.sign._now().strftime('%Y%m'))
        self.assertEqual(data[self.sign._now().strftime('%Y-%m-%d')]['points'], '2')
        with self.assertRaises(ValueError):
            plugin.parse_calendar('<root><![CDATA[alert(1)]]></root>', '202610')
        with self.assertRaises(ValueError):
            plugin.parse_calendar('<!DOCTYPE root><root/>', '202610')

    def test_calendar_month_mismatch(self):
        with self.assertRaises(ValueError):
            plugin.parse_calendar(calendar(), '200001')

    def test_already_checked_never_calls_checkin_endpoint(self):
        with patch.object(self.sign, '_request', side_effect=[response(page(True)), response(calendar())]) as call:
            result = self.sign._run(self.session)
        self.assertEqual(result['status'], '已签到')
        self.assertEqual(call.call_count, 2)

    def test_checkin_get_submitted_once_and_verified(self):
        replies = [response(page()), response(calendar()), response('签到成功'), response(page(True)), response(calendar())]
        with patch.object(self.sign, '_request', side_effect=replies) as call:
            result = self.sign._run(self.session)
        self.assertTrue(result['success'])
        self.assertEqual(result['award'], '2')
        self.assertEqual(sum('id=fx_checkin:checkin' in c.args[2] for c in call.call_args_list), 1)

    def test_sign_timeout_is_reconciled_not_replayed(self):
        replies = [response(page()), response(calendar()), plugin.SignError('超时'), response(page(True)), response(calendar())]
        with patch.object(self.sign, '_request', side_effect=replies) as call:
            self.assertTrue(self.sign._run(self.session)['success'])
        self.assertEqual(sum('id=fx_checkin:checkin' in c.args[2] for c in call.call_args_list), 1)

    def test_success_text_alone_is_not_success(self):
        replies = [response(page()), response(calendar()), response('签到成功'), response(page()), response(calendar())]
        with patch.object(self.sign, '_request', side_effect=replies):
            self.assertFalse(self.sign._run(self.session)['success'])

    def test_calendar_failure_does_not_lose_status(self):
        with patch.object(self.sign, '_request', side_effect=[response(page(True)), response('not-json')]):
            data = self.sign._init(self.session)
        self.assertTrue(data['has_checked_today'])
        self.assertIn('calendar_warning', data)

    def test_login_form_dynamic_action_and_fields(self):
        form = plugin.parse_login_form(LOGIN)
        self.assertIn('loginhash=AbC', form.action)
        self.assertEqual(form.fields['formhash'], 'abcdef12')

    def test_password_login_security_answer_and_cookie_saved(self):
        self.sign._config.update(cookie='', username='fixture-user', password='fixture-password', questionid='1', answer='fixture-answer')
        self.session.cookies.set('prefix_auth', 'new-session', domain='www.wnflb2023.com', path='/')
        replies = [response(page(uid='0')), response(LOGIN), response('welcome'), response(page(True)), response(calendar())]
        with patch.object(self.sign, '_request', side_effect=replies) as call:
            result = self.sign._run(self.session)
        self.assertTrue(result['success'])
        data = call.call_args_list[2].kwargs['data']
        self.assertEqual(data['questionid'], '1')
        self.assertEqual(data['answer'], 'fixture-answer')
        self.assertEqual(data['cookietime'], '2592000')
        self.assertEqual(self.sign.saved_config['cookie'], 'prefix_auth=new-session')

    def test_login_captcha_blocks_submission(self):
        self.sign._config.update(username='fixture', password='fixture')
        html = LOGIN.replace('</form>', '<input name="seccodeverify"></form>')
        with patch.object(self.sign, '_request', return_value=response(html)) as call:
            with self.assertRaisesRegex(plugin.AuthError, '验证码'):
                self.sign._login(self.session)
        self.assertEqual(call.call_count, 1)

    def test_missing_security_answer_blocks_submission(self):
        self.sign._config.update(username='fixture', password='fixture', questionid='2')
        with patch.object(self.sign, '_request', return_value=response(LOGIN)) as call:
            with self.assertRaisesRegex(plugin.SignError, '安全问题'):
                self.sign._login(self.session)
        self.assertEqual(call.call_count, 1)

    def test_login_failure_suspends_automatic_retry(self):
        self.sign._config.update(username='fixture', password='wrong')
        with patch.object(self.sign, '_request', side_effect=[response(LOGIN), response('失败'), response(page(uid='0'))]) as call:
            with self.assertRaises(plugin.AuthError):
                self.sign._login(self.session)
            count = call.call_count
            with self.assertRaises(plugin.AuthError):
                self.sign._login(self.session)
            self.assertEqual(call.call_count, count)

    def test_cross_origin_requests_blocked(self):
        with patch.object(self.session, 'request') as call:
            with self.assertRaises(plugin.SignError):
                self.sign._request(self.session, 'POST', 'https://example.org/member.php')
        call.assert_not_called()

    def test_calendar_page_contains_no_hash_or_identity(self):
        data = plugin.parse_status(page(True))
        data['records'] = plugin.parse_calendar(calendar(), self.sign._now().strftime('%Y%m'))
        self.sign._snapshot(data)
        with patch.object(self.sign, '_request') as call:
            output = json.dumps(self.sign.get_page(), ensure_ascii=False)
        call.assert_not_called()
        self.assertIn('#e8f5ec', output)
        self.assertIn('福利余额', output)
        self.assertNotIn('abcdef12', output)
        self.assertNotIn('sign_path', json.dumps(self.sign.storage))

    def test_fingerprint_handles_numeric_question_and_answer_change(self):
        self.sign._config['questionid'] = 2
        old = self.sign._fingerprint()
        self.sign._config['answer'] = 'new'
        self.assertNotEqual(old, self.sign._fingerprint())

    def test_forms_serializable(self):
        output = json.dumps(self.sign.get_form(), ensure_ascii=False)
        self.assertIn('安全问题答案', output)
        self.assertEqual(self.sign.get_service()[0]['id'], 'WnflbSign')

    def test_read_failure_never_triggers_password_login(self):
        with patch.object(self.sign, '_request', return_value=response('error', 503)), patch.object(self.sign, '_login') as login:
            with self.assertRaises(plugin.SignError):
                self.sign._run(self.session)
        login.assert_not_called()

    def test_checkin_verification_failure_is_pending(self):
        replies = [response(page()), response(calendar()), response('签到成功'), plugin.SignError('超时')]
        with patch.object(self.sign, '_request', side_effect=replies) as call:
            result = self.sign._run(self.session)
        self.assertEqual(result['status'], '待确认')
        self.assertFalse(result['success'])
        self.assertEqual(call.call_count, 4)

    def test_cookie_reused_after_config_reload_without_password(self):
        self.sign._config.update(username='fixture', password='fixture')
        self.session.cookies.set('prefix_auth', 'rotated', domain='.wnflb2023.com', path='/')
        self.sign._save_session(self.session)
        self.sign.init_plugin(self.sign.saved_config)
        with self.sign._session() as session, patch.object(self.sign, '_login') as login, \
                patch.object(self.sign, '_request', side_effect=[response(page(True)), response(calendar())]):
            self.assertEqual(session.cookies.get('prefix_auth'), 'rotated')
            self.assertTrue(self.sign._run(session)['success'])
        login.assert_not_called()

    def test_gbk_html_decoded_from_declared_charset(self):
        reply = response()
        reply._content = ('<meta charset="gbk">' + page(True)).encode('gbk')
        data = plugin.parse_status(self.sign._html(reply))
        self.assertEqual(data['points'], 34)

    def test_valid_login_redirect_only(self):
        reply = response(status=302)
        reply.headers['Location'] = '/member.php?mod=logging&action=login'
        with self.assertRaises(plugin.AuthError):
            self.sign._html(reply)
        reply.headers['Location'] = 'https://example.org/member.php?action=login'
        with self.assertRaises(plugin.SignError) as exc:
            self.sign._html(reply)
        self.assertNotIsInstance(exc.exception, plugin.AuthError)

    def test_login_uses_discuz_ajax_and_login_referer(self):
        self.sign._config.update(username='fixture-user', password='fixture-password')
        replies = [response(LOGIN), response('welcome'), response(page(True)), response(calendar())]
        with patch.object(self.sign, '_request', side_effect=replies) as call:
            self.sign._login(self.session)
        submit = call.call_args_list[1]
        self.assertIn('inajax=1', submit.args[2])
        self.assertIn('loginhash=AbC', submit.args[2])
        self.assertEqual(submit.kwargs['headers']['Referer'], self.sign.BASE_URL + '/member.php?mod=logging&action=login')
        self.assertNotIn('loginsubmit', submit.kwargs['data'])
        self.assertIn('loginsubmit=yes', submit.args[2])

    def test_login_diagnostics_do_not_echo_response_secrets(self):
        cases = [
            ('用户名或密码错误，fixture-user fixture-password', '论坛拒绝了账号凭证'),
            ('验证码填写不正确 secret-cookie', '验证码或安全验证'),
            ('安全问题答案不正确 private-answer', '安全问题或答案'),
            ('密码错误次数过多', '登录尝试次数'),
            ('您的请求来路不正确', '登录表单或请求来源'),
        ]
        for body, expected in cases:
            with self.subTest(expected=expected):
                result = plugin.login_failure_reason(body)
                self.assertIn(expected, result)
                for private in ('fixture-user', 'fixture-password', 'secret-cookie', 'private-answer'):
                    self.assertNotIn(private, result)

    def test_login_rejection_reason_reaches_result(self):
        self.sign._config.update(username='fixture-user', password='fixture-password')
        replies = [response(LOGIN), response('<root><![CDATA[用户名或密码错误]]></root>'), response(page(uid='0'))]
        with patch.object(self.sign, '_request', side_effect=replies):
            with self.assertRaisesRegex(plugin.AuthError, '论坛拒绝了账号凭证'):
                self.sign._login(self.session)

    def test_network_verification_error_not_reported_as_password_error(self):
        self.sign._config.update(username='fixture-user', password='fixture-password')
        replies = [response(LOGIN), response('welcome'), plugin.SignError('网络请求超时')]
        with patch.object(self.sign, '_request', side_effect=replies):
            with self.assertRaisesRegex(plugin.AuthError, '状态复查失败.*网络请求超时'):
                self.sign._login(self.session)

    def test_diagnostic_is_metadata_only(self):
        reply = response('<html>fixture-password</html>')
        reply.cookies.set('prefix_auth', 'secret-cookie')
        self.session.cookies.set('prefix_auth', 'secret-cookie', domain='www.wnflb2023.com')
        result = plugin.login_diagnostics(reply, reply.text, self.session.cookies)
        self.assertIn('新认证Cookie:有', result)
        self.assertIn('会话认证Cookie:有', result)
        self.assertNotIn('secret-cookie', result)
        self.assertNotIn('fixture-password', result)
        reply.status_code = 302
        reply.headers['Location'] = 'https://example.org/?token=secret'
        result = plugin.login_diagnostics(reply, '', self.session.cookies)
        self.assertIn('跳转:站外', result)
        self.assertNotIn('example.org', result)
        self.assertNotIn('secret', result)

    def test_escaped_and_markup_errors_classified(self):
        escaped = '密码错误'.encode('unicode_escape').decode()
        self.assertIn('论坛拒绝了账号凭证', plugin.login_failure_reason(escaped))
        self.assertIn('登录尝试次数', plugin.login_failure_reason('密码错误次数过多，请于 <b>15</b> 分钟后重试'))

    def test_http_charset_and_gb18030_fallback(self):
        for header in ('text/html; charset=GB18030', 'text/html', 'text/html; charset=utf-8'):
            with self.subTest(header=header):
                reply = response()
                reply._content = page(True).encode('gb18030')
                reply.headers['Content-Type'] = header
                self.assertEqual(plugin.parse_status(self.sign._html(reply))['points'], 34)

    def test_isolated_bad_byte_does_not_break_login_state(self):
        reply = response()
        reply._content = ('<meta charset="utf-8">' + page(True)).encode() + b'<!--\xff-->'
        self.assertTrue(plugin.parse_status(self.sign._html(reply))['has_checked_today'])

    def test_script_charset_not_mistaken_for_document_charset(self):
        html = '<script charset="GBK" src="calendar.js"></script>' + page(True)
        reply = response(html)
        self.assertEqual(plugin.parse_status(self.sign._html(reply))['points'], 34)

    def test_xml_encoding_and_bom(self):
        text = '<?xml version="1.0" encoding="gbk"?><root>错误提示</root>'
        self.assertIn('错误提示', plugin.decode_page(text.encode('gbk')))
        self.assertTrue(plugin.decode_page(b'\xef\xbb\xbf<html>ok</html>').startswith('<html>'))

    def test_corrupt_and_empty_responses_rejected(self):
        for data in (b'', b'\xff' * 100):
            with self.assertRaises(ValueError):
                plugin.decode_page(data)

    def test_decoding_does_not_make_unknown_page_authenticated(self):
        reply = response()
        reply._content = '<html>访问异常</html>'.encode('gb18030')
        with self.assertRaises(ValueError):
            plugin.parse_status(self.sign._html(reply))


if __name__ == '__main__':
    unittest.main()

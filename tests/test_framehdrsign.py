"""Offline contract tests. Run with requests, pytz and apscheduler installed."""

import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests


class PluginBase:
    def __init__(self):
        self.storage = {}
        self.notifications = []

    def get_data(self, key):
        return copy.deepcopy(self.storage.get(key))

    def save_data(self, key, value):
        self.storage[key] = copy.deepcopy(value)

    def update_config(self, config):
        self.saved_config = copy.deepcopy(config)

    def post_message(self, **kwargs):
        self.notifications.append(kwargs)


def load_plugin():
    modules = {name: types.ModuleType(name) for name in (
        "app", "app.core", "app.core.config", "app.log", "app.plugins", "app.schemas", "app.schemas.types",
    )}
    modules["app.core.config"].settings = types.SimpleNamespace(TZ="Asia/Shanghai", PROXY={})
    modules["app.log"].logger = Mock()
    modules["app.plugins"]._PluginBase = PluginBase
    modules["app.schemas.types"].NotificationType = types.SimpleNamespace(Plugin="plugin")
    path = Path(__file__).resolve().parents[1] / "plugins.v2/framehdrsign/__init__.py"
    spec = importlib.util.spec_from_file_location("framehdrsign_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


plugin = load_plugin()


def response(data=None, status=200, text=None, location=None):
    result = requests.Response()
    result.status_code = status
    result._content = (text if text is not None else json.dumps(data)).encode()
    result.encoding = "utf-8"
    if location:
        result.headers["Location"] = location
    return result


def state(checked=False, token="fixture-csrf"):
    today = plugin.FrameHdrSign._now().strftime("%Y-%m-%d")
    return {"success": True, "data": {
        "has_checked_today": checked, "csrf_token": token,
        "points": 105 if checked else 100, "total_days": 2 if checked else 1,
        "continuous_days": 2 if checked else 1,
        "records": {today: {"points": 5, "is_makeup": 0}} if checked else {},
    }}


LOGIN_HTML = '''<form id="loginForm" method="POST">
<input type="hidden" name="login_mode" value="password">
<input name="company_dynamic" value=""><input name="username">
<input name="password" type="password"><input type="hidden" name="csrf" value="login-token">
</form><form id="codeLoginForm"><input name="login_email"></form>
<div id="geetest-captcha-login"></div>'''


class FrameHdrTests(unittest.TestCase):
    def setUp(self):
        self.sign = plugin.FrameHdrSign()
        self.sign.init_plugin({"enabled": True, "cookie": "PHPSESSID=fixture-cookie", "retry": 0, "notify": True})
        self.session = requests.Session()
        self.addCleanup(self.session.close)
        self.addCleanup(self.sign.stop_service)

    def run_with(self, results):
        with patch.object(self.sign, "_request", side_effect=results) as call:
            result = self.sign._run(self.session)
        return result, call

    def test_success_is_verified_and_csrf_is_multipart(self):
        result, call = self.run_with([response(state()), response({"success": True, "data": {"points": 5}}), response(state(True))])
        self.assertTrue(result["success"])
        self.assertEqual(result["award"], 5)
        self.assertEqual(call.call_args_list[1].kwargs["files"], {"csrf_token": (None, "fixture-csrf")})
        self.assertNotIn("fixture-csrf", json.dumps(self.sign.storage))

    def test_already_checked_does_not_post(self):
        result, call = self.run_with([response(state(True))])
        self.assertEqual(result["status"], "已签到")
        self.assertEqual(call.call_count, 1)

    def test_missing_csrf_never_posts(self):
        with patch.object(self.sign, "_request", return_value=response(state(token=""))) as call:
            with self.assertRaises(plugin.SignError):
                self.sign._run(self.session)
        self.assertEqual(call.call_count, 1)

    def test_timeout_post_reconciled_without_replay(self):
        result, call = self.run_with([response(state()), plugin.SignError("超时"), response(state(True))])
        self.assertTrue(result["success"])
        self.assertEqual(sum(c.args[1] == "POST" for c in call.call_args_list), 1)

    def test_unconfirmed_post_is_not_reported_success(self):
        result, call = self.run_with([response(state()), response({"success": True}), plugin.SignError("超时")])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "待确认")
        self.assertEqual(call.call_count, 3)

    def test_business_failure_not_replayed(self):
        result, call = self.run_with([response(state()), response({"success": False, "message": "csrf error"}), response(state())])
        self.assertFalse(result["success"])
        self.assertIn("校验令牌", result["message"])
        self.assertEqual(call.call_count, 3)

    def test_password_fallback_and_form_fields(self):
        self.sign._config.update(username="fixture-user", password="fixture-password")
        replies = [response(status=302, location="/api/login.php"), response(text=LOGIN_HTML),
                   response({"success": True, "data": {"is_locked": False, "attempts_left": 5}}),
                   response(status=302, location="/index.php"), response(state(True))]
        result, call = self.run_with(replies)
        self.assertTrue(result["success"])
        data = call.call_args_list[3].kwargs["data"]
        self.assertEqual(data["username"], "fixture-user")
        self.assertEqual(data["company_dynamic"], "")
        self.assertEqual(data["csrf"], "login-token")
        self.assertEqual(data["remember_me"], "on")
        self.assertNotIn("login_email", data)
        self.assertEqual(self.sign.get_data("login_blocked"), "")

    def test_wrong_password_blocks_future_automatic_login(self):
        self.sign._config.update(username="fixture-user", password="wrong")
        replies = [response(text=LOGIN_HTML), response({"success": True, "data": {"is_locked": False}}),
                   response(text=LOGIN_HTML), response(status=302, location="/login.php")]
        with patch.object(self.sign, "_request", side_effect=replies) as call:
            with self.assertRaises(plugin.AuthError):
                self.sign._login(self.session)
            count = call.call_count
            with self.assertRaises(plugin.AuthError):
                self.sign._login(self.session)
            self.assertEqual(count, call.call_count)
        self.assertEqual(self.sign.get_data("login_blocked"), self.sign._fingerprint())

    def test_manual_retry_clears_login_block_on_success(self):
        self.sign._config.update(username="fixture-user", password="corrected")
        self.sign.save_data("login_blocked", self.sign._fingerprint())
        replies = [response(text=LOGIN_HTML), response({"success": True, "data": {"is_locked": False}}),
                   response(status=302), response(state(True))]
        with patch.object(self.sign, "_request", side_effect=replies):
            self.sign._login(self.session, manual=True)
        self.assertEqual(self.sign.get_data("login_blocked"), "")

    def test_captcha_detection_does_not_submit(self):
        self.sign._config.update(username="fixture-user", password="fixture-password")
        with patch.object(self.sign, "_request", return_value=response(text=LOGIN_HTML + "<script>initGeetest4({})</script>")) as call:
            with self.assertRaisesRegex(plugin.AuthError, "人机验证"):
                self.sign._login(self.session)
        self.assertEqual(call.call_count, 1)

    def test_locked_account_does_not_submit(self):
        self.sign._config.update(username="fixture-user", password="fixture-password")
        with patch.object(self.sign, "_request", side_effect=[response(text=LOGIN_HTML),
                 response({"success": True, "data": {"is_locked": True}})]) as call:
            with self.assertRaises(plugin.AuthError):
                self.sign._login(self.session)
        self.assertEqual(call.call_count, 2)

    def test_network_exception_does_not_leak_credentials(self):
        with patch.object(self.session, "request", side_effect=requests.RequestException("https://secret:password@proxy")):
            with self.assertRaises(plugin.SignError) as exc:
                self.sign._request(self.session, "GET", "/index.php")
        self.assertNotIn("secret", str(exc.exception))
        self.assertNotIn("password", str(exc.exception))

    def test_cross_origin_login_action_is_blocked(self):
        with patch.object(self.session, "request") as call:
            with self.assertRaises(plugin.SignError):
                self.sign._request(self.session, "POST", "https://example.org/login", data={"password": "fixture"})
        call.assert_not_called()

    def test_redirects_not_automatically_followed(self):
        with patch.object(self.session, "request", return_value=response(status=302)) as call:
            self.sign._request(self.session, "GET", "/login.php")
        self.assertFalse(call.call_args.kwargs["allow_redirects"])

    def test_login_redirect_variants_are_auth_failures(self):
        for location in ("/login.php", "/api/login.php", "login.php", "../login.php",
                         "/api/login.php?return=checkin", "https://framehdr.com/api/login.php"):
            with self.subTest(location=location):
                reply = response(status=302, location=location)
                reply.url = "https://framehdr.com/api/checkin.php?action=init"
                with self.assertRaises(plugin.AuthError):
                    self.sign._json(reply)

    def test_foreign_or_unknown_redirect_is_not_auth_failure(self):
        for location in ("https://example.org/api/login.php", "http://framehdr.com/login.php",
                         "/maintenance.php", "//example.org/login.php", ""):
            with self.subTest(location=location):
                with self.assertRaises(plugin.SignError) as exc:
                    self.sign._json(response(status=302, location=location))
                self.assertNotIsInstance(exc.exception, plugin.AuthError)

    def test_api_login_redirect_without_credentials_has_actionable_error(self):
        with patch.object(self.sign, "_request", return_value=response(status=302, location="/api/login.php")) as call:
            with self.assertRaisesRegex(plugin.AuthError, "更新 Cookie 或配置用户名和密码"):
                self.sign._run(self.session)
        self.assertEqual(call.call_count, 1)

    def test_cookie_rotation_updates_config_and_cache(self):
        self.session.cookies.set("PHPSESSID", "rotated", domain="framehdr.com", path="/", secure=True)
        self.sign._save_session(self.session)
        with self.sign._session() as session:
            self.assertEqual(session.cookies.get("PHPSESSID"), "rotated")
        self.assertEqual(self.sign._config["cookie"], "PHPSESSID=rotated")
        self.assertEqual(self.sign.saved_config["cookie"], "PHPSESSID=rotated")
        self.assertEqual(self.sign.get_data("session")["fingerprint"], self.sign._fingerprint())

    def test_cookie_rotation_preserves_current_account_history(self):
        old = self.sign._fingerprint()
        self.sign._snapshot(state(True)["data"])
        self.sign.save_data("history", [{"fingerprint": old, "status": "成功"},
                                        {"fingerprint": "other-account", "status": "失败"}])
        self.session.cookies.set("PHPSESSID", "rotated", domain="framehdr.com", path="/")
        self.sign._save_session(self.session)
        self.assertNotEqual(old, self.sign._fingerprint())
        self.assertEqual(self.sign.get_data("account")["fingerprint"], self.sign._fingerprint())
        self.assertEqual(self.sign.get_data("history")[0]["fingerprint"], self.sign._fingerprint())
        self.assertEqual(self.sign.get_data("history")[1]["fingerprint"], "other-account")

    def test_cookie_export_excludes_foreign_expired_and_unrelated_paths(self):
        self.session.cookies.set("session", "active", domain="framehdr.com", path="/")
        self.session.cookies.set("foreign", "private", domain="example.org", path="/")
        self.session.cookies.set("expired", "private", domain="framehdr.com", path="/", expires=1)
        self.session.cookies.set("admin", "private", domain="framehdr.com", path="/admin")
        self.sign._save_session(self.session)
        self.assertEqual(self.sign.saved_config["cookie"], "session=active")

    def test_cookie_export_is_not_repeated_when_unchanged(self):
        self.session.cookies.set("PHPSESSID", "fixture-cookie", domain="framehdr.com", path="/")
        with patch.object(self.sign, "update_config") as save:
            self.sign._save_session(self.session)
        save.assert_not_called()

    def test_password_login_cookie_is_reused_after_reload_without_login(self):
        self.sign._config.update(cookie="", username="fixture-user", password="fixture-password")
        self.session.cookies.set("wangpan_session", "new-login", domain="framehdr.com", path="/")
        replies = [response(status=302, location="/api/login.php"), response(text=LOGIN_HTML),
                   response({"success": True, "data": {"is_locked": False}}),
                   response(status=302), response(state(True))]
        with patch.object(self.sign, "_request", side_effect=replies):
            self.sign._run(self.session)
        self.assertEqual(self.sign.saved_config["cookie"], "wangpan_session=new-login")
        self.sign.init_plugin(self.sign.saved_config)
        with self.sign._session() as session, patch.object(self.sign, "_login") as login, \
                patch.object(self.sign, "_request", return_value=response(state(True))):
            self.assertEqual(session.cookies.get("wangpan_session"), "new-login")
            self.sign._run(session)
        login.assert_not_called()

    def test_legacy_cache_cookie_backfilled_without_password_login(self):
        self.sign._config.update(cookie="", username="fixture-user", password="fixture-password")
        self.sign.save_data("session", {"fingerprint": self.sign._fingerprint(), "cookies": [
            {"name": "wangpan_session", "value": "legacy", "domain": "framehdr.com", "path": "/"}]})
        with self.sign._session() as session, patch.object(self.sign, "_login") as login, \
                patch.object(self.sign, "_request", return_value=response(state(True))):
            self.sign._run(session)
        login.assert_not_called()
        self.assertEqual(self.sign.saved_config["cookie"], "wangpan_session=legacy")

    def test_changed_credentials_ignore_previous_cache(self):
        self.session.cookies.set("PHPSESSID", "old", domain="framehdr.com", path="/")
        self.sign._save_session(self.session)
        self.sign._config["cookie"] = "PHPSESSID=new"
        with self.sign._session() as session:
            self.assertEqual(session.cookies.get("PHPSESSID"), "new")

    def test_cached_cookie_deletion_does_not_restore_old_cookie(self):
        self.sign._save_session(self.session)
        with self.sign._session() as session:
            self.assertFalse(list(session.cookies))

    def test_cookie_header_prefix_accepted(self):
        self.sign._config["cookie"] = "Cookie: PHPSESSID=abc; remember_me=xyz"
        with self.sign._session() as session:
            self.assertEqual(session.cookies.get("remember_me"), "xyz")

    def test_invalid_cookie_rejected(self):
        self.sign._config["cookie"] = "invalid"
        with self.assertRaises(plugin.SignError):
            self.sign._session()

    def test_failure_persisted_and_notification_safe(self):
        with patch.object(self.sign, "_run", side_effect=plugin.SignError("配置无效")):
            result = self.sign.check()
        self.assertFalse(result["success"])
        self.assertEqual(len(self.sign.get_data("history")), 1)
        self.assertEqual(len(self.sign.notifications), 1)
        self.assertNotIn("fixture-cookie", json.dumps(self.sign.notifications))

    def test_disabled_plugin_runs_only_manually(self):
        self.sign._config["enabled"] = False
        with patch.object(self.sign, "_run", return_value={"success": True, "status": "已签到", "message": "已签到"}) as call:
            self.assertIsNone(self.sign.check())
            self.assertTrue(self.sign.check(manual=True)["success"])
        self.assertEqual(call.call_count, 1)

    def test_history_bounded(self):
        self.sign.save_data("history", [{"time": str(i)} for i in range(100)])
        with patch.object(self.sign, "_run", side_effect=plugin.SignError("失败")):
            self.sign.check()
        self.assertEqual(len(self.sign.get_data("history")), 100)

    def test_page_uses_cache_without_network_or_secrets(self):
        self.sign._snapshot(state(True)["data"])
        with patch.object(self.sign, "_request") as call:
            page = json.dumps(self.sign.get_page(), ensure_ascii=False)
        call.assert_not_called()
        self.assertIn("已签到", page)
        self.assertNotIn("fixture-csrf", page)
        self.assertNotIn("fixture-cookie", page)
        self.assertNotIn(self.sign._fingerprint(), page)

    def test_stale_page_does_not_claim_today_signed(self):
        self.sign._snapshot(state(True)["data"])
        self.sign.storage["account"]["updated_at"] = "2000-01-01 09:00:00"
        self.assertIn("待查询", json.dumps(self.sign.get_page(), ensure_ascii=False))

    def test_configuration_forms_serializable_and_secret_masked(self):
        form, defaults = self.sign.get_form()
        json.dumps(form)
        self.assertFalse(defaults["onlyonce"])
        def walk(nodes):
            for node in nodes:
                yield node
                yield from walk(node.get("content", []))
        fields = {n["props"]["model"]: n["props"] for n in walk(form) if "model" in n.get("props", {})}
        for name in ("cookie", "password", "proxy"):
            self.assertEqual(fields[name]["type"], "password")

    def test_bad_cron_disables_service(self):
        self.sign.init_plugin({"enabled": True, "cron": "invalid"})
        self.assertEqual(self.sign.get_service(), [])
        self.assertIn("无效", json.dumps(self.sign.get_form(), ensure_ascii=False))

    def test_cron_uses_configured_timezone(self):
        self.assertEqual(str(self.sign.get_service()[0]["trigger"].timezone), "Asia/Shanghai")

    def test_onlyonce_is_reset_before_scheduling(self):
        with patch.object(plugin, "BackgroundScheduler") as scheduler:
            self.sign.init_plugin({"onlyonce": True})
        self.assertFalse(self.sign.saved_config["onlyonce"])
        scheduler.return_value.add_job.assert_called_once()
        self.assertEqual(scheduler.return_value.add_job.call_args.kwargs["kwargs"], {"manual": True})

    def test_malformed_status_is_not_treated_as_unsigned(self):
        data = state()
        data["data"]["has_checked_today"] = "false"
        with patch.object(self.sign, "_request", return_value=response(data)):
            with self.assertRaises(plugin.SignError):
                self.sign._init(self.session)

    def test_read_only_retry(self):
        self.sign._config["retry"] = 1
        with patch.object(self.sign._stop, "wait", return_value=False), patch.object(
                self.sign, "_init", side_effect=[plugin.SignError("超时"), state(True)["data"]]) as call:
            self.assertTrue(self.sign._initial_state(self.session)["has_checked_today"])
        self.assertEqual(call.call_count, 2)

    def test_service_error_does_not_attempt_password(self):
        self.sign._config.update(username="fixture-user", password="fixture-password")
        with patch.object(self.sign, "_request", return_value=response({"success": False}, status=500)), \
                patch.object(self.sign, "_login") as login:
            with self.assertRaises(plugin.SignError):
                self.sign._run(self.session)
        login.assert_not_called()

    def test_untrusted_response_message_not_in_history_or_notification(self):
        replies = [response(state()), response({"success": False, "message": "fixture-cookie fixture-password"}), response(state())]
        with patch.object(self.sign, "_request", side_effect=replies):
            self.sign.check()
        public = json.dumps([self.sign.get_data("history"), self.sign.notifications])
        self.assertNotIn("fixture-cookie", public)
        self.assertNotIn("fixture-password", public)

    def test_system_proxy_precedes_custom_proxy(self):
        self.sign._config.update(use_system_proxy=True, proxy="http://custom:8888")
        with patch.object(plugin.settings, "PROXY", {"https": "http://system:8888"}):
            with self.sign._session() as session:
                self.assertEqual(session.proxies, {"https": "http://system:8888"})
                self.assertFalse(session.trust_env)

    def test_stopped_plugin_never_runs(self):
        self.sign.stop_service()
        with patch.object(self.sign, "_run") as call:
            self.assertIsNone(self.sign.check(manual=True))
        call.assert_not_called()

    def test_page_hides_previous_account(self):
        self.sign._snapshot(state(True)["data"])
        self.sign._config["username"] = "another-account"
        page = json.dumps(self.sign.get_page(), ensure_ascii=False)
        self.assertIn("尚未同步", page)
        self.assertIn("待查询", page)

    def test_calendar_has_green_accessible_status_and_today(self):
        data = state(True)["data"]
        self.sign._snapshot(data)
        tree = self.sign._calendar(self.sign.get_data("account"))
        def walk(node):
            yield node
            for child in node.get("content", []):
                yield from walk(child)
        cells = [n for n in walk(tree) if "aria-label" in n.get("props", {})]
        today = next(c for c in cells if "，今天" in c["props"]["aria-label"])
        self.assertIn("已签到", today["props"]["aria-label"])
        self.assertIn("#e8f5ec", today["props"]["style"])
        self.assertIn("outline:2px", today["props"]["style"])
        self.assertIn("mdi-check-circle", json.dumps(today))
        self.assertEqual(len(cells), plugin.calendar.monthrange(self.sign._now().year, self.sign._now().month)[1])

    def test_calendar_makeup_and_missing_records_distinguished(self):
        self.sign._snapshot(state(True)["data"])
        account = self.sign.get_data("account")
        date = self.sign._now().strftime("%Y-%m-%d")
        account["records"][date]["is_makeup"] = 1
        tree = json.dumps(self.sign._calendar(account), ensure_ascii=False)
        self.assertIn("已补签", tree)
        self.assertIn("mdi-check-circle-outline", tree)
        self.assertIn("repeat(7,minmax(0,1fr))", tree)
        account["month"] = "2000-01"
        tree = json.dumps(self.sign._calendar(account), ensure_ascii=False)
        self.assertIn("本月已签 0 天", tree)


if __name__ == "__main__":
    unittest.main()

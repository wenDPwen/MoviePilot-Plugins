import calendar
import datetime
import hashlib
import re
from http.cookies import SimpleCookie
from threading import Event, RLock
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .site import decode_page, login_diagnostics, login_failure_reason, parse_calendar, parse_status, parse_login_form

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType


class SignError(Exception):
    pass


class AuthError(SignError):
    pass


class WnflbSign(_PluginBase):
    plugin_name = "福利吧自动签到"
    plugin_desc = "福利吧每日签到，支持 Cookie 与账号密码登录、积分统计、签到日历和结果通知。"
    plugin_icon = "https://raw.githubusercontent.com/wenDPwen/MoviePilot-Plugins/main/icons/wnflb-favicon.png"
    plugin_version = "1.0.5"
    plugin_author = "wen"
    author_url = "https://github.com/wenDPwen"
    author_icon = "https://raw.githubusercontent.com/wenDPwen/MoviePilot-Plugins/main/icons/author.jpg"
    plugin_config_prefix = "wnflbsign_"
    plugin_order = 33
    auth_level = 2
    BASE_URL = "https://www.wnflb2023.com"
    DEFAULTS = {
        "enabled": False, "notify": True, "onlyonce": False,
        "cron": "30 9 * * *", "cookie": "", "username": "", "password": "",
        "retry": 2, "use_system_proxy": False, "proxy": "",
        "questionid": "0", "answer": "",
    }

    def __init__(self):
        super().__init__()
        self._config = dict(self.DEFAULTS)
        self._lock = RLock()
        self._stop = Event()
        self._scheduler = None

    @staticmethod
    def _now():
        return datetime.datetime.now(pytz.timezone("Asia/Shanghai"))

    def init_plugin(self, config=None):
        self.stop_service()
        with self._lock:
            self._stop.clear()
            self._config = {**self.DEFAULTS, **(config or {})}
            for key in ("cookie", "username", "proxy", "cron"):
                self._config[key] = str(self._config.get(key) or "").strip()
            self._config["password"] = str(self._config.get("password") or "")
            self._config["cron"] = self._config["cron"] or self.DEFAULTS["cron"]
            try:
                self._config["retry"] = max(0, min(3, int(self._config["retry"])))
            except (ValueError, TypeError):
                self._config["retry"] = 2
            self._cron_error = ""
            try:
                CronTrigger.from_crontab(self._config["cron"], timezone=settings.TZ)
            except (ValueError, TypeError):
                self._cron_error = "定时表达式无效，请填写五段 Cron 表达式。"
                logger.error("福利吧：定时表达式无效")
            if self._config.get("onlyonce"):
                self._config["onlyonce"] = False
                self.update_config(dict(self._config))
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    self.check, "date", kwargs={"manual": True},
                    run_date=self._now() + datetime.timedelta(seconds=3),
                )
                self._scheduler.start()

    def get_state(self):
        return bool(self._config.get("enabled"))

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        return []

    def get_service(self):
        if not self.get_state() or getattr(self, "_cron_error", ""):
            return []
        return [{
            "id": "WnflbSign", "name": self.plugin_name,
            "trigger": CronTrigger.from_crontab(self._config["cron"], timezone=settings.TZ),
            "func": self.check, "kwargs": {},
        }]

    def stop_service(self):
        self._stop.set()
        if self._scheduler:
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def _fingerprint(self):
        values = [str(self._config.get(k, "")) for k in ("username", "password", "cookie", "questionid", "answer")]
        return hashlib.sha256("\0".join(values).encode()).hexdigest()

    def _session(self):
        session = requests.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Referer": self.BASE_URL + "/forum.php",
            "Origin": self.BASE_URL,
        })
        if self._config.get("use_system_proxy"):
            session.proxies.update(getattr(settings, "PROXY", None) or {})
        elif self._config.get("proxy"):
            proxy = self._config["proxy"]
            if urlparse(proxy).scheme not in ("http", "https", "socks5", "socks5h"):
                session.close()
                raise SignError("代理地址无效，请填写 http(s) 或 socks5 代理地址。")
            session.proxies.update({"http": proxy, "https": proxy})
        raw = self._config.get("cookie", "")
        if raw.lower().startswith("cookie:"):
            raw = raw[7:].strip()
        if raw:
            try:
                cookies = SimpleCookie()
                cookies.load(raw)
                if not cookies:
                    raise ValueError()
                for name, item in cookies.items():
                    session.cookies.set(name, item.value, domain="www.wnflb2023.com", path="/", secure=True)
            except Exception:
                session.close()
                raise SignError("Cookie 格式无效，请填写 name=value; name2=value2 格式。") from None
        cache = self.get_data("session") or {}
        if cache.get("fingerprint") == self._fingerprint():
            # The persisted jar supersedes the original cookie, including deletions/rotation.
            session.cookies.clear()
            for item in cache.get("cookies", []):
                if item.get("domain", "").lstrip(".") not in ("www.wnflb2023.com", "wnflb2023.com"):
                    continue
                session.cookies.set(**item)
        return session

    def _save_session(self, session):
        previous = self._fingerprint()
        request = requests.Request("GET", self.BASE_URL + "/plugin.php").prepare()
        cookie = requests.cookies.get_cookie_header(session.cookies, request) or ""
        if cookie and cookie != self._config.get("cookie"):
            config = {**self._config, "cookie": cookie}
            self.update_config(config)
            self._config = config
            current = self._fingerprint()
            # Automatic rotation is the same account, unlike a manual credential change.
            account = self.get_data("account") or {}
            if account.get("fingerprint") == previous:
                account["fingerprint"] = current
                self.save_data("account", account)
            history = self.get_data("history") or []
            changed = False
            for record in history:
                if record.get("fingerprint") == previous:
                    record["fingerprint"] = current
                    changed = True
            if changed:
                self.save_data("history", history)
            if self.get_data("login_blocked") == previous:
                self.save_data("login_blocked", "")
        self.save_data("session", {
            "fingerprint": self._fingerprint(),
            "cookies": [
                {"name": c.name, "value": c.value, "domain": c.domain,
                 "path": c.path, "secure": c.secure, "expires": c.expires}
                for c in session.cookies
                if c.domain.lstrip(".") in ("www.wnflb2023.com", "wnflb2023.com") and not c.is_expired()
            ],
        })

    def _request(self, session, method, path, **kwargs):
        if self._stop.is_set():
            raise SignError("任务已停止。")
        url = urljoin(self.BASE_URL + "/", path)
        parsed = urlparse(url)
        if (parsed.scheme, parsed.netloc) != ("https", "www.wnflb2023.com"):
            raise SignError("站点返回了非本站地址，已停止请求以保护凭证。")
        try:
            return session.request(method, url, timeout=(10, 25), allow_redirects=False, **kwargs)
        except requests.RequestException:
            # Never propagate requests exceptions: they may contain proxy credentials.
            raise SignError("网络请求失败或超时，请检查网络及代理配置。") from None

    @staticmethod
    def _html(response):
        if response.status_code in (301, 302, 303, 307, 308):
            target = urlparse(urljoin(response.url or WnflbSign.BASE_URL + '/', response.headers.get('Location', '')))
            if ((target.scheme, target.netloc) == ('https', 'www.wnflb2023.com')
                    and target.path == '/member.php'
                    and parse_qs(target.query).get('action') == ['login']):
                raise AuthError('登录态已失效。')
            raise SignError('站点发生未知跳转，已停止请求。')
        if response.status_code == 401:
            raise AuthError('登录态已失效。')
        if response.status_code != 200:
            raise SignError('站点返回 HTTP %s，请检查网络或站点状态。' % response.status_code)
        try:
            return decode_page(response.content, response.headers.get('Content-Type', ''))
        except ValueError:
            raise SignError('站点返回空页面或无法识别的编码，请检查站点及代理响应。') from None

    def _init(self, session):
        html = self._html(self._request(session, 'GET', '/home.php?mod=spacecp&ac=credit&showcredit=1'))
        try:
            data = parse_status(html)
        except ValueError:
            raise SignError('无法识别论坛签到状态，可能出现验证页或站点页面已变化。') from None
        if data is None:
            raise AuthError('登录态已失效。')
        month = self._now().strftime('%Y%m')
        data['records'] = {}
        try:
            text = self._html(self._request(session, 'GET', '/plugin.php', params={
                'id': 'fx_checkin:ajax', 'date': month, 'inajax': 1,
            }))
            data['records'] = parse_calendar(text, month)
        except AuthError:
            raise
        except (SignError, ValueError, SyntaxError):
            # Calendar is optional; a failed calendar must not hide an authoritative status.
            data['calendar_warning'] = '签到日历同步失败，当前页面状态仍可用。'
        return data

    def _initial_state(self, session):
        for attempt in range(self._config['retry'] + 1):
            try:
                return self._init(session)
            except AuthError:
                raise
            except SignError:
                if attempt == self._config['retry'] or self._stop.wait(2):
                    raise

    def _login(self, session, manual=False):
        if not self._config.get('username') or not self._config.get('password'):
            raise AuthError('Cookie 缺失或失效，请更新 Cookie 或配置论坛用户名和密码。')
        fingerprint = self._fingerprint()
        if not manual and self.get_data('login_blocked') == fingerprint:
            raise AuthError('密码登录已暂停；请检查凭证后保存并开启「立即运行一次」重试。')
        login_url = self.BASE_URL + '/member.php?mod=logging&action=login'
        html = self._html(self._request(session, 'GET', login_url))
        try:
            form = parse_login_form(html)
        except ValueError:
            raise SignError('登录表单发生变化，请更新插件或改用 Cookie。') from None
        if form.captcha or re.search(r'updateseccode\(|initGeetest|turnstile\.render|g-recaptcha', html, re.I):
            raise AuthError('登录需要验证码，请在浏览器完成登录并更新 Cookie。')
        question = str(self._config.get('questionid') or '0')
        answer = str(self._config.get('answer') or '')
        if question not in [str(i) for i in range(8)] or (question != '0' and not answer):
            raise SignError('请正确配置安全问题及答案；未设置安全问题时选择「未设置」。')
        form.fields.update(username=self._config['username'], password=self._config['password'],
                           questionid=question, answer=answer, cookietime='2592000')
        query = parse_qs(urlparse(form.action).query)
        if urlparse(form.action).path not in ('member.php', '/member.php') or query.get('action') != ['login']:
            raise SignError('登录提交地址异常，已停止传递凭证。')
        action = urlparse(form.action)
        params = [(key, value) for key, value in parse_qsl(action.query) if key != 'inajax']
        params.append(('inajax', '1'))
        action = urlunparse(action._replace(query=urlencode(params)))
        self.save_data('login_blocked', fingerprint)
        # Discuz ajaxpost submits to an iframe with inajax=1 and the login-page Referer.
        response = self._request(session, 'POST', action, data=form.fields, headers={'Referer': login_url})
        reason = ''
        body = ''
        if response.status_code == 200:
            body = self._html(response)
            reason = login_failure_reason(body)
        elif response.status_code not in (301, 302, 303, 307, 308):
            reason = '登录提交返回 HTTP %s，请检查站点或网络限制。' % response.status_code
        try:
            data = self._init(session)
        except AuthError:
            detail = reason or '提交后论坛仍显示未登录，未返回可识别的拒绝原因；请先在浏览器核对用户名、密码及安全问题。'
            diagnostic = login_diagnostics(response, body, session.cookies)
            raise AuthError(detail + ' 诊断：' + diagnostic + '。自动密码登录已暂停。') from None
        except SignError as exc:
            detail = reason or ('登录提交后状态复查失败：' + str(exc))
            raise AuthError(detail + ' 暂不能判定密码错误；自动密码登录已暂停。') from None
        self.save_data('login_blocked', '')
        return data

    def _snapshot(self, data):
        snapshot = {k: data.get(k) for k in ('points', 'score', 'coins', 'continuous_days', 'total_days',
                                            'has_checked_today', 'records', 'calendar_warning')}
        snapshot['updated_at'] = self._now().strftime('%Y-%m-%d %H:%M:%S')
        snapshot['month'] = self._now().strftime('%Y-%m')
        snapshot['fingerprint'] = self._fingerprint()
        self.save_data('account', snapshot)

    def _run(self, session, manual=False):
        try:
            data = self._initial_state(session)
        except AuthError:
            data = self._login(session, manual)
        self._save_session(session)
        self._snapshot(data)
        base = {'balance': data.get('points'), 'streak': data.get('continuous_days'), 'award': 0}
        if data['has_checked_today']:
            return {**base, 'success': True, 'status': '已签到', 'message': '今天已经签到，无需重复提交。'}
        path = data.get('sign_path') or ''
        query = parse_qs(urlparse(path).query)
        if urlparse(path).path not in ('plugin.php', '/plugin.php') or query.get('id') != ['fx_checkin:checkin']:
            raise SignError('签到地址异常，已停止提交。')
        try:
            # This GET changes state. Never retry it as an ordinary read request.
            self._request(session, 'GET', path + '&infloat=yes&handlekey=fx_checkin&inajax=1&ajaxtarget=fwin_content_fx_checkin')
        except SignError:
            pass
        try:
            after = self._initial_state(session)
            self._save_session(session)
            self._snapshot(after)
        except SignError:
            return {**base, 'success': False, 'status': '待确认',
                    'message': '签到已尝试，但无法复查结果；未重复提交，下次运行将先查询状态。'}
        if after['has_checked_today']:
            record = after.get('records', {}).get(self._now().strftime('%Y-%m-%d'), {})
            return {'success': True, 'status': '成功', 'message': '签到已确认成功。',
                    'award': record.get('points'), 'balance': after.get('points'), 'streak': after.get('continuous_days')}
        return {**base, 'success': False, 'status': '失败',
                'message': '站点未确认签到成功，本次未重复提交；请在浏览器检查验证提示或稍后重试。'}

    def check(self, manual=False):
        if self._stop.is_set() or (not manual and not self.get_state()):
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            try:
                if not self._config.get("cookie") and not (self._config.get("username") and self._config.get("password")):
                    raise SignError("请配置 Cookie，或同时填写用户名和密码。")
                with self._session() as session:
                    result = self._run(session, manual)
            except SignError as exc:
                result = {"success": False, "status": "失败", "message": str(exc)}
            except Exception:
                result = {"success": False, "status": "失败", "message": "插件执行异常，请检查配置或更新插件。"}
            result["time"] = self._now().strftime("%Y-%m-%d %H:%M:%S")
            result["source"] = "手动" if manual else "定时"
            result["fingerprint"] = self._fingerprint()
            history = self.get_data("history") or []
            self.save_data("history", (history + [result])[-100:])
            logger.info("福利吧签到：%s" % result["message"])
            if self._config.get("notify"):
                try:
                    self.post_message(mtype=NotificationType.Plugin,
                                      title="福利吧签到 · " + result["status"],
                                      text="\n".join([result["message"],
                                                      "福利奖励：%s | 福利余额：%s | 连续：%s 天" % (
                                                          result.get("award", "—"), result.get("balance", "—"), result.get("streak", "—")),
                                                      result["time"]]))
                except Exception:
                    logger.warning("福利吧：通知发送失败，签到记录已保留。")
            return result
        finally:
            self._lock.release()

    @staticmethod
    def _node(component, text=None, content=None, **props):
        node = {"component": component}
        if props:
            node["props"] = props
        if text is not None:
            node["text"] = str(text)
        if content is not None:
            node["content"] = content
        return node

    def get_form(self):
        n = self._node

        def field(model, label, hint, kind="VTextField", md=6, **props):
            return n("VCol", content=[n(kind, model=model, label=label, hint=hint,
                                        **{"persistent-hint": True, **props})], cols=12, md=md)

        def card(title, icon, fields):
            return n("VCard", content=[
                n("VCardTitle", content=[n("VIcon", icon, color="primary", **{"class": "mr-2"}), n("span", title)]),
                n("VDivider"), n("VCardText", content=[n("VRow", content=fields)]),
            ], **{"class": "mt-3", "variant": "outlined"})

        cards = [
            card("基础设置", "mdi-cog", [
                field("enabled", "启用插件", "开启后按计划自动签到", "VSwitch"),
                field("notify", "签到结果通知", "成功、已签到、失败及待确认均发送通知", "VSwitch"),
            ]),
            card("登录设置", "mdi-account-key", [
                field("username", "论坛用户名", "Cookie 失效时使用账号密码登录", autocomplete="username"),
                field("password", "登录密码", "保存在 MoviePilot 配置中，不写入日志", type="password", autocomplete="new-password"),
                field("questionid", "安全问题（可选）", "与论坛账号设置一致；未设置则保留默认值", "VSelect", items=[
                    {"title": label, "value": str(i)} for i, label in enumerate([
                        "未设置", "母亲的名字", "爷爷的名字", "父亲出生的城市", "您其中一位老师的名字",
                        "您个人计算机的型号", "您最喜欢的餐馆名称", "驾驶执照最后四位数字",
                    ])]),
                field("answer", "安全问题答案", "仅设置了安全问题时填写；不记录到日志", type="password", autocomplete="off"),
                field("cookie", "Cookie（自动保存 / 可手动填写）", "登录成功后自动回填并优先复用；重新打开配置可查看。可仅填写账号密码", md=12, type="password"),
            ]),
            card("签到设置", "mdi-calendar-check", [
                field("cron", "定时表达式", "默认每天 09:30；使用 MoviePilot 配置的时区"),
                field("retry", "状态查询重试次数", "仅重试只读查询，不盲目重试签到或密码登录", "VSelect", items=[0, 1, 2, 3]),
                field("use_system_proxy", "使用 MoviePilot 系统代理", "开启后忽略自定义代理；两者均未设置则直连", "VSwitch", md=12),
                field("proxy", "自定义代理（可选）", "例如 http://127.0.0.1:7890；留空则直连", md=12, type="password"),
            ]),
            card("手动执行", "mdi-play-circle", [
                field("onlyonce", "立即运行一次", "保存后执行真实签到并自动关闭此开关；也会允许再尝试一次被暂停的密码登录", "VSwitch", md=12),
            ]),
            n("VAlert", "凭证按 MoviePilot 配置/插件数据保存，界面掩码不等于加密，请保护备份和管理权限。遇验证码需浏览器登录后更新 Cookie；不自动补签、不消耗积分。",
              type="info", variant="tonal", **{"class": "mt-3"}),
        ]
        if getattr(self, "_cron_error", ""):
            cards.insert(0, n("VAlert", self._cron_error, type="error", variant="tonal"))
        return [n("VForm", content=cards)], dict(self.DEFAULTS)

    def get_page(self):
        n = self._node
        account = self.get_data("account") or {}
        fingerprint = self._fingerprint()
        if account.get("fingerprint") != fingerprint:
            account = {}
        history = [r for r in (self.get_data("history") or []) if r.get("fingerprint") == fingerprint]
        today = self._now().strftime("%Y-%m-%d")
        fresh = str(account.get("updated_at", "")).startswith(today)
        status = ("已签到" if account.get("has_checked_today") else "未签到") if fresh else "待查询"
        kpis = [("福利余额", account.get("points", "—"), "mdi-wallet", "info"),
                ("连续签到", account.get("continuous_days", "—"), "mdi-fire", "warning"),
                ("累计签到天数", account.get("total_days", "—"), "mdi-calendar-check", "success"),
                ("今日状态", status, "mdi-check-circle", "primary")]
        cards = []
        for label, value, icon, color in kpis:
            cards.append(n("VCol", content=[n("VCard", content=[n("VCardText", content=[
                n("VIcon", icon, color=color, size=28),
                n("div", value, **{"class": "text-h6 font-weight-bold mt-2"}),
                n("div", label, **{"class": "text-caption text-medium-emphasis"}),
            ], **{"class": "text-center"})], variant="tonal", **{"class": "h-100"})], cols=6, md=6))
        page = [n("VRow", content=cards, dense=True), n("VAlert",
                "最近同步：%s。页面展示缓存，不会触发登录或签到。" % account.get("updated_at", "尚未同步"),
                type="info", variant="tonal", **{"class": "my-3"})]
        if history:
            latest = history[-1]
            page.append(n("VAlert", latest["message"], type="success" if latest.get("success") else "warning",
                          variant="tonal", **{"class": "mb-3"}))
        page.append(n("VAlert", "论坛总积分：%s · 金币：%s。签到奖励为「福利」，不是金币；总积分由论坛规则换算。" % (
            account.get("score", "—"), account.get("coins", "—")), type="info", variant="tonal", **{"class": "mb-3"}))
        if account.get("calendar_warning"):
            page.append(n("VAlert", account["calendar_warning"], type="warning", variant="tonal", **{"class": "mb-3"}))
        page.append(self._calendar(account))
        rows = []
        for item in reversed(history[-30:]):
            values = [item.get(k, "—") for k in ("time", "source", "status", "award", "balance", "message")]
            rows.append(n("tr", content=[n("td", value if value is not None else "—") for value in values]))
        table = n("VTable", content=[
            n("thead", content=[n("tr", content=[n("th", t) for t in ("执行时间", "来源", "状态", "福利奖励", "福利余额", "说明")])]),
            n("tbody", content=rows),
        ], density="comfortable") if rows else n("VAlert", "暂无记录，请配置登录信息后执行一次签到。", type="info", variant="tonal")
        page.append(n("VCard", content=[n("VCardTitle", "签到记录 · 最近 30 次"),
                    n("VCardText", content=[table], style="overflow-x:auto")], variant="outlined", **{"class": "mt-3"}))
        return page

    def _calendar(self, account):
        n = self._node
        now = self._now()
        records = (account.get("records") or {}) if account.get("month") == now.strftime("%Y-%m") else {}
        fresh = str(account.get("updated_at", "")).startswith(now.strftime("%Y-%m-%d"))
        cells = [n("div", d, **{"class": "text-caption text-medium-emphasis text-center py-2"}) for d in "一二三四五六日"]
        signed = 0
        for week in calendar.Calendar().monthdayscalendar(now.year, now.month):
            for day in week:
                if not day:
                    cells.append(n("div", **{"aria-hidden": "true"}))
                    continue
                key = "%04d-%02d-%02d" % (now.year, now.month, day)
                record = records.get(key)
                checked = isinstance(record, dict)
                makeup = checked and bool(record.get("is_makeup"))
                today = day == now.day
                signed += int(checked)
                label = "已补签" if makeup else "已签到"
                if not checked:
                    label = "未到" if day > now.day else ("待签到" if today and fresh else "无记录")
                style = ("min-width:0;min-height:88px;border-radius:12px;padding:8px 2px;"
                         "display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;")
                if checked:
                    style += "background:#e8f5ec;color:#166534;border:1px solid #b6dfc2;"
                else:
                    style += "background:rgba(var(--v-theme-on-surface),0.025);border:1px solid rgba(var(--v-theme-on-surface),0.09);"
                if today:
                    style += "outline:2px solid rgb(var(--v-theme-primary));outline-offset:-2px;"
                status = [n("VIcon", "mdi-check-circle-outline" if makeup else "mdi-check-circle",
                            size=14, **{"aria-hidden": "true"})] if checked else []
                status.append(n("span", label))
                children = [n("div", "%02d" % day, style="font-size:18px;font-weight:700;line-height:1.2;font-variant-numeric:tabular-nums"),
                            n("div", content=status, style="display:flex;align-items:center;justify-content:center;gap:2px;font-size:12px;flex-wrap:wrap;line-height:1.3")]
                if today:
                    children.append(n("span", "今天", **{"class": "text-primary", "style": "font-size:11px;line-height:1.2;font-weight:600"}))
                cells.append(n("div", content=children, style=style,
                               **{"aria-label": "%s，%s%s" % (key, label, "，今天" if today else ""), "title": "%s · %s" % (key, label)}))
        heading = n("div", content=[n("span", now.strftime("%Y年%m月签到日历"), **{"class": "text-subtitle-1 font-weight-bold"}),
                                    n("VChip", "本月已签 %s 天" % signed, size="small", color="success", variant="tonal")],
                    style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap")
        legend = n("div", content=[n("VChip", "已签到", color="success", size="small", variant="tonal", **{"prepend-icon": "mdi-check-circle"}),
                                   n("VChip", "已补签", color="success", size="small", variant="outlined", **{"prepend-icon": "mdi-check-circle-outline"}),
                                   n("VChip", "今天", color="primary", size="small", variant="outlined")],
                   style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px")
        return n("VCard", content=[n("VCardText", content=[heading,
                 n("div", content=cells, style="display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:clamp(3px,1vw,8px);margin-top:12px"),
                 legend, n("div", "按最近同步数据展示；无记录不代表未签到。本插件不自动补签。",
                           **{"class": "text-caption text-medium-emphasis mt-3"})])], variant="outlined")

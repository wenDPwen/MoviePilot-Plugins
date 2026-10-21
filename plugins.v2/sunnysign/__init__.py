import calendar
import datetime
import hashlib
import math
from http.cookies import SimpleCookie
from threading import Event, RLock
from urllib.parse import urljoin, urlparse

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


class SunnySign(_PluginBase):
    plugin_name = "Sunny签到"
    plugin_desc = "SunnyPT 自动签到，支持 Cookie 与账号密码、魔力奖励统计、签到日历和通知。"
    plugin_icon = "https://raw.githubusercontent.com/wenDPwen/MoviePilot-Plugins/main/icons/sunny-logo.png"
    plugin_version = "1.0.2"
    plugin_author = "wen"
    author_url = "https://github.com/wenDPwen"
    author_icon = "https://raw.githubusercontent.com/wenDPwen/MoviePilot-Plugins/main/icons/author.jpg"
    plugin_config_prefix = "sunnysign_"
    plugin_order = 34
    auth_level = 2
    BASE_URL = "https://sunnypt.top"
    API_URL = "https://api.sunnypt.top"
    DEFAULTS = {
        "enabled": False, "notify": True, "onlyonce": False,
        "cron": "30 9 * * *", "cookie": "", "username": "", "password": "",
        "retry": 2, "use_system_proxy": False, "proxy": "",
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
            self._config = {key: (config or {}).get(key, value) for key, value in self.DEFAULTS.items()}
            for key in ("cookie", "username", "proxy", "cron"):
                self._config[key] = str(self._config.get(key) or "").strip()
            self._config['password'] = str(self._config.get('password') or '')
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
                logger.error("Sunny：定时表达式无效")
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
            "id": "SunnySign", "name": self.plugin_name,
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
        values = [self._config.get(key, '') for key in ('cookie', 'username', 'password')]
        return hashlib.sha256('\0'.join(values).encode()).hexdigest()

    def _session(self, web=True):
        session = requests.Session()
        session.trust_env = False
        session.headers.update({'User-Agent': 'Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36',
                                'Accept': 'application/json', 'Origin': self.BASE_URL,
                                'Referer': self.BASE_URL + '/user/attendance'})
        if self._config.get('use_system_proxy'):
            session.proxies.update(getattr(settings, 'PROXY', None) or {})
        elif self._config.get('proxy'):
            proxy = self._config['proxy']
            if urlparse(proxy).scheme not in ('http', 'https', 'socks5', 'socks5h'):
                session.close()
                raise SignError('代理地址无效。')
            session.proxies.update({'http': proxy, 'https': proxy})
        if web:
            raw = self._config.get('cookie', '')
            if raw.lower().startswith('cookie:'):
                raw = raw[7:].strip()
            try:
                cookies = SimpleCookie()
                cookies.load(raw)
                if (raw and not cookies) or '\n' in raw or '\r' in raw:
                    raise ValueError()
                for name, item in cookies.items():
                    session.cookies.set(name, item.value, domain='sunnypt.top', path='/', secure=True)
            except Exception:
                session.close()
                raise SignError('Cookie 格式无效，请填写完整 name=value; name2=value2。') from None
            cached = self.get_data('session') or {}
            if cached.get('fingerprint') == self._fingerprint():
                session.cookies.clear()
                for item in cached.get('cookies', []):
                    if item.get('domain', '').lstrip('.') == 'sunnypt.top':
                        session.cookies.set(**item)
        return session

    def _save_session(self, web):
        previous = self._fingerprint()
        request = requests.Request('GET', self.BASE_URL + '/api/auth/session').prepare()
        cookie = requests.cookies.get_cookie_header(web.cookies, request) or ''
        if cookie and cookie != self._config.get('cookie'):
            config = {**self._config, 'cookie': cookie}
            self.update_config(config)
            self._config = config
            current = self._fingerprint()
            account = self.get_data('account') or {}
            if account.get('fingerprint') == previous:
                account['fingerprint'] = current
                self.save_data('account', account)
            history = self.get_data('history') or []
            changed = False
            for row in history:
                if row.get('fingerprint') == previous:
                    row['fingerprint'] = current
                    changed = True
            if changed:
                self.save_data('history', history)
        self.save_data('session', {
            'fingerprint': self._fingerprint(),
            'cookies': [{'name': c.name, 'value': c.value, 'domain': c.domain,
                         'path': c.path, 'secure': c.secure, 'expires': c.expires}
                        for c in web.cookies if c.domain.lstrip('.') == 'sunnypt.top' and not c.is_expired()],
        })

    def _request(self, session, method, path, token=None, **kwargs):
        web = token is None
        base = self.BASE_URL if web else self.API_URL
        url = urljoin(base + '/', path)
        parsed = urlparse(url)
        allowed = {'/api/auth/session', '/api/auth/session/login'} if web else {
            '/api/v1/attendance/status', '/api/v1/attendance/monthly', '/api/v1/attendance/check-in'}
        if (parsed.scheme, parsed.netloc) != ('https', urlparse(base).netloc) or parsed.path not in allowed:
            raise SignError('非预期的站点接口，已停止请求以保护凭证。')
        if self._stop.is_set():
            raise SignError('任务已停止。')
        if (method, parsed.path) not in {
            ('GET', '/api/auth/session'), ('POST', '/api/auth/session/login'),
            ('GET', '/api/v1/attendance/status'), ('GET', '/api/v1/attendance/monthly'),
            ('POST', '/api/v1/attendance/check-in'),
        }:
            raise SignError('接口方法不符合预期，已停止请求。')
        headers = {}
        if not web:
            if 'Cookie' in session.headers or list(session.cookies):
                raise SignError('API 会话不应包含 Cookie，已停止请求。')
            headers['Authorization'] = 'Bearer ' + token
        try:
            response = session.request(method, url, headers=headers, timeout=(10, 25), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise SignError('网络请求失败或超时，请检查网络及代理配置。') from None
        finally:
            # Ignore API Set-Cookie: only the explicitly configured website cookie is used.
            if not web:
                session.cookies.clear()
        if response.status_code == 401:
            raise AuthError('Cookie 或临时登录令牌已失效，请在浏览器登录 SunnyPT 后更新 Cookie。')
        if response.status_code in (301, 302, 303, 307, 308):
            raise SignError('站点接口发生跳转，未跟随跳转；请检查 Cookie 或站点状态。')
        if not 200 <= response.status_code < 300:
            raise SignError('站点返回 HTTP %s；可能存在限流、访问验证或网络异常。' % response.status_code)
        try:
            result = response.json()
        except ValueError:
            raise SignError('站点未返回 JSON，可能需要在浏览器完成人工验证。') from None
        if not isinstance(result, dict):
            raise SignError('接口响应格式异常。')
        if web and result.get('error'):
            raise AuthError('会话请求未成功，请检查凭证或在浏览器处理二次验证。')
        code = result.get('code')
        if code in (401, 401000):
            raise AuthError('登录态已失效，请更新 Cookie。')
        if not web and (type(code) is not int or code != 0):
            raise SignError('站点拒绝了请求，请检查站点状态；未输出原始响应以保护隐私。')
        if web and code is not None and (type(code) is not int or code != 0):
            raise SignError('本站会话查询未成功。')
        return result.get('data')

    def _token(self, web):
        data = self._request(web, 'GET', '/api/auth/session')
        if data is None:
            raise AuthError('Cookie 缺失或失效，请重新登录网站并填写完整 Cookie。')
        if not isinstance(data, dict):
            raise SignError('会话接口数据异常。')
        token = data.get('accessToken')
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise AuthError('Cookie 未能获取临时登录令牌，请更新 Cookie。')
        return token

    def _login(self, web, manual=False):
        if not self._config.get('username') or not self._config.get('password'):
            raise AuthError('Cookie 已失效，请更新 Cookie，或填写用户名和密码。')
        fingerprint = self._fingerprint()
        if not manual and self.get_data('login_blocked') == fingerprint:
            raise AuthError('自动密码登录已暂停；请检查凭证后开启「立即运行一次」，或更新 Cookie。')
        # Suspend before sending: timeout must not cause repeated password submissions.
        self.save_data('login_blocked', fingerprint)
        web.cookies.clear()
        try:
            self._request(web, 'POST', '/api/auth/session/login', json={
                'username': self._config['username'], 'password': self._config['password'],
                'code': '', 'rememberMe': True,
            })
            token = self._token(web)
            if not requests.cookies.get_cookie_header(web.cookies, requests.Request('GET', self.BASE_URL + '/api/auth/session').prepare()):
                raise AuthError('登录响应未设置 Cookie。')
        except SignError:
            raise AuthError('密码登录未确认成功，已暂停自动尝试；请检查密码、登录限制或二步验证。开启二步验证的账号请浏览器登录后更新 Cookie。') from None
        self.save_data('login_blocked', '')
        self._save_session(web)
        return token

    def _status(self, api, token):
        data = self._request(api, 'GET', '/api/v1/attendance/status', token=token)
        if not isinstance(data, dict) or not isinstance(data.get('checked_in'), bool):
            raise SignError('签到状态字段异常，已停止签到。')
        for key in ('days', 'total_days', 'points', 'makeup_cards'):
            if key in data and (type(data[key]) not in (int, float) or not math.isfinite(data[key]) or data[key] < 0):
                raise SignError('签到统计字段异常。')
        return data

    def _read_status(self, api, token):
        for attempt in range(self._config['retry'] + 1):
            try:
                return self._status(api, token)
            except AuthError:
                raise
            except SignError:
                if attempt == self._config['retry'] or self._stop.wait(2):
                    raise

    def _snapshot(self, api, token, data):
        now = self._now()
        snapshot = {'points': data.get('points'), 'continuous_days': data.get('days'),
                    'total_days': data.get('total_days'), 'makeup_cards': data.get('makeup_cards'),
                    'has_checked_today': data['checked_in'], 'records': {},
                    'month': now.strftime('%Y-%m'), 'updated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'fingerprint': self._fingerprint()}
        try:
            monthly = self._request(api, 'GET', '/api/v1/attendance/monthly', token=token,
                                    params={'year': now.year, 'month': now.month})
            if not isinstance(monthly, dict) or not isinstance(monthly.get('records'), list):
                raise SignError('日历格式异常。')
            for record in monthly['records']:
                if not isinstance(record, dict):
                    continue
                date = str(record.get('date', ''))
                try:
                    parsed = datetime.date.fromisoformat(date)
                except ValueError:
                    continue
                if parsed.strftime('%Y-%m') == snapshot['month']:
                    # Only date is required by the current frontend; do not assume undocumented reward/type fields.
                    snapshot['records'][date] = {'is_makeup': False}
        except SignError:
            snapshot['calendar_warning'] = '月度签到记录暂未同步；下方仅保留本次确认的今日状态。'
        if data['checked_in']:
            snapshot['records'][now.strftime('%Y-%m-%d')] = {'points': data.get('points'), 'is_makeup': False}
        self.save_data('account', snapshot)

    def _run(self, web, api, manual=False):
        logged_in = False
        try:
            token = self._token(web)
        except AuthError:
            token = self._login(web, manual)
            logged_in = True
        try:
            before = self._read_status(api, token)
        except AuthError:
            if logged_in:
                raise
            token = self._login(web, manual)
            before = self._read_status(api, token)
        self._save_session(web)
        def outcome(data, status, success, message):
            return {'success': success, 'status': status, 'message': message,
                    'award': data.get('points') if data.get('checked_in') else None,
                    'streak': data.get('days'), 'total_days': data.get('total_days')}
        if before['checked_in']:
            self._snapshot(api, token, before)
            return outcome(before, '已签到', True, '今天已经签到，仅同步状态，未重复提交；奖励为今日站点奖励。')
        day = self._now().date()
        try:
            self._request(api, 'POST', '/api/v1/attendance/check-in', token=token)
        except SignError:
            pass
        try:
            after = self._read_status(api, token)
        except SignError:
            return outcome(before, '待确认', False, '签到已尝试但复查失败，未重复提交；下次运行会先查询状态。')
        self._snapshot(api, token, after)
        if self._now().date() != day:
            return outcome(after, '待确认', False, '执行期间跨越北京时间午夜，无法可靠确认本次结果；下次运行将重新查询。')
        if after['checked_in']:
            return outcome(after, '成功', True, '签到已通过站点状态复查确认成功。')
        return outcome(after, '失败', False, '站点仍显示未签到，本次未重复提交；请检查站点或更新 Cookie 后重试。')

    def check(self, manual=False):
        if self._stop.is_set() or (not manual and not self.get_state()):
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            try:
                if not self._config.get("cookie") and not (self._config.get('username') and self._config.get('password')):
                    raise SignError("请填写 SunnyPT Cookie，或用户名和密码。")
                with self._session(web=True) as session, self._session(web=False) as api:
                    result = self._run(session, api, manual)
            except SignError as exc:
                result = {"success": False, "status": "失败", "message": str(exc)}
            except Exception:
                result = {"success": False, "status": "失败", "message": "插件执行异常，请检查配置或更新插件。"}
            result["time"] = self._now().strftime("%Y-%m-%d %H:%M:%S")
            result["source"] = "手动" if manual else "定时"
            result["fingerprint"] = self._fingerprint()
            history = self.get_data("history") or []
            self.save_data("history", (history + [result])[-100:])
            logger.info("Sunny签到：%s" % result["message"])
            if self._config.get("notify"):
                try:
                    self.post_message(mtype=NotificationType.Plugin,
                                      title="Sunny签到 · " + result["status"],
                                      text="\n".join([result["message"],
                                                      "今日奖励：%s 魔力 | 连续：%s 天 | 累计：%s 天" % (
                                                          result.get("award", "—"), result.get("streak", "—"), result.get("total_days", "—")),
                                                      result["time"]]))
                except Exception:
                    logger.warning("Sunny：通知发送失败，签到记录已保留。")
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
                field("username", "SunnyPT 用户名", "Cookie 缺失或失效时用于自动登录", autocomplete="username"),
                field("password", "登录密码", "保存在 MoviePilot 配置中，不写入日志", type="password", autocomplete="new-password"),
                field("cookie", "Cookie（自动保存 / 可手动填写）", "优先使用 Cookie；登录成功后自动回填。重新打开配置可查看", md=12, type="password"),
            ]),
            card("签到设置", "mdi-calendar-check", [
                field("cron", "定时表达式", "默认每天 09:30；使用 MoviePilot 配置的时区"),
                field("retry", "状态查询重试次数", "仅重试只读查询，不盲目重试签到请求", "VSelect", items=[0, 1, 2, 3]),
                field("use_system_proxy", "使用 MoviePilot 系统代理", "开启后忽略自定义代理；两者均未设置则直连", "VSwitch", md=12),
                field("proxy", "自定义代理（可选）", "例如 http://127.0.0.1:7890；留空则直连", md=12, type="password"),
            ]),
            card("手动执行", "mdi-play-circle", [
                field("onlyonce", "立即运行一次", "保存后执行一次；当天已签仅同步。允许一次被暂停的密码登录，请先检查凭证", "VSwitch", md=12),
            ]),
            n("VAlert", "Cookie 优先，账号密码后备；开启二步验证时请在浏览器登录后更新 Cookie。临时令牌只用于 api.sunnypt.top，不持久化。凭证掩码不等于加密，请保护配置和备份。不自动补签、不消耗补签卡。",
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
        kpis = [("今日奖励（魔力）", account.get("points", "—") if fresh else "—", "mdi-wallet", "info"),
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
        if account.get("calendar_warning"):
            page.append(n("VAlert", account["calendar_warning"], type="warning", variant="tonal", **{"class": "mb-3"}))
        page.append(n("VAlert", "补签卡：%s 张；插件不自动补签，不使用补签卡。" % account.get("makeup_cards", "—"), type="info", variant="tonal", **{"class": "mb-3"}))
        page.append(self._calendar(account))
        rows = []
        for item in reversed(history[-30:]):
            values = [item.get(k, "—") for k in ("time", "source", "status", "award", "total_days", "message")]
            rows.append(n("tr", content=[n("td", value if value is not None else "—") for value in values]))
        table = n("VTable", content=[
            n("thead", content=[n("tr", content=[n("th", t) for t in ("执行时间", "来源", "状态", "今日魔力", "累计天数", "说明")])]),
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
                today = day == now.day
                signed += int(checked)
                label = "已签到"
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
                status = [n("VIcon", "mdi-check-circle",
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
                                   n("VChip", "今天", color="primary", size="small", variant="outlined")],
                   style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px")
        return n("VCard", content=[n("VCardText", content=[heading,
                 n("div", content=cells, style="display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:clamp(3px,1vw,8px);margin-top:12px"),
                 legend, n("div", "按最近同步数据展示；历史仅标识签到日期，不区分补签。无记录不代表未签到。",
                           **{"class": "text-caption text-medium-emphasis mt-3"})])], variant="outlined")

import calendar
import datetime
import hashlib
import re
from html.parser import HTMLParser
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


class LoginForm(HTMLParser):
    """Read only the password form; retain dynamic hidden and empty decoy fields."""

    def __init__(self, html):
        super().__init__()
        self.active = False
        self.found = False
        self.action = ""
        self.fields = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.active = attrs.get("id") == "loginForm"
            if self.active:
                self.found = True
                self.action = attrs.get("action", "")
        if tag == "input" and self.active and attrs.get("name"):
            if attrs.get("type", "text") not in ("submit", "button", "checkbox"):
                self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False


class FrameHdrSign(_PluginBase):
    plugin_name = "帧影自动签到"
    plugin_desc = "帧影每日签到，支持 Cookie 与账号密码登录、积分统计、签到日历和结果通知。"
    plugin_icon = "https://raw.githubusercontent.com/wenDPwen/MoviePilot-Plugins/main/icons/movie.jpg"
    plugin_version = "1.0.2"
    plugin_author = "wen"
    author_url = "https://github.com/wenDPwen"
    author_icon = "https://raw.githubusercontent.com/wenDPwen/MoviePilot-Plugins/main/icons/author.jpg"
    plugin_config_prefix = "framehdrsign_"
    plugin_order = 32
    auth_level = 2
    BASE_URL = "https://framehdr.com"
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
                logger.error("帧影：定时表达式无效")
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
            "id": "FrameHdrSign", "name": self.plugin_name,
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
        values = [self._config.get(k, "") for k in ("username", "password", "cookie")]
        return hashlib.sha256("\0".join(values).encode()).hexdigest()

    def _session(self):
        session = requests.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Referer": self.BASE_URL + "/index.php",
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
                    session.cookies.set(name, item.value, domain="framehdr.com", path="/", secure=True)
            except Exception:
                session.close()
                raise SignError("Cookie 格式无效，请填写 name=value; name2=value2 格式。") from None
        cache = self.get_data("session") or {}
        if cache.get("fingerprint") == self._fingerprint():
            # The persisted jar supersedes the original cookie, including deletions/rotation.
            session.cookies.clear()
            for item in cache.get("cookies", []):
                if item.get("domain", "").lstrip(".") != "framehdr.com":
                    continue
                session.cookies.set(**item)
        return session

    def _save_session(self, session):
        previous = self._fingerprint()
        request = requests.Request("GET", self.BASE_URL + "/api/checkin.php").prepare()
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
                if c.domain.lstrip(".") == "framehdr.com" and not c.is_expired()
            ],
        })

    def _request(self, session, method, path, **kwargs):
        if self._stop.is_set():
            raise SignError("任务已停止。")
        url = urljoin(self.BASE_URL + "/", path)
        parsed = urlparse(url)
        if (parsed.scheme, parsed.netloc) != ("https", "framehdr.com"):
            raise SignError("站点返回了非本站地址，已停止请求以保护凭证。")
        try:
            return session.request(method, url, timeout=(10, 25), allow_redirects=False, **kwargs)
        except requests.RequestException:
            # Never propagate requests exceptions: they may contain proxy credentials.
            raise SignError("网络请求失败或超时，请检查网络及代理配置。") from None

    @staticmethod
    def _json(response):
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            target = urlparse(urljoin(response.url or FrameHdrSign.BASE_URL + "/api/checkin.php", location))
            # The API's unauthenticated redirect points to /api/login.php.
            if ((target.scheme, target.netloc) == ("https", "framehdr.com")
                    and target.path in ("/login.php", "/api/login.php")):
                raise AuthError("登录态已失效。")
            raise SignError("接口发生异常跳转，未跟随跳转。")
        if response.status_code == 401:
            raise AuthError("登录态已失效。")
        if response.status_code == 429:
            raise SignError("站点请求频率受限，请稍后再试。")
        try:
            result = response.json()
        except ValueError:
            raise SignError("站点未返回 JSON，可能需要人工验证或站点暂时不可用。") from None
        if not isinstance(result, dict):
            raise SignError("站点响应格式异常。")
        if result.get("success") is not True:
            message = str(result.get("message") or "")
            if any(word in message for word in ("未登录", "请先登录", "登录已过期", "登录失效")):
                raise AuthError("登录态已失效。")
        if not 200 <= response.status_code < 300:
            raise SignError("站点返回 HTTP %s，请检查站点状态。" % response.status_code)
        return result

    def _init(self, session):
        now = self._now()
        response = self._request(session, "GET", "/api/checkin.php", params={
            "action": "init", "year": now.year, "month": now.month,
        })
        result = self._json(response)
        data = result.get("data")
        if result.get("success") is not True or not isinstance(data, dict):
            raise SignError("无法获取签到状态，请检查站点或更新 Cookie。")
        if not isinstance(data.get("has_checked_today"), bool):
            raise SignError("签到状态字段异常，已停止签到。")
        return data

    def _initial_state(self, session):
        for attempt in range(self._config["retry"] + 1):
            try:
                return self._init(session)
            except AuthError:
                raise
            except SignError:
                if attempt == self._config["retry"] or self._stop.wait(2):
                    raise

    def _login(self, session, manual=False):
        if not self._config.get("username") or not self._config.get("password"):
            raise AuthError("Cookie 缺失或失效，请更新 Cookie 或配置用户名和密码。")
        fingerprint = self._fingerprint()
        if not manual and self.get_data("login_blocked") == fingerprint:
            raise AuthError("密码登录已暂停；请检查凭证后保存并开启「立即运行一次」重试。")
        response = self._request(session, "GET", "/login.php")
        if response.status_code != 200:
            raise SignError("无法加载登录页，请更新 Cookie 或检查站点。")
        html = response.text
        # An empty geetest placeholder is present even when verification is disabled.
        if re.search(r"initGeetest|initGeetest4|turnstile\.render|g-recaptcha|challenges\.cloudflare\.com", html, re.I):
            raise AuthError("站点启用了人机验证，请在浏览器完成登录并更新 Cookie。")
        form = LoginForm(html)
        if not form.found or not {"username", "password", "login_mode"}.issubset(form.fields):
            raise SignError("登录表单发生变化，请更新插件或改用 Cookie。")
        status = self._json(self._request(session, "GET", "/api/login_status.php", params={"action": "login_status"}))
        security = status.get("data")
        if status.get("success") is not True or not isinstance(security, dict):
            raise SignError("无法确认登录安全状态，已停止密码登录。")
        if security.get("is_locked") or security.get("attempts_left", 1) <= 0:
            raise AuthError("站点已限制登录，请等待解锁后再试。")
        form.fields.update({"username": self._config["username"], "password": self._config["password"],
                            "login_mode": "password", "remember_me": "on"})
        # Suspend before the POST so an uncertain network outcome cannot trigger repeated guesses.
        self.save_data("login_blocked", fingerprint)
        self._request(session, "POST", urljoin(self.BASE_URL + "/login.php", form.action), data=form.fields)
        try:
            data = self._init(session)
        except SignError:
            raise AuthError("密码登录未确认成功，已暂停自动登录；请检查密码、验证码或锁定状态后手动重试。") from None
        self.save_data("login_blocked", "")
        return data

    def _snapshot(self, data):
        # Whitelist display data; never persist CSRF tokens in history or page payloads.
        snapshot = {k: data.get(k) for k in ("points", "continuous_days", "total_days", "has_checked_today", "records")}
        snapshot["updated_at"] = self._now().strftime("%Y-%m-%d %H:%M:%S")
        snapshot["month"] = self._now().strftime("%Y-%m")
        snapshot["fingerprint"] = self._fingerprint()
        self.save_data("account", snapshot)

    def _run(self, session, manual=False):
        try:
            data = self._initial_state(session)
        except AuthError:
            data = self._login(session, manual)
        self._save_session(session)
        self._snapshot(data)
        base = {"balance": data.get("points"), "streak": data.get("continuous_days"), "award": 0}
        if data["has_checked_today"]:
            return {**base, "success": True, "status": "已签到", "message": "今天已经签到，无需重复提交。"}
        token = data.get("csrf_token")
        if not isinstance(token, str) or not token:
            raise SignError("未获取到签到校验令牌，已停止提交；请稍后重试。")
        result = None
        submit_error = ""
        try:
            response = self._request(session, "POST", "/api/checkin.php?action=checkin",
                                     files={"csrf_token": (None, token)})
            result = self._json(response)
        except SignError as exc:
            submit_error = str(exc)
        # A timed-out POST may have committed. Reconcile, never blindly replay it.
        try:
            after = self._initial_state(session)
            self._snapshot(after)
            self._save_session(session)
        except SignError:
            return {**base, "success": False, "status": "待确认",
                    "message": "签到已尝试，但无法复查结果；未重复提交，下次运行将先查询状态。"}
        if after["has_checked_today"]:
            record = (after.get("records") or {}).get(self._now().strftime("%Y-%m-%d"), {})
            award = record.get("points")
            if award is None and result and isinstance(result.get("data"), dict):
                award = result["data"].get("points")
            return {"success": True, "status": "成功", "message": "签到已确认成功。",
                    "award": award, "balance": after.get("points"), "streak": after.get("continuous_days")}
        message = submit_error or "站点未确认签到成功，请检查站点后重试。"
        if result and any(w in str(result.get("message", "")).lower() for w in ("csrf", "令牌", "token")):
            message = "签到校验令牌失效，本次未重复提交，请稍后重新运行。"
        return {**base, "success": False, "status": "失败", "message": message}

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
            logger.info("帧影签到：%s" % result["message"])
            if self._config.get("notify"):
                try:
                    self.post_message(mtype=NotificationType.Plugin,
                                      title="帧影签到 · " + result["status"],
                                      text="\n".join([result["message"],
                                                      "奖励：%s | 积分：%s | 连续：%s 天" % (
                                                          result.get("award", "—"), result.get("balance", "—"), result.get("streak", "—")),
                                                      result["time"]]))
                except Exception:
                    logger.warning("帧影：通知发送失败，签到记录已保留。")
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
                field("username", "用户名 / 邮箱", "Cookie 失效时使用账号密码登录", autocomplete="username"),
                field("password", "登录密码", "保存在 MoviePilot 配置中，不写入日志", type="password", autocomplete="new-password"),
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
        kpis = [("当前积分", account.get("points", "—"), "mdi-wallet", "info"),
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
        page.append(self._calendar(account))
        rows = []
        for item in reversed(history[-30:]):
            values = [item.get(k, "—") for k in ("time", "source", "status", "award", "balance", "message")]
            rows.append(n("tr", content=[n("td", value if value is not None else "—") for value in values]))
        table = n("VTable", content=[
            n("thead", content=[n("tr", content=[n("th", t) for t in ("执行时间", "来源", "状态", "奖励", "积分", "说明")])]),
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

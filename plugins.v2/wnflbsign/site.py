"""Read Discuz HTML/XML as data; never execute returned JavaScript."""

import json
import codecs
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree


def decode_page(content, content_type=''):
    """Prefer declared encodings; tolerate isolated broken bytes like a browser."""
    if not content:
        raise ValueError('Empty response')
    if content.startswith(b'\xef\xbb\xbf'):
        declared = 'utf-8-sig'
    else:
        header = re.search(r'charset\s*=\s*[\x27\"]?([\w-]+)', content_type, re.I)
        meta = re.search(br'<meta\b[^>]*charset\s*=\s*[\x27\"]?([\w-]+)', content[:16384], re.I)
        xml = re.search(br'<\?xml\b[^>]*encoding\s*=\s*[\x27\"]([\w-]+)', content[:256], re.I)
        declared = header[1] if header else meta[1].decode('ascii') if meta else xml[1].decode('ascii') if xml else ''
    encodings = []
    for name in (declared, 'utf-8', 'gb18030'):
        try:
            encoding = codecs.lookup(name).name
        except LookupError:
            continue
        if encoding not in ('utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'gb18030', 'big5', 'cp950'):
            continue
        if encoding not in encodings:
            encodings.append(encoding)
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    text = content.decode(encodings[0], errors='replace')
    if text.count('\ufffd') > max(4, len(text) // 100):
        raise ValueError('Response encoding is not recognizable')
    return text


class ForumPage(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.scripts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.elements.append(element)
        if tag not in {"input", "img", "br", "hr", "link", "meta", "base", "area", "wbr", "source", "embed", "param", "col"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                if tag == "script":
                    self.scripts.append(self.stack[i]["text"])
                del self.stack[i:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element["text"] += data

    def by_id(self, name):
        return next((e for e in self.elements if e["attrs"].get("id") == name), None)


def parse_status(html):
    page = ForumPage(html)
    script = "\n".join(page.scripts)
    uid = re.search(r"\bdiscuz_uid\s*=\s*['\"](\d+)['\"]", script)
    if uid and uid[1] == "0":
        return None
    logout = next((e for e in page.elements if e["tag"] == "a"
                   and "action=logout" in e["attrs"].get("href", "")), None)
    if not uid or not logout:
        raise ValueError("Login status unavailable")
    status = re.search(r"\b(?:var\s+)?fx_chk_menu\s*=\s*(true|false)\b", script)
    button = page.by_id("fx_checkin_b")
    if not status or not button:
        raise ValueError("Checkin status unavailable")
    checked = status[1] == "true"
    if button["attrs"].get("alt") == "已签到" and not checked:
        raise ValueError("Contradictory status")
    summary = page.by_id("fx_checkin_menub")
    summary_text = summary["text"] if summary else ""
    streak = re.search(r"连续签到\s*[:：]\s*(\d+)", summary_text)
    days = re.search(r"累计签到\s*[:：]\s*(\d+)", summary_text)
    target = re.search(r"showWindow\(\s*['\"]fx_checkin['\"]\s*,\s*['\"]([^'\"]+)['\"]", script)
    sign_path = target[1] if target else ""
    query = parse_qs(urlparse(sign_path).query)
    token = query.get("formhash", [""])[0]
    if not checked and (query.get("id") != ["fx_checkin:checkin"] or not re.fullmatch(r"[0-9a-fA-F]{8}", token)):
        raise ValueError("Checkin formhash unavailable")
    credits = next((e["text"] for e in page.elements if e["tag"] == "ul"
                    and "creditl" in e["attrs"].get("class", "").split()), "")
    balances = {}
    for label, key in (("福利", "points"), ("金币", "coins"), ("积分", "score")):
        match = re.search(label + r"\s*[:：]\s*([\d,.]+)", credits)
        balances[key] = float(match[1].replace(",", "")) if match else None
    return {**balances, "has_checked_today": checked,
            "continuous_days": int(streak[1]) if streak else None,
            "total_days": int(days[1]) if days else None,
            "sign_path": sign_path, "uid": uid[1]}


def parse_calendar(text, month):
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Unexpected XML declarations")
    payload = ElementTree.fromstring(text).text if text.lstrip().startswith("<") else text
    data = json.loads(payload or "")
    if not isinstance(data, dict) or data.get("d") != month:
        raise ValueError("Calendar month mismatch")
    records = {}
    for key, value in data.items():
        if key.isdigit() and 1 <= int(key) <= 31 and isinstance(value, dict):
            day = "%s-%s-%02d" % (month[:4], month[4:], int(key))
            records[day] = {"points": value.get("m"), "is_makeup": bool(value.get("bq")), "rank": value.get("l")}
    return records


def parse_login_form(html):
    class LoginParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.action = ""
            self.fields = {}
            self.captcha = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.active = attrs.get("id", "").startswith("loginform_")
                if self.active:
                    self.action = attrs.get("action", "")
            if self.active and tag == "input" and attrs.get("name"):
                name = attrs["name"]
                if name.startswith(("seccodeverify", "secanswer")):
                    self.captcha = True
                if attrs.get("type") not in ("submit", "checkbox", "button"):
                    self.fields[name] = attrs.get("value", "")

        def handle_endtag(self, tag):
            if tag == "form":
                self.active = False

    parser = LoginParser()
    parser.feed(html)
    if not parser.action or not {"username", "password", "formhash"}.issubset(parser.fields):
        raise ValueError("Password login form missing")
    return parser


def login_failure_reason(html):
    """Return fixed diagnostic text only; never echo account data from a response."""
    text = unescape(html)
    text = text.replace('<![CDATA[', '').replace(']]>', '')
    text = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m[1], 16)), text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    for pattern, message in (
        (r'login_strike|password_retry_limit|尝试.*(?:次数|多次).*限制|密码错误次数过多|登录失败次数过多|请.*(?:分钟|小时).*后.*(?:登录|重试)|累计.*错误.*次|多次输入密码错误',
         '论坛限制了登录尝试次数，请等待解锁后再试。'),
        (r'验证码.*(?:错误|不正确|失效)|seccode_invalid|secqaa_invalid|安全验证|请.*完成.*验证',
         '论坛要求验证码或安全验证，请在浏览器登录后更新 Cookie。'),
        (r'login_question_empty|安全提问.*(?:错误|不正确)|安全问题.*(?:错误|不正确)|答案.*(?:错误|不正确)',
         '论坛提示安全问题或答案不正确，请核对账号安全设置。'),
        (r'login_invalid|登录失败|用户名.*(?:错误|不正确|不存在)|密码.*(?:错误|不正确)|账户.*(?:错误|不存在)',
         '论坛拒绝了账号凭证，请核对论坛用户名、密码以及安全问题和答案。'),
        (r'submit_invalid|formhash.*(?:invalid|错误)|您的请求来路不正确|请求来路不正确|表单.*(?:失效|过期)',
         '论坛拒绝了登录表单或请求来源，请重新运行以获取新的登录表单。'),
        (r'用户.*(?:禁止|冻结)|账号.*(?:禁止|冻结)|login_succeed_inactive_member|login_succeed_password_change',
         '论坛要求账号激活、解除限制或修改密码，请先在浏览器处理。'),
    ):
        if re.search(pattern, text, re.I):
            return message
    return ''


def login_diagnostics(response, html, cookies):
    """Only emit enumerated metadata, never raw text, headers, URLs or cookie values."""
    body = html.lstrip()
    kind = 'XML' if body.startswith('<?xml') or body.startswith('<root') else ('HTML' if '<html' in body.lower() else '其他')
    has_auth = any(c.name.endswith('_auth') and c.value and not c.is_expired()
                   and c.domain.lstrip('.') in ('wnflb2023.com', 'www.wnflb2023.com') for c in cookies)
    response_auth = any(c.name.endswith('_auth') and c.value and not c.is_expired()
                        for c in response.cookies)
    form = bool(re.search(r'id=[\'"]loginform_', html))
    callback = ('成功标记' if re.search(r'login_succeed|succeedhandle_|欢迎您回来', html)
                else '错误标记' if re.search(r'errorhandle_|showDialog\(|alert_error|alert_info', html)
                else '无标记')
    target = urlparse(response.headers.get('Location', ''))
    redirect = ('无' if not response.is_redirect else
                '本站' if not target.netloc or (target.scheme == 'https' and target.netloc == 'www.wnflb2023.com') else '站外')
    return 'HTTP %s / %s / 登录表单:%s / %s / 新认证Cookie:%s / 会话认证Cookie:%s / 跳转:%s' % (
        response.status_code, kind, '有' if form else '无', callback,
        '有' if response_auth else '无', '有' if has_auth else '无', redirect)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GLaDOS 每日自动签到 (glados.cloud)
=====================================================================
零第三方依赖：仅使用 Python 标准库，无需 pip install，双击 / 计划任务 / CI 均可运行。

接口契约（2026 年新版，基于抓包确认）：
    签到   POST https://glados.cloud/api/user/checkin   body {"token": "glados.cloud"}
    状态   GET  https://glados.cloud/api/user/status    -> data.email / data.leftDays
    积分   GET  https://glados.cloud/api/user/points    -> points 或 data.points

    ⚠️ token 必须是 "glados.cloud"。旧脚本填 "glados.one" 会被服务端拒绝，
       固定返回 "please checkin via https://glados.cloud"。

    返回码（2026-09 实测 glados.cloud）：成功时返回 code=1，
    message="Today's observation logged. Return tomorrow for more points."，
    并未使用旧资料描述的 "code=0=成功 / code=1=已签到" 语义。
    code∈{0,1} 均视为成功；仅当 message 含 "please checkin via" 等才为失败。
    响应顶层 points=0 并非总额，真实每日变化在 list[0].change。

用法：
    python glados_checkin.py                  # 执行签到（读取同目录 config.json）
    python glados_checkin.py --dry-run        # 只查状态，不发签到请求
    python glados_checkin.py --selftest       # 离线自检（不联网，验证逻辑）
    python glados_checkin.py --config a.json  # 指定配置文件
    python glados_checkin.py --notify-test    # 只测试推送通道是否可用

退出码：0 = 全部成功或已签到；1 = 存在失败账号
=====================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_URL = "https://glados.cloud"
CHECKIN_URL = f"{BASE_URL}/api/user/checkin"
STATUS_URL = f"{BASE_URL}/api/user/status"
POINTS_URL = f"{BASE_URL}/api/user/points"

# 服务端要求的签到令牌（写死，不要改）
CHECKIN_TOKEN = "glados.cloud"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

RETRY_TIMES = 3           # 总尝试次数
RETRY_BACKOFF = 1.5       # 退避基数（秒），实际等待 = base * 2^n + 抖动
TIMEOUT = 30              # 秒

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"

# 尽可能用 certifi 的根证书，避免部分 Windows 环境 SSL 校验失败；缺失则回退系统证书
try:  # pragma: no cover - 属于可选优化
    import certifi  # type: ignore

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
class Logger:
    """极简日志器：同时输出到控制台和 logs/YYYY-MM.log。"""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self._fh = None
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._fh = (LOG_DIR / f"{datetime.now():%Y-%m}.log").open("a", encoding="utf-8")
        except OSError:
            self._fh = None

    def _write(self, level: str, msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
        if self._fh:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass
        if not (self.quiet and level == "INFO"):
            try:
                print(line, flush=True)
            except UnicodeEncodeError:  # 老终端编码兜底
                print(line.encode("utf-8", "replace").decode("utf-8", "replace"))

    def info(self, msg: str) -> None:
        self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# 脱敏工具
# ---------------------------------------------------------------------------
def mask_email(value: str) -> str:
    """a****@qq.com -> a***@qq.com，避免日志泄露账号。"""
    value = (value or "").strip()
    if "@" not in value:
        return "***"
    name, _, domain = value.partition("@")
    head = name[:1] if name else ""
    return f"{head}***@{domain}"


def mask_cookie(value: str) -> str:
    """只保留 Cookie 首尾各少量字符。"""
    value = (value or "").strip()
    if len(value) <= 24:
        return "***"
    return f"{value[:12]}...{value[-6:]}"


# ---------------------------------------------------------------------------
# Cookie 处理
# ---------------------------------------------------------------------------
def split_accounts(raw: str) -> list[str]:
    """按 & 或换行拆分多账号。"""
    if not raw:
        return []
    parts = re.split(r"[&\n\r]+", raw)
    return [p.strip() for p in parts if p.strip()]


def validate_cookie(cookie: str) -> tuple[bool, str]:
    """
    预校验 Cookie 格式，提前发现常见粘贴错误：
      - 缺少 koa:sess
      - 分号后缺空格
      - 首尾多余引号
    """
    c = cookie.strip()
    if c.startswith(("'", '"')) or c.endswith(("'", '"')):
        return False, "Cookie 首尾带了引号，请去掉"
    if "koa:sess=" not in c:
        return False, "缺少 koa:sess，请从浏览器 Application -> Cookies 复制完整值"
    if "koa:sess.sig=" in c and ";koa:sess.sig=" in c:
        return False, "分号后缺少空格，应为 '; koa:sess.sig='"
    if any(ch in c for ch in "\r\n"):
        return False, "Cookie 内含换行符"
    return True, "OK"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class CheckinError(Exception):
    """签到流程中的可读错误。"""


def _build_request(url: str, cookie: str, payload: dict | None = None) -> urllib.request.Request:
    method = "POST" if payload is not None else "GET"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    req.add_header("Origin", BASE_URL)
    req.add_header("Referer", f"{BASE_URL}/console/checkin")
    req.add_header("Cookie", cookie)
    if data is not None:
        req.add_header("Content-Type", "application/json;charset=UTF-8")
    return req


def http_json(url: str, cookie: str, payload: dict | None = None, logger: Logger | None = None):
    """
    发起请求并解析 JSON，带指数退避重试。

    仅在“可重试”的错误上重试：网络异常、超时、5xx、非 JSON 响应（网关错误页）。
    认证类错误（401/403）不重试，直接抛出，避免把无效 Cookie 打满。
    """
    last_err: Exception | None = None

    for attempt in range(1, RETRY_TIMES + 1):
        try:
            req = _build_request(url, cookie, payload)
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise CheckinError(
                        f"服务端返回了非 JSON 内容（HTTP {resp.status}）：{(body or '')[:120]}"
                    ) from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:160]
            except Exception:  # noqa: BLE001
                pass

            if exc.code in (401, 403):
                raise CheckinError(
                    f"认证失败（HTTP {exc.code}），Cookie 可能已过期，请重新登录 glados.cloud 复制。{detail}"
                ) from exc
            if 500 <= exc.code < 600:
                last_err = CheckinError(f"服务端错误 HTTP {exc.code} {detail}")
            else:
                raise CheckinError(f"请求被拒绝（HTTP {exc.code}）{detail}") from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
            last_err = CheckinError(f"网络异常：{exc}")
        except CheckinError as exc:
            last_err = exc

        if attempt < RETRY_TIMES:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            if logger:
                logger.warn(f"第 {attempt} 次请求失败（{last_err}），{wait:.1f}s 后重试")
            time.sleep(wait)

    raise last_err if last_err else CheckinError("未知错误")


# ---------------------------------------------------------------------------
# 业务逻辑（纯函数，便于自检）
# ---------------------------------------------------------------------------
REPEAT_KEYWORDS = (
    "repeat", "already", "已签到", "重复", "明天再", "请勿重复",
)


def classify_checkin(code, message: str) -> str:
    """
    判定签到结果：ok / repeat / fail

    2026-09 实测 glados.cloud：
      - 成功时返回 code=1，message="Today's observation logged. Return tomorrow for more points."
      - 与旧资料 "code=1=已签到" 的描述不符，故 code∈{0,1} 均视为成功。
      - 仅当 message 含 "please checkin via" 等明确失败信号时才为 fail。
      - 若 message 含 "请勿重复"/"already" 等幂等提示，归为 repeat（非失败）。
    """
    msg = (message or "").lower()

    # 明确的失败信号（token 写错 / 域名不对）："please checkin via https://glados.cloud"
    if "please checkin via" in msg:
        return "fail"
    # 明确的"已签到"幂等提示
    if any(kw in msg for kw in REPEAT_KEYWORDS):
        return "repeat"
    # 成功文案识别
    if "observation logged" in msg or re.search(r"got\s+\d+\s+points?", msg) or "获得" in msg:
        return "ok"

    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = -2
    if code_int in (0, 1):
        return "ok"
    return "fail"


def parse_earned_points(message: str) -> int:
    """
    从 message 文本解析本次获得积分（接口不单独返回 points 字段）。
    英文："Checkin success, got 12 points"
    中文："已经签到成功，获得 8 点"
    """
    if not message:
        return 0
    m = re.search(r"got\s+(\d+)\s+points?", message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"获得\s*(\d+)\s*点", message)
    if m:
        return int(m.group(1))
    return 0


def extract_today_points(payload: dict) -> float:
    """
    从签到响应 list 中取"今天"这条记录的 change 作为本次获得积分。

    关键修正（2026-09 实测）：响应顶层 points 字段为 0，并非总额；
    真实每日变化写在 list[].change，且 list[0] 为最近一天（含今天）。
    """
    lst = payload.get("list") or []
    today = datetime.now().strftime("%Y-%m-%d")
    for item in lst:
        if str(item.get("detail", "")) == today:
            try:
                return round(float(item.get("change", 0)), 2)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def to_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 单账号流程
# ---------------------------------------------------------------------------
def checkin_one(cookie: str, index: int, total: int, logger: Logger, dry_run: bool = False) -> dict:
    """处理单个账号：签到 -> 查状态 -> 查积分。返回结果字典。"""
    result = {
        "index": index,
        "email": "",
        "status": "fail",     # ok / repeat / fail
        "earned": 0,
        "total_points": None,
        "left_days": None,
        "message": "",
        "error": "",
    }

    logger.info(f"[{index}/{total}] Cookie={mask_cookie(cookie)}")

    # 1) 签到
    if dry_run:
        logger.info("  (dry-run) 跳过签到请求")
        result["status"] = "ok"
        result["message"] = "dry-run"
    else:
        try:
            payload = http_json(CHECKIN_URL, cookie, {"token": CHECKIN_TOKEN}, logger)
            code = payload.get("code", -2)
            message = str(payload.get("message", ""))
            result["status"] = classify_checkin(code, message)
            result["message"] = message
            result["earned"] = extract_today_points(payload) or parse_earned_points(message)

            if result["status"] == "ok":
                logger.info(f"  ✅ 签到成功，本次 +{result['earned']:g} 积分")
            elif result["status"] == "repeat":
                logger.info(f"  🔄 今日已签到（{message or 'Repeats'}），跳过")
            else:
                logger.error(f"  ❌ 签到失败：code={code} message={message}")
        except CheckinError as exc:
            result["error"] = str(exc)
            logger.error(f"  ❌ 签到异常：{exc}")

    # 2) 账号状态
    try:
        status = http_json(STATUS_URL, cookie, None, logger)
        data = status.get("data") or {}
        if data.get("email"):
            result["email"] = mask_email(str(data["email"]))
        if data.get("leftDays") is not None:
            result["left_days"] = to_int(data["leftDays"])
            logger.info(f"  📅 剩余天数：{result['left_days']} 天")
    except CheckinError as exc:
        logger.warn(f"  ⚠️ 状态查询失败：{exc}")

    # 3) 积分余额
    try:
        points = http_json(POINTS_URL, cookie, None, logger)
        pts = points.get("points")
        if pts is None:
            pts = (points.get("data") or {}).get("points")
        if pts is not None:
            result["total_points"] = to_int(pts)
            logger.info(f"  💰 当前积分：{result['total_points']}")
    except CheckinError as exc:
        logger.warn(f"  ⚠️ 积分查询失败：{exc}")

    return result


# ---------------------------------------------------------------------------
# 通知（可选，失败不影响签到结果）
# ---------------------------------------------------------------------------
def build_report(results: list[dict], dry_run: bool) -> tuple[str, str]:
    now = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    title = ("[测试] " if dry_run else "") + f"GLaDOS 签到 {datetime.now():%m-%d}"

    ok = sum(1 for r in results if r["status"] == "ok")
    repeat = sum(1 for r in results if r["status"] == "repeat")
    fail = sum(1 for r in results if r["status"] == "fail")
    earned = sum(r["earned"] for r in results)

    lines = [
        f"## GLaDOS 自动签到",
        f"- 时间：{now}",
        f"- 结果：成功 {ok} / 已签到 {repeat} / 失败 {fail}",
        f"- 本次共获得：**+{earned:g}** 积分",
        "",
    ]
    for r in results:
        icon = {"ok": "✅", "repeat": "🔄", "fail": "❌"}[r["status"]]
        line = f"- {icon} 账号{r['index']} `{r['email'] or '未知'}`"
        if r["total_points"] is not None:
            line += f" 余额 {r['total_points']}"
        if r["left_days"] is not None:
            line += f" 剩余 {r['left_days']}天"
        if r["status"] == "fail" and r["error"]:
            line += f" （{r['error'][:80]}）"
        lines.append(line)

    return title, "\n".join(lines)


def _post_json(url: str, payload: dict, timeout: int = 15) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        resp.read()


def send_notifications(cfg: dict, title: str, content: str, logger: Logger) -> None:
    """向已配置的通道推送结果；任一通道失败只记日志，不抛异常。"""
    notify = cfg.get("notify") or {}

    # PushPlus（微信）
    token = (notify.get("pushplus") or "").strip()
    if token:
        try:
            _post_json(
                "https://www.pushplus.plus/send",
                {"token": token, "title": title, "content": content, "template": "markdown"},
            )
            logger.info("  📱 PushPlus 推送成功")
        except Exception as exc:  # noqa: BLE001
            logger.warn(f"  ⚠️ PushPlus 推送失败：{exc}")

    # Server 酱
    key = (notify.get("serverchan") or "").strip()
    if key:
        try:
            body = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
            req = urllib.request.Request(f"https://sctapi.ftqq.com/{key}.send", data=body, method="POST")
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                resp.read()
            logger.info("  📱 Server酱 推送成功")
        except Exception as exc:  # noqa: BLE001
            logger.warn(f"  ⚠️ Server酱 推送失败：{exc}")

    # 企业微信群机器人
    hook = (notify.get("wecom_webhook") or "").strip()
    if hook:
        try:
            _post_json(hook, {"msgtype": "markdown", "markdown": {"content": content}})
            logger.info("  📱 企业微信 推送成功")
        except Exception as exc:  # noqa: BLE001
            logger.warn(f"  ⚠️ 企业微信 推送失败：{exc}")

    # Telegram
    tg = notify.get("telegram") or {}
    tg_token = (tg.get("bot_token") or "").strip()
    tg_chat = str(tg.get("chat_id") or "").strip()
    if tg_token and tg_chat:
        try:
            _post_json(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                {"chat_id": tg_chat, "text": f"{title}\n\n{content}", "parse_mode": "Markdown"},
            )
            logger.info("  📱 Telegram 推送成功")
        except Exception as exc:  # noqa: BLE001
            logger.warn(f"  ⚠️ Telegram 推送失败：{exc}")


# ---------------------------------------------------------------------------
# 配置装载
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_NAME = "config.json"


def load_config(path: Path | None, logger: Logger) -> dict:
    cfg: dict = {}

    # 1) --config 指定文件，或同目录默认 config.json
    candidate = path if path else (SCRIPT_DIR / DEFAULT_CONFIG_NAME)
    if candidate and candidate.exists():
        try:
            cfg = json.loads(candidate.read_text(encoding="utf-8"))
            logger.info(f"已加载配置文件：{candidate}")
        except (OSError, ValueError) as exc:
            logger.warn(f"配置文件解析失败（{candidate}）：{exc}，将回退到环境变量")
            cfg = {}
    elif path:
        logger.warn(f"指定的配置文件不存在：{candidate}")

    # 2) GLADOS_CONFIG 指向的配置文件
    env_cfg = os.environ.get("GLADOS_CONFIG", "").strip()
    if env_cfg and Path(env_cfg).exists():
        try:
            merged = json.loads(Path(env_cfg).read_text(encoding="utf-8"))
            cfg = {**cfg, **merged}
            logger.info(f"已合并环境变量指定的配置：{env_cfg}")
        except (OSError, ValueError) as exc:
            logger.warn(f"GLADOS_CONFIG 解析失败：{exc}")

    # 3) 环境变量直供 Cookie —— 优先级最高，CI 场景只需这一项
    env_cookie = os.environ.get("GLADOS_COOKIE") or os.environ.get("GLADOS_COOKIES") or ""
    if env_cookie.strip():
        cfg["cookies"] = env_cookie.strip()
        logger.info("已从环境变量 GLADOS_COOKIE 读取 Cookie")

    # 4) 推送凭证也支持环境变量（GitHub Actions Secret 场景）
    notify = cfg.get("notify") or {}
    tg = notify.get("telegram") or {}
    env_notify = {
        "pushplus": os.environ.get("PUSHPLUS", "").strip() or notify.get("pushplus", ""),
        "serverchan": os.environ.get("SERVERCHAN_KEY", "").strip() or notify.get("serverchan", ""),
        "wecom_webhook": os.environ.get("WECOM_WEBHOOK", "").strip() or notify.get("wecom_webhook", ""),
        "telegram": {
            "bot_token": os.environ.get("TG_BOT_TOKEN", "").strip() or tg.get("bot_token", ""),
            "chat_id": os.environ.get("TG_CHAT_ID", "").strip() or tg.get("chat_id", ""),
        },
    }
    if any(str(v).strip() for k, v in env_notify.items() if k != "telegram"):
        logger.info("已从环境变量读取部分推送凭证")
    cfg["notify"] = env_notify

    if not cfg.get("cookies") and not cfg.get("cookie"):
        raise CheckinError(
            "未找到任何 Cookie。请在 config.json 中填写 cookies，"
            "或设置环境变量 GLADOS_COOKIE。参考 config.example.json。"
        )

    raw = cfg.get("cookies") or cfg.get("cookie")
    cookies = raw if isinstance(raw, list) else split_accounts(str(raw))
    cookies = [c.strip() for c in cookies if str(c).strip()]
    if not cookies:
        raise CheckinError("Cookie 列表为空，请检查 config.json 的 cookies 字段。")

    cfg["_cookies"] = cookies
    return cfg


def log_config_hint() -> None:
    print(
        "\n没有找到 config.json。请先复制模板：\n"
        f'  copy "{SCRIPT_DIR / "config.example.json"}" "{SCRIPT_DIR / DEFAULT_CONFIG_NAME}"\n'
        "然后填入你的 Cookie（见 config.example.json 内的说明）。\n"
    )


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------
def selftest() -> int:
    """不联网，仅验证解析/分类/脱敏/拆分的正确性。"""
    checks: list[tuple[str, bool]] = []

    def eq(name: str, got, want) -> None:
        checks.append((name, got == want))

    eq("classify code=0", classify_checkin(0, ""), "ok")
    eq("classify code=1 实测成功", classify_checkin(1, "Today's observation logged. Return tomorrow for more points."), "ok")
    eq("classify 英文成功文案", classify_checkin(None, "Checkin success, got 12 points"), "ok")
    eq("classify 中文成功文案", classify_checkin(None, "已经签到成功，获得 8 点"), "ok")
    eq("classify 中文已签到", classify_checkin(None, "签到失败，请勿重复签到"), "repeat")
    eq("classify 未知失败", classify_checkin(-2, "please checkin via https://glados.cloud"), "fail")

    eq("parse 英文", parse_earned_points("Checkin success, got 12 points"), 12)
    eq("parse 英文单数 point", parse_earned_points("Got 8 Point"), 8)
    eq("parse 中文", parse_earned_points("已经签到成功，获得 8 点，请明天继续签到哦！"), 8)
    eq("parse 无积分", parse_earned_points("Repeats"), 0)

    # 从 list 解析今日积分（实测：顶层 points=0，真实变化在 list[0].change）
    sample = {
        "list": [
            {"detail": datetime.now().strftime("%Y-%m-%d"), "change": "9.00000000", "balance": "20"},
            {"detail": "2026-09-28", "change": "3", "balance": "11"},
        ]
    }
    eq("extract 今日积分", extract_today_points(sample), 9.0)
    eq("extract 无今日记录", extract_today_points({"list": [{"detail": "2026-01-01", "change": "5"}]}), 0.0)

    eq("email 脱敏", mask_email("471826412@qq.com"), "4***@qq.com")
    eq("email 异常值", mask_email("nope"), "***")
    eq("cookie 脱敏长度", masked := mask_cookie("koa:sess=" + "x" * 60), "koa:sess=xxx...xxxxxx")  # noqa: F841
    eq("cookie 过短", mask_cookie("short"), "***")

    eq("多账号 & 拆分", split_accounts("a=1&b=2"), ["a=1", "b=2"])
    eq("多账号换行拆分", split_accounts("a=1\nb=2"), ["a=1", "b=2"])
    eq("空串拆分", split_accounts(""), [])

    eq("cookie 缺 koa:sess", validate_cookie("foo=bar")[0], False)
    eq("cookie 带引号", validate_cookie('"koa:sess=abc; koa:sess.sig=def"')[0], False)
    eq("cookie 缺空格", validate_cookie("koa:sess=abc;koa:sess.sig=def")[0], False)
    eq("cookie 合法", validate_cookie("koa:sess=abc; koa:sess.sig=def")[0], True)
    eq("cookie 仅 koa:sess", validate_cookie("koa:sess=abc")[0], True)

    eq("to_int 字符串浮点", to_int("320.0"), 320)
    eq("to_int 非法值", to_int("abc", -1), -1)

    eq("CHECKIN_TOKEN 契约", CHECKIN_TOKEN, "glados.cloud")
    eq("签到 URL 契约", CHECKIN_URL, "https://glados.cloud/api/user/checkin")

    failed = [name for name, ok in checks if not ok]
    print(f"\n离线自检：{len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  ✗ {name}")
        return 1
    print("✅ 全部通过（未联网）")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="GLaDOS 每日自动签到（glados.cloud）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", type=Path, default=None, help="配置文件路径，默认 ./config.json")
    p.add_argument("--dry-run", action="store_true", help="只查询账号状态，不发送签到请求")
    p.add_argument("--selftest", action="store_true", help="离线自检，不联网")
    p.add_argument("--notify-test", action="store_true", help="使用占位结果测试推送通道")
    p.add_argument("--no-notify", action="store_true", help="本次不推送")
    p.add_argument("--quiet", action="store_true", help="仅在日志文件记录 INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.selftest:
        return selftest()

    logger = Logger(quiet=args.quiet)
    started = time.time()

    try:
        cfg = load_config(args.config, logger)
    except CheckinError as exc:
        if not args.config and not (SCRIPT_DIR / DEFAULT_CONFIG_NAME).exists():
            logger.error(str(exc))
            log_config_hint()
        else:
            logger.error(str(exc))
        logger.close()
        return 1

    cookies = cfg["_cookies"]
    logger.info("=" * 56)
    logger.info(f"GLaDOS 自动签到启动 | 账号数 {len(cookies)} | dry-run={args.dry_run}")
    logger.info("=" * 56)

    results: list[dict] = []
    for i, cookie in enumerate(cookies, 1):
        valid, why = validate_cookie(cookie)
        if not valid:
            logger.error(f"[{i}/{len(cookies)}] Cookie 格式校验未通过：{why}")
            results.append({
                "index": i, "email": "", "status": "fail", "earned": 0,
                "total_points": None, "left_days": None, "message": "", "error": why,
            })
            continue
        results.append(checkin_one(cookie, i, len(cookies), logger, dry_run=args.dry_run))
        if i < len(cookies):
            time.sleep(random.uniform(1.0, 2.5))  # 账号间停顿，避免触发风控

    ok = sum(1 for r in results if r["status"] == "ok")
    repeat = sum(1 for r in results if r["status"] == "repeat")
    fail = sum(1 for r in results if r["status"] == "fail")
    earned = sum(r["earned"] for r in results)

    logger.info("-" * 56)
    logger.info(f"汇总：成功 {ok} | 已签到 {repeat} | 失败 {fail} | 本次 +{earned:g} 积分")
    logger.info(f"耗时 {time.time() - started:.1f}s")

    title, content = build_report(results, args.dry_run)
    if not args.no_notify:
        send_notifications(cfg, title, content, logger)

    logger.close()
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

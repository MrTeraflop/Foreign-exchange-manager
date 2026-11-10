#!/usr/bin/env python3
"""中国银行 英镑 现汇卖出价 监控脚本（由 GitHub Actions 定时运行）。

规则：
- 只在北京时间 ACTIVE_START_HOUR ~ ACTIVE_END_HOUR 之间检查（手动运行不受限制）。
- 价格低于阈值时提醒一次；之后每再跌破一条“阈值 - n × 步长”的线再提醒一次。
- 一次跌破多条线只发一条；价格回到阈值及以上时重置。
- 连续抓取失败达到 FAIL_ALERT_COUNT 次时提醒一次，恢复后再发一条恢复通知。

只使用 Python 标准库，无需安装任何依赖。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from html.parser import HTMLParser

BEIJING = timezone(timedelta(hours=8))
CURRENCY = "英镑"
PRICE_COLUMN = "现汇卖出价"
BOC_PAGE = "https://www.boc.cn/sourcedb/whpj/"
BOC_SEARCH = "https://srh.bankofchina.com/search/whpj/search_cn.jsp"
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------- 配置

def env_decimal(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return Decimal(default)
    try:
        value = Decimal(raw)
    except InvalidOperation:
        print(f"[警告] 变量 {name}={raw!r} 不是数字，改用默认值 {default}")
        return Decimal(default)
    return value


def env_int(name, default):
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[警告] 变量 {name}={raw!r} 不是整数，改用默认值 {default}")
        return default


THRESHOLD = env_decimal("ALERT_THRESHOLD", "890")
STEP = env_decimal("ALERT_STEP", "2")
if STEP <= 0:
    print("[警告] ALERT_STEP 必须大于 0，改用 2")
    STEP = Decimal("2")
FAIL_ALERT_COUNT = max(1, env_int("FAIL_ALERT_COUNT", 3))
ACTIVE_START_HOUR = env_int("ACTIVE_START_HOUR", 7)
ACTIVE_END_HOUR = env_int("ACTIVE_END_HOUR", 23)
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (os.environ.get("NTFY_TOPIC") or "").strip()
MODE = (os.environ.get("MODE") or "scheduled").strip()  # scheduled / check / test
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # 本地测试用：只打印不推送
FIXTURE = os.environ.get("BOC_FIXTURE")  # 本地测试用：从文件读取页面


# ---------------------------------------------------------------- 抓取与解析

class TableParser(HTMLParser):
    """把页面里所有表格行提取成 [[单元格文字, ...], ...]。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_rate(html):
    """从牌价表格中取出英镑的现汇卖出价和发布时间。"""
    parser = TableParser()
    parser.feed(html)
    header = None
    for row in parser.rows:
        if header is None:
            if "货币名称" in row and PRICE_COLUMN in row:
                header = row
            continue
        if row and row[0] == CURRENCY and len(row) >= len(header):
            cells = dict(zip(header, row))
            price_text = cells.get(PRICE_COLUMN, "").replace(",", "")
            try:
                price = Decimal(price_text)
            except InvalidOperation:
                raise ValueError(f"现汇卖出价不是数字：{price_text!r}")
            if not (Decimal(100) <= price <= Decimal(5000)):
                raise ValueError(f"现汇卖出价超出合理范围：{price}")
            date_part = cells.get("发布日期", "")
            time_part = cells.get("发布时间", "")
            published = date_part
            if time_part and time_part not in date_part:
                published = f"{date_part} {time_part}".strip()
            published = published.replace(".", "-").replace("/", "-")
            return price, published
    if header is None:
        raise ValueError("页面中没有找到牌价表格（可能是网页改版或被拦截）")
    raise ValueError(f"牌价表格中没有找到“{CURRENCY}”这一行")


def http_get(url, data=None):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def fetch_rate():
    """先抓中行牌价主页，失败再试中行牌价查询页；每个地址最多试 2 次。"""
    if FIXTURE:
        with open(FIXTURE, encoding="utf-8") as f:
            content = f.read()
        if content.startswith("FAIL"):
            raise RuntimeError("（测试）模拟抓取失败")
        return parse_rate(content)

    errors = []
    sources = [
        ("中行牌价主页", lambda: http_get(BOC_PAGE)),
        ("中行牌价查询", lambda: http_get(BOC_SEARCH, {"erectDate": "", "nothing": "", "pjname": CURRENCY})),
    ]
    for name, getter in sources:
        for attempt in range(2):
            try:
                return parse_rate(getter())
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name} 第{attempt + 1}次：{exc}")
                time.sleep(3)
    raise RuntimeError("；".join(errors))


# ---------------------------------------------------------------- 推送

def notify(title, message, priority=3, tags=None):
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "click": BOC_PAGE,
    }
    if tags:
        payload["tags"] = tags
    if DRY_RUN:
        print(f"[推送] (优先级 {priority}) {title}\n{message}\n")
        return
    if not NTFY_TOPIC:
        raise RuntimeError("没有设置 NTFY_TOPIC（仓库 Secrets 里的 ntfy 频道名）")
    req = urllib.request.Request(
        NTFY_SERVER + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    print(f"[已推送] {title}")


# ---------------------------------------------------------------- 状态

DEFAULT_STATE = {
    "threshold": None,
    "step": None,
    "alerted_index": None,  # 已提醒到第几条线：0 表示阈值本身，1 表示阈值-步长……
    "last_price": None,
    "last_published": None,
    "consecutive_failures": 0,
    "failure_alerted": False,
}


def load_state():
    state = dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print(f"[警告] 读取状态文件失败，按全新状态处理：{exc}")
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------- 主逻辑

def fmt(value):
    """890.00 → 890，888.50 → 888.5"""
    return f"{Decimal(value):.2f}".rstrip("0").rstrip(".")


def level(index):
    return THRESHOLD - STEP * index


def deepest_index(price):
    """价格跌破了第几条线（只在 price < THRESHOLD 时调用）。"""
    crossed = ((THRESHOLD - price) / STEP).to_integral_value(rounding=ROUND_CEILING)
    return int(crossed) - 1


def now_beijing():
    override = os.environ.get("NOW_BEIJING")  # 本地测试用
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=BEIJING)
    return datetime.now(BEIJING)


def handle_price(state, price, published):
    # 阈值或步长改过：重新计算
    if state["threshold"] != str(THRESHOLD) or state["step"] != str(STEP):
        if state["threshold"] is not None:
            print(f"[信息] 设置已更改（阈值 {state['threshold']}→{THRESHOLD}，步长 {state['step']}→{STEP}），重新计算")
        state["threshold"], state["step"] = str(THRESHOLD), str(STEP)
        state["alerted_index"] = None

    if price >= THRESHOLD:
        if state["alerted_index"] is not None:
            print("[信息] 价格回到阈值及以上，重置提醒")
        state["alerted_index"] = None
        return

    idx = deepest_index(price)
    prev = state["alerted_index"]
    if prev is not None and idx <= prev:
        print(f"[信息] 仍在已提醒范围内（已提醒到 {fmt(level(prev))}），不重复提醒")
        return

    crossed = level(idx)
    next_line = level(idx + 1)
    per_unit = (price / 100).quantize(Decimal("0.0001"))
    title = f"📉 英镑现汇卖出价跌破 {fmt(crossed)}"
    message = (
        f"当前现汇卖出价：{price:.2f}（每100英镑）\n"
        f"约合 1 英镑 = {per_unit} 人民币\n"
        f"中行发布时间：{published}（北京时间）\n"
        f"提醒阈值：{fmt(THRESHOLD)} ｜ 下一条提醒线：{fmt(next_line)}"
    )
    notify(title, message, priority=4, tags=["chart_with_downwards_trend"])
    state["alerted_index"] = idx


def main():
    now = now_beijing()
    print(f"[信息] 北京时间 {now:%Y-%m-%d %H:%M}，模式 {MODE}，阈值 {THRESHOLD}，步长 {STEP}")

    if MODE == "scheduled" and not (ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR):
        print("[信息] 不在北京时间检查时段内，跳过")
        return 0

    state = load_state()

    try:
        price, published = fetch_rate()
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 抓取失败：{exc}")
        if MODE == "test":
            notify("⚠️ 汇率监控测试：抓取失败", f"错误信息：{str(exc)[:300]}", priority=3, tags=["warning"])
            return 1
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        if state["consecutive_failures"] >= FAIL_ALERT_COUNT and not state["failure_alerted"]:
            last = "暂无记录"
            if state.get("last_price"):
                last = f"{state['last_price']}（中行发布于 {state.get('last_published')}）"
            notify(
                f"⚠️ 汇率监控连续 {state['consecutive_failures']} 次抓取失败",
                f"最后一次抓到的价格：{last}\n可能是中行网站暂时打不开，程序会继续尝试。",
                priority=3,
                tags=["warning"],
            )
            state["failure_alerted"] = True
        save_state(state)
        return 0

    print(f"[信息] 英镑现汇卖出价 {price}，发布时间 {published}")

    if MODE == "test":
        notify(
            "🔔 汇率监控测试推送",
            f"当前现汇卖出价：{price:.2f}（每100英镑）\n中行发布时间：{published}（北京时间）\n"
            f"提醒阈值：{fmt(THRESHOLD)} ｜ 步长：{fmt(STEP)}\n监控工作正常。",
            priority=3,
            tags=["white_check_mark"],
        )
        return 0

    if state.get("failure_alerted"):
        notify("✅ 汇率监控已恢复", f"当前现汇卖出价 {price:.2f}", priority=3, tags=["white_check_mark"])
    state["consecutive_failures"] = 0
    state["failure_alerted"] = False

    handle_price(state, price, published)

    state["last_price"] = f"{price:.2f}"
    state["last_published"] = published
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

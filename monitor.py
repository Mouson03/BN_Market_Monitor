#!/usr/bin/env python3
"""
币安标的价格监控 + PushPlus 微信推送
- 单次检测:  python monitor.py --once
- 循环监控:  python monitor.py --loop-minutes 70
- 测试推送:  python monitor.py --test
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"
BEIJING = timezone(timedelta(hours=8))

# GitHub Actions 运行机在美国，api.binance.com 有地区封锁，
# data-api.binance.vision 为币安公开行情专用域名、无地区限制，放最前。
PRICE_ENDPOINTS = [
    "https://data-api.binance.vision/api/v3/ticker/price",
    "https://api.binance.com/api/v3/ticker/price",
    "https://api1.binance.com/api/v3/ticker/price",
    "https://api2.binance.com/api/v3/ticker/price",
    "https://api3.binance.com/api/v3/ticker/price",
    "https://api4.binance.com/api/v3/ticker/price",
]
PUSHPLUS_URL = "http://www.pushplus.plus/send"
REQUEST_TIMEOUT = 10


class InvalidSymbolError(Exception):
    """交易对不存在或已下架"""


class ConfigError(Exception):
    """config.yaml 格式错误"""


def now_beijing() -> datetime:
    return datetime.now(BEIJING)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        raise ConfigError(f"config.yaml 读取/解析失败: {e}")
    if not isinstance(cfg, dict) or not cfg.get("pushplus_token"):
        raise ConfigError("config.yaml 缺少 pushplus_token")
    rules = cfg.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ConfigError("config.yaml 缺少 rules 列表")
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or not r.get("symbol"):
            raise ConfigError(f"rules[{i}] 缺少 symbol")
        if r.get("alert_type") not in ("price_below", "price_above"):
            raise ConfigError(f"rules[{i}] alert_type 必须是 price_below 或 price_above")
        if not isinstance(r.get("threshold"), (int, float)):
            raise ConfigError(f"rules[{i}] threshold 必须是数字")
        if r.get("frequency", "daily") not in ("once", "daily"):
            raise ConfigError(f"rules[{i}] frequency 必须是 once 或 daily")
    return cfg


def rule_id(rule: dict) -> str:
    return f"{rule['symbol']}:{rule['alert_type']}:{rule['threshold']}"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    commit_state()


def commit_state() -> None:
    """在 GitHub Actions 环境中把 state.json 提交回仓库（带冲突重试）。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for attempt in range(3):
        try:
            subprocess.run(["git", "add", "state.json"], check=True, cwd=BASE_DIR)
            # 没有变更时 commit 返回非零，属正常情况
            r = subprocess.run(
                ["git", "commit", "-m", "chore: update alert state [skip ci]"],
                cwd=BASE_DIR, capture_output=True, text=True,
            )
            if r.returncode != 0:
                return
            subprocess.run(["git", "pull", "--rebase"], check=True, cwd=BASE_DIR,
                           capture_output=True)
            subprocess.run(["git", "push"], check=True, cwd=BASE_DIR,
                           capture_output=True)
            return
        except subprocess.CalledProcessError:
            time.sleep(5)
    print("警告: state.json 提交失败，下次运行将按旧状态判断", flush=True)


def get_price(symbol: str) -> float | None:
    """拉取最新价。网络全挂返回 None；交易对无效抛 InvalidSymbolError。"""
    for round_no in range(2):  # 最多重试 2 轮
        for base in PRICE_ENDPOINTS:
            try:
                resp = requests.get(
                    base, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException:
                continue
            if resp.status_code == 200:
                try:
                    return float(resp.json()["price"])
                except (KeyError, ValueError):
                    continue
            # 400 + code -1121 = 无效交易对，重试无意义，直接抛出
            try:
                code = resp.json().get("code")
            except ValueError:
                code = None
            if code == -1121:
                raise InvalidSymbolError(symbol)
        if round_no == 0:
            time.sleep(3)
    return None


def push(token: str, title: str, content: str) -> bool:
    try:
        resp = requests.post(
            PUSHPLUS_URL,
            json={"token": token, "title": title,
                  "content": content, "template": "markdown"},
            timeout=REQUEST_TIMEOUT,
        )
        ok = resp.status_code == 200 and resp.json().get("code") == 200
        if not ok:
            print(f"PushPlus 推送失败: HTTP {resp.status_code} {resp.text[:200]}",
                  flush=True)
        return ok
    except Exception as e:
        print(f"PushPlus 推送异常: {e}", flush=True)
        return False


def is_triggered(rule: dict, price: float) -> bool:
    if rule["alert_type"] == "price_below":
        return price < rule["threshold"]
    return price > rule["threshold"]


def should_alert(rule: dict, state: dict) -> bool:
    freq = rule.get("frequency", "daily")
    st = state.get(rule_id(rule), {})
    if freq == "once":
        return not st.get("alerted", False)
    return st.get("last_alert_date") != now_beijing().strftime("%Y-%m-%d")


def mark_alerted(rule: dict, state: dict) -> None:
    state[rule_id(rule)] = {
        "alerted": True,
        "last_alert_date": now_beijing().strftime("%Y-%m-%d"),
    }


def condition_text(rule: dict) -> str:
    op = "低于" if rule["alert_type"] == "price_below" else "高于"
    return f"{op} {rule['threshold']}"


def check_once(cfg: dict, state: dict) -> None:
    """对所有规则执行一轮检测。"""
    ts = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    state_changed = False
    for rule in cfg["rules"]:
        symbol = rule["symbol"]
        try:
            price = get_price(symbol)
        except InvalidSymbolError:
            push(cfg["pushplus_token"], "监控配置错误",
                 f"标的 **{symbol}** 在币安不存在或已下架，请检查 config.yaml。")
            print(f"[{ts}] {symbol} 交易对无效，本轮终止", flush=True)
            sys.exit(0)
        if price is None:
            print(f"[{ts}] {symbol} 价格拉取失败，本轮跳过", flush=True)
            continue
        triggered = is_triggered(rule, price)
        print(f"[{ts}] {symbol} = {price} | 条件: {condition_text(rule)} | "
              f"触发: {triggered}", flush=True)
        if not triggered or not should_alert(rule, state):
            continue
        title = f"价格提醒: {symbol} 已{condition_text(rule)}"
        content = (
            f"## 币安价格提醒\n\n"
            f"- **标的**: {symbol}\n"
            f"- **当前价格**: {price} USDT\n"
            f"- **触发条件**: 价格{condition_text(rule)}\n"
            f"- **触发时间**: {ts}（北京时间）\n\n"
            f"[查看币安行情](https://www.binance.com/zh-CN/trade/"
            f"{symbol.replace('USDT', '')}_USDT)"
        )
        if push(cfg["pushplus_token"], title, content):
            mark_alerted(rule, state)
            state_changed = True
    if state_changed:
        save_state(state)


def test_push(cfg: dict) -> None:
    lines = ["## 监控测试推送\n", "| 标的 | 最新价 |", "|---|---|"]
    for rule in cfg["rules"]:
        try:
            price = get_price(rule["symbol"])
        except InvalidSymbolError:
            price = None
        lines.append(f"| {rule['symbol']} | "
                     f"{'拉取失败' if price is None else price} |")
    lines.append(f"\n时间: {now_beijing().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）")
    ok = push(cfg["pushplus_token"], "币安价格监控 - 测试推送", "\n".join(lines))
    print("测试推送" + ("成功" if ok else "失败"), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="币安价格监控")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="只检测一轮后退出")
    g.add_argument("--loop-minutes", type=int, default=0,
                   help="循环运行 N 分钟，每 60 秒检测一轮")
    parser.add_argument("--test", action="store_true",
                        help="发送一条测试推送后退出")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"配置错误: {e}", flush=True)
        # 配置都读不出来时无法推送（可能没有 token），仅退出
        sys.exit(1)

    if args.test:
        test_push(cfg)
        return

    state = load_state()
    if args.loop_minutes and args.loop_minutes > 0:
        deadline = time.monotonic() + args.loop_minutes * 60
        while True:
            check_once(cfg, state)
            state = load_state()  # 可能被其他并发运行更新
            if time.monotonic() + 60 > deadline:
                break
            time.sleep(60)
        print("本轮监控时长结束，退出", flush=True)
    else:
        check_once(cfg, state)


if __name__ == "__main__":
    main()

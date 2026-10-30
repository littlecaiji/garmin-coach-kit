#!/usr/bin/env python3
"""回写 secret 之前的守门员：确认 Garmin token 文件**没坏**。

## 为什么需要

工作流跑完会把 token 写回 GitHub secret（轮转结果必须回写，否则整族失效）。
但如果这一轮因为别的原因把文件写坏了 —— 空文件、半截 JSON、被清空的字段 ——
**盲目回写等于用坏文件覆盖掉 secret 里唯一的好 token**，第二天就彻底登不上。
2026-10-19 那次故障的教训就是：不要相信文件，要校验。

## 判据

必须同时满足：
  1. 是合法 JSON 对象；
  2. 含非空字符串 `di_token`、`di_refresh_token`（各 >20 字符）。

`di_token` 的 JWT `exp` 只作提示（过期不等于是坏文件 —— 过期正是需要刷新的场景）。

用法：python3 check_garmin_tokens.py <token.json> [...]
退出码：0 = 全部通过；1 = 有文件不合法（调用方应拒绝回写）。
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone

REQUIRED = ("di_token", "di_refresh_token")


def jwt_exp(token: str) -> datetime | None:
    """不验签，只解 payload 取 exp —— 仅用于打印剩余寿命。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(data["exp"]), tz=timezone.utc)
    except Exception:
        return None


def check(path: str) -> str | None:
    """返回 None 表示合格，否则返回原因。"""
    try:
        with open(path) as f:
            d = json.load(f)
    except FileNotFoundError:
        return "文件不存在"
    except json.JSONDecodeError as e:
        return f"不是合法 JSON（{e}）"
    except OSError as e:
        return f"读取失败（{e}）"

    if not isinstance(d, dict):
        return f"顶层不是对象，而是 {type(d).__name__}"
    for k in REQUIRED:
        v = d.get(k)
        if not isinstance(v, str) or len(v) <= 20:
            return f"字段 {k} 缺失或过短"
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: check_garmin_tokens.py <token.json> [...]")
        return 1

    bad = 0
    for path in argv:
        why = check(path)
        if why:
            print(f"❌ {path}: {why}")
            bad += 1
            continue
        d = json.load(open(path))
        exp = jwt_exp(d["di_token"])
        if exp:
            left = (exp - datetime.now(timezone.utc)).total_seconds() / 3600
            state = "仍有效" if left > 0 else "**已过期**"
            print(f"✅ {path}: 结构完整，di_token {state}"
                  f"（exp {exp:%Y-%m-%d %H:%M} UTC，{left:+.1f}h）")
        else:
            print(f"✅ {path}: 结构完整（di_token 无法解析 exp，跳过寿命检查）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

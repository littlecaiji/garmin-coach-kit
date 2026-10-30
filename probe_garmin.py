#!/usr/bin/env python3
"""Garmin 云端可行性探针。

目的只有一个：判断 Garmin Connect 会不会因为「异地数据中心 IP」拒绝我们的
会话。这是把周报搬上云唯一的真实风险点——算力、依赖都不是问题。

成功 = 能登录 + 能拉到活动数据。
失败 = Garmin 要求重新 MFA 登录（token 被判异常），此时上云方案需要换思路。

退出码：
  0  通过
  1  Garmin 拒绝 / token 失效（预期中的失败，需要重新授权）
  2  其他异常
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

TOKEN_DIR = os.path.expanduser("~/.garminconnect")
TOKEN_FILE = os.path.join(TOKEN_DIR, "garmin_tokens.json")


def public_ip():
    """打印 runner 的公网出口 IP——出问题时要靠它对比是否被风控。"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return "<无法获取>"


def main():
    print("=" * 62)
    print("Garmin 云端可行性探针")
    print("=" * 62)
    print(f"时间(UTC)      : {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"公网出口 IP    : {public_ip()}")
    print(f"token 目录     : {TOKEN_DIR}")

    if not os.path.exists(TOKEN_FILE):
        print("\n❌ 没有找到 token 文件。检查 secret GARMIN_TOKENS_B64 是否已写入。")
        return 1

    size = os.path.getsize(TOKEN_FILE)
    mtime = datetime.fromtimestamp(os.path.getmtime(TOKEN_FILE)).isoformat(timespec="seconds")
    print(f"token 文件     : {size} bytes, mtime={mtime}")

    try:
        with open(TOKEN_FILE) as f:
            blob = json.load(f)
        # 只打印键名，绝不打印内容（access/refresh token 泄露=账号被接管）
        print(f"token 键       : {sorted(blob.keys())}")
    except Exception as e:
        print(f"⚠️  token 文件解析失败: {e}")

    print("\n— 尝试登录 Garmin Connect —")
    try:
        from garminconnect import Garmin

        client = Garmin()
        client.login(TOKEN_DIR)
        print("✅ 登录成功（会话在异地数据中心 IP 上被接受）")
    except Exception as e:
        print(f"❌ 登录失败: {type(e).__name__}: {e}")
        print("\n→ 结论：Garmin 拒绝了异地会话。需要在云端重新完成一次 MFA 登录，")
        print("  或者放弃上云、改用本地定时唤醒（pmset）兜底。")
        return 1

    print("\n— 拉取最近 5 条活动 —")
    try:
        acts = client.get_activities(0, 5)
    except Exception as e:
        print(f"❌ 拉取活动失败: {type(e).__name__}: {e}")
        return 1

    if not acts:
        print("（没有活动记录）")
    for a in acts:
        date = (a.get("startTimeLocal") or "")[:16]
        dist = (a.get("distance") or 0) / 1000
        dur = (a.get("duration") or 0) / 60
        name = a.get("activityName", "")
        print(f"  {date} | {dist:7.1f}km | {dur:5.0f}min | {name}")

    print("\n" + "=" * 62)
    print("✅ 探针通过——Garmin 接受异地会话，可以继续做云端周报。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())

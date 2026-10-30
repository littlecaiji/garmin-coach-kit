#!/usr/bin/env python3
"""Garmin token 保活：刷新 access/refresh token，并让调用方把新值写回 secret。

为什么需要它（2026-10-19 事故复盘）：
  Garmin 的 refresh token 是**轮转**的（库源码 client.py:1184
  `self.di_refresh_token = data.get("refresh_token", self.di_refresh_token)`），
  每刷新一次旧值即作废。以前 token 存在本地文件里，dump() 会写回同一个文件，
  链条自动延续；搬到 Actions secret 后，secret 是**只读快照**，runner 里刷出来的
  新 token 随容器销毁 —— 于是每跑一次就在制造一张废票，最终整族失效，
  本地那份也跟着死。

行为：
  1. 从 ~/.garminconnect 读 token（由 workflow 从 secret 还原）；
  2. 只有当 access token 剩余寿命 < 12h 时才真正刷新（避免无意义轮转）；
  3. 刷新后立即拉 1 条活动自检，确认新 token 可用；
  4. 若 token 内容发生变化，往 $GITHUB_OUTPUT 写 changed=true，由 workflow
     用 PAT 调 gh secret set 写回 GARMIN_TOKENS_B64。

退出码：0 正常（含「无需刷新」）；1 token 已死或刷新失败（需要本地重登）。
"""
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta

TOKEN_DIR = os.path.expanduser("~/.garminconnect")
TOKEN_FILE = os.path.join(TOKEN_DIR, "garmin_tokens.json")
REFRESH_IF_LESS_THAN = timedelta(hours=12)


def sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def token_exp() -> datetime | None:
    try:
        with open(TOKEN_FILE) as f:
            t = json.load(f).get("di_token", "")
        payload = t.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(exp) if exp else None
    except Exception:
        return None


def emit_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


def main() -> int:
    if not os.path.exists(TOKEN_FILE):
        print("❌ 没有 token 文件 —— secret GARMIN_TOKENS_B64 可能没写入")
        return 1

    before = sha(TOKEN_FILE)
    exp = token_exp()
    now = datetime.now()
    force = os.environ.get("FORCE_REFRESH") == "1"
    print(f"当前 token：exp={exp}  剩余={exp - now if exp else '?'}")

    if not force and exp and exp - now > REFRESH_IF_LESS_THAN:
        print("ℹ️ 剩余寿命仍充足，本次不刷新（避免无意义轮转）")
        emit_output("changed", "false")
        return 0
    if force:
        print("⚠️ FORCE_REFRESH=1 —— 无视剩余寿命，强制刷新（仅测试用）")

    from garminconnect import Garmin

    print("→ 刷新中…")
    try:
        client = Garmin()          # 不带账号密码：只走 token + refresh token
        client.login(TOKEN_DIR)    # 内部遇到「快过期」会调 _refresh_di_token 并 dump
        if force:
            # 库只在「15 分钟内到期」时才自己刷新；force 测试要显式调一次轮转，
            # 否则只会原样 dump，changed=false，写回链路根本没被验证到。
            client.client._refresh_di_token()
            print("✅ 已强制轮转（FORCE_REFRESH）")
    except Exception as e:  # noqa: BLE001
        print(f"❌ 刷新失败 {type(e).__name__}: {e}")
        print("→ 说明 refresh token 已失效，需要在本地跑 work/garmin_relogin.py 重新登录")
        return 1

    # 双保险：显式 dump（login 内部路径依赖 tokenstore 参数，这里显式再写一次）
    try:
        client.client.dump(TOKEN_DIR)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 显式 dump 失败（login 可能已写过）: {type(e).__name__}: {e}")

    try:
        acts = client.get_activities(0, 1)
        print(f"✅ 新 token 可用，拉取到 {len(acts)} 条活动")
    except Exception as e:  # noqa: BLE001
        print(f"❌ 刷新后仍拉不到数据: {type(e).__name__}: {e}")
        return 1

    after = sha(TOKEN_FILE)
    new_exp = token_exp()
    print(f"新 token：exp={new_exp}  changed={'是' if after != before else '否'}")
    emit_output("changed", "true" if after != before else "false")
    if new_exp:
        emit_output("exp", new_exp.isoformat(timespec="seconds"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

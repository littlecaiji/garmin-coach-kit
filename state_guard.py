#!/usr/bin/env python3
"""推送前校验：本地 state 是否比云端旧（防止把云端算好的跨周记忆覆写掉）。

## 为什么需要这个

`state/training_state.json` 是「唯一真相源」，而它**会被云端 workflow 自己
commit 回仓库**（每次周报跑完都有 `周报 YYYY-MM-DD [skip ci]` 这个提交）。
本地仓库不会自动跟进 —— 于是出现下面这个真实事故：

    10/19 云端跑完周报 → e232fa1：last_advanced_week=2026-W43、力量断档 1 周、
                        长骑 last_long=76.2、决策日志 6 条（含 HRV 闸门那条）
    10/19 本地手改 state 补 gate 字段（用的是 10/12 那份旧文件）→ a2d073a
                        last_advanced_week 被打回 2026-W42、updated 打回 10-12

后果不是「报告难看」，而是**幂等闸门被踩穿**：本周（W43）内只要再跑一次
周报（补跑、手动 dispatch、调试），`last_advanced_week(W42) != week(W43)`
就成立 → 阈值课 3×10→4×10、VO2max 5×3→6×3、力量负重全线 +、长骑基准 +30min。
而这本来是个零强度恢复周，加量毫无依据 —— 这正是「负荷过山车」的生成机制。

## 判据

只比两个有语义的字段，不比整份文件（本地本来就可能有待提交的改动）：

- `last_advanced_week` 更旧  → 判定「本地落后」，退出码 3（阻止推送）
- `updated` 更旧（同周内）  → 同样判定落后
- 完全读不到（网络/鉴权）   → 只警告**不拦**，退出码 0
  （不该因为 GitHub 抽风就把本地提交卡死）

## 用法

    python3 state_guard.py                      # 只检查（在 cloud/ 下执行）
    python3 state_guard.py --state ../state/training_state.json

push_and_watch.sh 会在 push 前自动调用；确需强行推送时设 SKIP_STATE_GUARD=1。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.environ.get("DATA_REPO", "littlecaiji/garmin-training-data")
BRANCH = "main"
REMOTE_PATH = "state/training_state.json"
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import coach_state as _cs          # 唯一解析入口，别在这里另拼路径
    DEFAULT_STATE = _cs.STATE_PATH
except Exception:                       # noqa: BLE001 —— 缺依赖也不该让守门员崩
    DEFAULT_STATE = os.path.join(os.path.abspath(os.path.join(_HERE, os.pardir)),
                                 "data", "state", "training_state.json")
DEFAULT_TOKEN = os.path.join(_HERE, "..", "work", "gh_token.txt")
PROXY = os.environ.get("PROXY", "http://127.0.0.1:7891")


def _get(url: str, token: str, proxy: str | None):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "state-guard",
    })
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as r:
        return json.loads(r.read().decode())


def fetch_remote_state(token: str) -> dict | None:
    """先直连（api.github.com 通常可达），失败再走代理。"""
    url = f"https://api.github.com/repos/{REPO}/contents/{REMOTE_PATH}?ref={BRANCH}"
    for proxy in (None, PROXY):
        try:
            data = _get(url, token, proxy)
            return json.loads(base64.b64decode(data["content"]).decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None            # 远端还没有这个文件，不算落后
            # 401/403 是鉴权问题，换代理也没用
            if e.code in (401, 403):
                print(f"⚠️  读取远端 state 被拒（HTTP {e.code}）—— token 权限或过期。"
                      f"跳过校验，不阻止推送。")
                return None
        except Exception:
            continue
    print("⚠️  连不上 api.github.com（直连与代理都失败）—— 跳过 state 新鲜度校验。")
    return None


def _week_rank(week: str | None) -> tuple[int, int]:
    """'2026-W42' → (2026, 42)；无法解析时返回 (-1, -1)，视为最旧。"""
    try:
        y, w = str(week).split("-W")
        return int(y), int(w)
    except Exception:
        return (-1, -1)


def _pull(remote: dict, local_path: str, local: dict, force: bool) -> int:
    """把云端 state 写回本地两份副本（cloud/state 与项目根 state）。"""
    rw, rwk = remote.get("last_advanced_week"), _week_rank(remote.get("last_advanced_week"))
    lw, lwk = local.get("last_advanced_week"), _week_rank(local.get("last_advanced_week"))

    if lwk > rwk and not force:
        print(f"⚠️  本地 state 比云端新（本地 {lw} vs 云端 {rw}）—— 默认不覆盖，"
              f"免得把本地还没推的改动冲掉。")
        print("   确实要丢弃本地改动：python3 state_guard.py --pull --force")
        return 4

    txt = json.dumps(remote, ensure_ascii=False, indent=2) + "\n"
    # 2026-10-30 起 state 只有**一份**（私有数据仓库 data/state）。
    # 以前 cloud/state 与根目录 state 两份镜像，是为了兼顾「仓库里的那份」和
    # 「本地开发用的那份」；数据仓库独立后这个二元结构消失，再写两份只会
    # 制造一个迟早过期的影子副本 —— 那正是 10/19 那次事故的土壤。
    with open(local_path, "w") as f:
        f.write(txt)
    print(f"✅ 已写入 {local_path}")
    print(f"\n   云端真值：last_advanced_week={rw}  updated={remote.get('updated')}")
    print(f"   覆盖掉的本地值：last_advanced_week={lw}  updated={local.get('updated')}")
    print("\n现在可以在此基础上改动了。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--token-file", default=DEFAULT_TOKEN)
    ap.add_argument("--pull", action="store_true",
                    help="把云端 state 拉回本地（写 cloud/state 与根目录两份副本）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --pull：即使本地更新也覆盖（默认拒绝，防丢本地改动）")
    args = ap.parse_args()

    local_path = os.path.abspath(args.state)
    if not os.path.exists(local_path):
        print(f"⚠️  找不到本地 state：{local_path} —— 跳过校验")
        return 0
    local = json.load(open(local_path))

    try:
        token = open(os.path.expanduser(args.token_file)).read().strip()
    except OSError:
        print(f"⚠️  读不到 token（{args.token_file}）—— 跳过 state 校验")
        return 0
    if not token:
        print("⚠️  token 文件是空的 —— 跳过 state 校验")
        return 0

    remote = fetch_remote_state(token)
    if remote is None:
        return 0

    if args.pull:
        return _pull(remote, local_path, local, args.force)

    lw, rw = local.get("last_advanced_week"), remote.get("last_advanced_week")
    lu, ru = local.get("updated"), remote.get("updated")

    behind_week = _week_rank(lw) < _week_rank(rw)
    behind_day = (not behind_week) and _week_rank(lw) == _week_rank(rw) and \
        str(lu or "") < str(ru or "")

    if behind_week or behind_day:
        print("")
        print("🛑 本地 state 比云端旧，拒绝推送 —— 你会把云端算好的跨周记忆覆写掉。")
        print("")
        print(f"    本地：last_advanced_week={lw}   updated={lu}   文件={local_path}")
        print(f"    云端：last_advanced_week={rw}   updated={ru}   ({BRANCH})")
        print("")
        print("  这不是「报告不好看」的问题：last_advanced_week 一旦倒退，本周内")
        print("  再跑一次周报就会把它当成新的一周**再推进一次档位**（阈值/VO2 组数、")
        print("  力量负重、长骑基准全线无依据上涨）。")
        print("")
        print("  处理方式（二选一）：")
        print("    1) 先拉云端真值，再在上面改：")
        print("         python3 state_guard.py --pull")
        print("       改完再推。")
        print("    2) 确认就是要在本地重建 state（例如云端那版已损坏）：")
        print("         SKIP_STATE_GUARD=1 ./push_and_watch.sh")
        print("")
        return 3

    if lw != rw or lu != ru:
        print(f"ℹ️  state 本地({lw}/{lu}) 与云端({rw}/{ru}) 不一致但不构成落后"
              f"（本地是新的一版），放行。")
    else:
        print(f"✅ state 新鲜度校验通过（{lw} / {lu}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

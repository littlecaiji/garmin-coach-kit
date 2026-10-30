#!/usr/bin/env python3
"""把 Garmin 国际服（connect.garmin.com）的活动单向复制到国服（connect.garmin.cn）。

## 这是什么 / 不是什么

- 是：**活动级单向复制**。读国际服原始 FIT → 传到国服。佳速度的好友榜、挑战赛、
  勋章、段位都能吃到这份数据。
- 不是：账号镜像。国服那份是「裸活动」——
  体重 / 睡眠 / **HRV** / 静息心率 / Body Battery / 训练状态 / 训练负荷(CTL-ATL) /
  设备绑定 / 训练计划 / 步数 全都**不会**过去。国服会从零重算自己的训练负荷。
- 特别地：**微信运动步数同步不了**。步数属于日常活动（dailies），只有设备直传国服
  才有，FIT 活动文件里根本没有这项。而且同一块手表同一时间只能绑一个服，
  所以永远只能是「服务器端复制」，不是「设备双写」。

> 结论：国服只能当「展示 / 社交」，当不了「教练分析源」。项目里的周报依旧只认国际服。

## 去重（三层，缺一不可）

1. **状态文件** `state/cn_sync_state.json` 记录已传过的 activity_id。
2. **时间+距离指纹**：上传前把国服同时间窗的活动拉下来建索引，命中就跳过。
   这层是为了扛「状态文件丢了 / 换机器 / 手工传过一条」的情况 —— 单靠状态文件
   太脆，10/19 的 state 事故已经证明过一次。
3. **HTTP 409**：国服自己判重复，按「已存在」处理，不算失败。

## 用法

    # 1) 首次：交互登录国服，token 落到 ~/.garminconnect-cn（密码不落盘）
    python3 sync_garmin_cn.py --auth

    # 2) 只读体检：两边各有多少、待传多少、哪几条会被跳过（不上传任何东西）
    python3 sync_garmin_cn.py --probe --days 30

    # 3) 正式同步最近 N 天
    python3 sync_garmin_cn.py --sync --days 7

    # 4) 历史回填（带限速，别一次传太多）
    python3 sync_garmin_cn.py --sync --from 2026-08-01 --to 2026-10-01

    # 5) 看同步进度
    python3 sync_garmin_cn.py --status

    # 6) 全量核对（状态文件里每条是否真在国服；也用于回填老记录）
    python3 sync_garmin_cn.py --verify

国际服的 token 复用 `~/.garminconnect`（周报那套，有 keepalive 保活）；
国服单独一套 `~/.garminconnect-cn`，**同样有 refresh 轮转问题**，需要自己的保活。
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import random
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

from garminconnect import Garmin

# 路径全部可用环境变量覆盖 —— 本地和 GitHub Actions 共用同一份代码，
# 但状态/缓存不该进 git（每天都会变），所以放家目录而不是仓库目录。
# 注意：本文件不依赖自己的所在目录，因此可以放在 cloud/ 下、由根目录软链引用。
INTL_TOKEN_DIR = os.path.expanduser(os.getenv("GARMIN_TOKEN_DIR") or "~/.garminconnect")
CN_TOKEN_DIR = os.path.expanduser(os.getenv("GARMIN_CN_TOKEN_DIR") or "~/.garminconnect-cn")
STATE_FILE = os.path.expanduser(
    os.getenv("CN_SYNC_STATE") or "~/.garmin-sync/cn_sync_state.json")
FIT_CACHE = os.path.expanduser(
    os.getenv("CN_SYNC_CACHE") or "~/.garmin-sync/fit_cache")

# 国服风控：官方建议每次分批 100-500 个文件；脚本层面再保守一点。
THROTTLE_SEC = 2.0          # 每条之间
BATCH_PAUSE_EVERY = 25      # 每 N 条
BATCH_PAUSE_SEC = 30.0


# ────────────────────────────── 状态 ──────────────────────────────
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"version": 1, "synced": {}, "last_run": None, "stats": {}}
    with open(STATE_FILE) as f:
        st = json.load(f)
    st.setdefault("synced", {})
    return st


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    st["last_run"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)          # 原子写，别留半个 JSON


# ────────────────────────────── 登录 ──────────────────────────────
def connect_intl():
    """国际服：复用周报那套 token（只读 token，不需要密码）。"""
    if not os.path.isdir(INTL_TOKEN_DIR):
        sys.exit(f"❌ 找不到国际服 token 目录 {INTL_TOKEN_DIR} —— 先跑一次周报脚本。")
    c = Garmin()
    c.login(INTL_TOKEN_DIR)
    _flush_tokens(c, INTL_TOKEN_DIR)
    return c


def connect_cn():
    if not os.path.isdir(CN_TOKEN_DIR):
        sys.exit(f"❌ 还没有国服 token：{CN_TOKEN_DIR}\n"
                 f"   先跑：python3 {os.path.basename(__file__)} --auth")
    c = Garmin(is_cn=True)
    c.login(CN_TOKEN_DIR)
    _flush_tokens(c, CN_TOKEN_DIR)
    return c


def _flush_tokens(c, token_dir: str) -> None:
    """登录时若触发了自动刷新，**必须立刻把轮转结果写回磁盘**。

    Garmin 的 refresh token 是轮转的：刷新一次，旧值立刻作废。`login(tokenstore)`
    只在「用账号密码新建会话」那一步才 dump，纯 token 恢复 + 临近过期自动刷新
    这条路径**不会落盘**（10/19 整族失效就是这么来的）。所以在 Actions 里
    跑完如果不写回 secret，等于每跑一次烧掉一张票。
    """
    try:
        c.client.dump(token_dir)
    except Exception as e:      # 落盘失败不该让整个同步崩掉，但要说出来
        print(f"   ⚠️ token 回写失败（{type(e).__name__}: {e}）—— 该账号下次可能需重登")


def cmd_auth() -> int:
    print("国服 Garmin 登录（密码只在内存里用一次，不落盘）\n")
    email = input("国服账号（邮箱 / 手机号）: ").strip()
    if not email:
        print("❌ 账号为空")
        return 1
    pwd = getpass.getpass("密码（输入时不显示）: ")
    if not pwd:
        print("❌ 密码为空")
        return 1

    os.makedirs(CN_TOKEN_DIR, exist_ok=True)
    token_file = os.path.join(CN_TOKEN_DIR, "garmin_tokens.json")
    before = os.path.getmtime(token_file) if os.path.exists(token_file) else 0

    c = Garmin(
        email, pwd,
        is_cn=True,
        prompt_mfa=lambda: input("MFA 验证码: ").strip(),
    )
    try:
        # 必须显式传路径，否则 0.3.7 不会 dump（10/19 踩过这个坑）
        c.login(CN_TOKEN_DIR)
        c.client.dump(CN_TOKEN_DIR)      # 二次兜底
    except Exception as e:
        print(f"\n❌ 国服登录失败：{type(e).__name__}: {e}")
        print("   常见原因：账号其实属于国际服 / 密码错 / 触发风控需稍后再试 / 需要 MFA")
        return 1

    # 严格校验：文件真的变新了吗？会话真的能用吗？
    ok_file = os.path.exists(token_file) and os.path.getmtime(token_file) > before
    if not ok_file:
        # 首次登录时 before=0，只要文件存在就算通过
        ok_file = os.path.exists(token_file)
    try:
        name = c.get_full_name()
        ok_api = bool(name)
    except Exception as e:
        name, ok_api = f"（读取失败：{e}）", False

    print(f"\n   token 文件：{token_file}")
    print(f"   token 落盘：{'✅' if ok_file else '❌'}")
    print(f"   会话可用  ：{'✅' if ok_api else '❌'}  显示名 = {name}")
    if not (ok_file and ok_api):
        print("\n❌ 登录没真正生效，不要往下走。")
        return 1
    print("\n✅ 国服就绪。下一步：python3 sync_garmin_cn.py --probe --days 30")
    return 0


# ────────────────────────────── 活动工具 ──────────────────────────────
def _epoch_sec(act: dict) -> int:
    """把活动开始时间统一成 UTC epoch 秒。

    必须统一，不能直接拿字段原值比 —— 国际服返回 `beginTimestamp`（epoch 毫秒），
    国服可能只给 `startTimeGMT`（"YYYY-MM-DD HH:MM:SS" 字符串）。两边字段名不同、
    类型不同，原值直接比永远不等 → 去重失效 → 同一条活动反复上传。

    ⚠️ 只用 GMT 字段。`startTimeLocal` 是本地时区，两边时区设置不同就会错开。
    """
    for key in ("beginTimestamp", "startTimeGMT"):
        v = act.get(key)
        if v is None or v == "":
            continue
        s = str(v).strip()
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            n = int(s)
            # 13 位是毫秒，10 位是秒
            return n // 1000 if n > 1_000_000_000_000 else n
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue
    return 0


def fp(act: dict) -> tuple:
    """活动指纹：(UTC 开始秒, 距离取整到 10m)。

    距离取整到 10m 是为了容忍 GPS 抖动（同一条 FIT 两边解析出来的距离可能
    差几米）。时间不用取整 —— 同一条活动的开始时间是精确相同的。
    """
    try:
        dist = round(float(act.get("distance") or 0) / 10.0)
    except (TypeError, ValueError):
        dist = 0
    ts = _epoch_sec(act)
    if ts == 0:
        # 时间解析不出来就别硬猜，用 id 兜底：宁可多传一次（409 会拦），
        # 也不要把两条不同活动错判成同一条而漏传。
        return (f"raw:{act.get('activityId')}", dist)
    return (ts, dist)


def list_intl(c, start: str, end: str, only_cycling: bool) -> list[dict]:
    acts = c.get_activities_by_date(start, end) or []
    if only_cycling:
        acts = [a for a in acts
                if (a.get("activityType") or {}).get("typeKey", "").startswith("cycling")]
    # 老 → 新，方便看进度（用统一的 epoch 排序，别依赖两边字段名一致）
    acts.sort(key=_epoch_sec)
    return acts


def list_cn_index(c, start: str, end: str) -> set[tuple]:
    """国服同时间窗已存在的活动指纹集合。

    ⚠️ 前后各放宽 2 天：时区/跨日边界会让同一条活动落在窗口外，
    窗口太紧会导致「明明传过却认不出来」→ 重复上传。
    """
    s = (date.fromisoformat(start) - timedelta(days=2)).isoformat()
    e = (date.fromisoformat(end) + timedelta(days=2)).isoformat()
    acts = c.get_activities_by_date(s, e) or []
    return {fp(a) for a in acts}


def _is_duplicate_error(e: Exception) -> bool:
    s = str(e).lower()
    return "409" in s or "already exists" in s or "duplicate" in s


def parse_import_result(result) -> tuple[list, list]:
    """国服/国际服的上传响应是**嵌套**的：

        {"detailedImportResult": {"successes": [...], "failures": [...]}}

    直接取顶层 `successes` 永远拿到空列表 → 失败会被当成成功混过去。
    这里把两种形状都兼容，宁可多剥一层，也别把「失败」静默记成「成功」。
    """
    if not isinstance(result, dict):
        return [], []
    node = result.get("detailedImportResult") or result
    succ = node.get("successes") or []
    fails = node.get("failures") or []
    return succ, fails


def download_fit(c, activity_id, dest_dir: str) -> str:
    """下载原始活动，**解包**成真正的 FIT 落盘，返回可上传的路径。

    ⚠️ 2026-10-20 实测踩到：`download_activity(..., ORIGINAL)` 返回的**不是裸 FIT，
    而是 ZIP**（开头 `PK\\x03\\x04`），里面才是 `24455673035_ACTIVITY.fit`。
    原来直接把这段字节写成 `{id}.fit` 去上传，国服按 FIT 解析 ZIP 头对不上，
    回 `415 The file is of a type we do not support`。

    所以这里必须做两件事：
    1. ZIP 就解压取里面的 .fit（可能是裸 FIT，也可能直接是 ZIP，两种都得支持）；
    2. **上传前校验魔数**（FIT 的第 8-12 字节必须是 `.FIT`）。宁可本地报错，
       也别把格式不对的东西送去国服换一个语焉不详的 415。
    拿不到合法 FIT 时回退 TCX（国服/国际服都接受 .tcx）。
    """
    raw = c.download_activity(activity_id, Garmin.ActivityDownloadFormat.ORIGINAL)
    if not raw:
        raise RuntimeError("下载到 0 字节")

    data = raw
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                fits = [n for n in zf.namelist() if n.lower().endswith(".fit")]
                if fits:
                    data = zf.read(fits[0])
        except zipfile.BadZipFile:
            pass

    if len(data) >= 12 and data[8:12] == b".FIT":
        path = os.path.join(dest_dir, f"{activity_id}.fit")
        with open(path, "wb") as f:
            f.write(data)
        return path

    # 回退：TCX（XML）。丢一点 FIT 专有字段，但轨迹/功率/心率都在。
    tcx = c.download_activity(activity_id, Garmin.ActivityDownloadFormat.TCX)
    if tcx and tcx[:5] == b"<?xml":
        path = os.path.join(dest_dir, f"{activity_id}.tcx")
        with open(path, "wb") as f:
            f.write(tcx)
        print(f"        ⚠️ 拿不到合法 FIT，已回退 TCX（{len(tcx)/1024:.0f}KB）")
        return path

    head = data[:16].hex(" ") if data else "(空)"
    raise RuntimeError(f"既不是合法 FIT 也不是 TCX，前 16 字节: {head}")


def fmt_act(a: dict) -> str:
    t = str(a.get("startTimeLocal") or a.get("startTimeGMT") or "?")[:16]
    km = (a.get("distance") or 0) / 1000.0
    name = (a.get("activityName") or "").strip()
    return f"{t}  {km:6.1f}km  {name[:34]}"


# ────────────────────────────── probe ──────────────────────────────
def cmd_probe(days: int, only_cycling: bool) -> int:
    end = date.today()
    start = end - timedelta(days=days - 1)
    s, e = start.isoformat(), end.isoformat()

    print(f"窗口：{s} → {e}（{days} 天）\n")
    intl = connect_intl()
    print(f"✅ 国际服连接成功：{intl.get_full_name()}")
    acts = list_intl(intl, s, e, only_cycling)
    print(f"   国际服活动 {len(acts)} 条")

    cn = connect_cn()
    print(f"✅ 国服连接成功：{cn.get_full_name()}")
    idx = list_cn_index(cn, s, e)
    print(f"   国服同窗口（±2 天）活动 {len(idx)} 条\n")

    st = load_state()
    synced = st.get("synced", {})

    pending, skipped = [], []
    for a in acts:
        aid = str(a.get("activityId"))
        reason = None
        if aid in synced:
            reason = "状态文件已记录"
        elif fp(a) in idx:
            reason = "国服已有同时间+距离的活动"
        if reason:
            skipped.append((a, reason))
        else:
            pending.append(a)

    print(f"→ 待同步 {len(pending)} 条，跳过 {len(skipped)} 条\n")
    if pending:
        print("待同步：")
        for a in pending:
            print("   + " + fmt_act(a))
    if skipped:
        print("\n跳过：")
        for a, r in skipped:
            print(f"   = {fmt_act(a)}   [{r}]")
    print("\n（probe 是只读的，没有上传任何东西）")
    print("确认无误后：python3 sync_garmin_cn.py --sync --days %d" % days)
    return 0


# ────────────────────────────── sync ──────────────────────────────
def verify_pending(cn, st: dict) -> tuple[int, int]:
    """回查「声称已上传」的条目是否**真的**进了国服。返回 (确认数, 剔除数)。

    ## 为什么非得有这一步

    `import_activity()` 对国服的响应解析不出 JSON，库里会**兜底返回**
    `{"status": "uploaded", "fileName": ...}` —— 因此**上传成功与否，调用方根本看不出来**
    （实测 9/9 条全是这个返回值，是回查国服活动列表才确认真的入库的）。

    国服一旦静默拒收（HTTP 200 但没入库），会被当成成功写进 state，
    然后**永远不再重试** —— 这是最难发现的一类故障。所以必须用
    「时间+距离指纹」回查一次，查不到的从 synced 里剔除，下次自动重传。

    容忍延迟：刚上传的活动可能要几秒到几分钟才出现在列表里，所以只做
    「剔除」不做「报错」，下次运行还会再核一次。
    """
    synced = st.get("synced") or {}
    pend = {k: v for k, v in synced.items()
            if v.get("verified") is False and v.get("fp")}
    if not pend:
        return 0, 0

    days = sorted(str(v.get("start") or "")[:10] for v in pend.values()
                  if str(v.get("start") or "")[:10])
    if not days:
        return 0, 0

    try:
        idx = list_cn_index(cn, days[0], days[-1])
    except Exception as e:
        print(f"   ⚠️ 回查失败（{type(e).__name__}: {e}），本轮不剔除，下次再核")
        return 0, 0

    ok = gone = 0
    for aid, v in list(pend.items()):
        if tuple(v["fp"]) in idx:
            v["verified"] = True
            ok += 1
        else:
            del synced[aid]
            gone += 1
    save_state(st)
    return ok, gone


def cmd_sync(start: str, end: str, only_cycling: bool, dry_run: bool,
             max_items: int | None, summary_out: str | None = None) -> int:
    print(f"窗口：{start} → {end}{'（DRY RUN，不真传）' if dry_run else ''}\n")
    intl = connect_intl()
    print(f"✅ 国际服：{intl.get_full_name()}")
    acts = list_intl(intl, start, end, only_cycling)
    print(f"   活动 {len(acts)} 条")

    cn = connect_cn()
    print(f"✅ 国服：{cn.get_full_name()}")

    # 先把上次「说传了但没核实」的条目核一遍，没进库的剔掉，下面会被重新处理
    v_ok, v_gone = verify_pending(cn, load_state())
    if v_ok or v_gone:
        print(f"   上次遗留核对：确认 {v_ok} 条、剔除 {v_gone} 条（剔除的会重传）")

    idx = list_cn_index(cn, start, end)
    print(f"   国服同窗口已有 {len(idx)} 条\n")

    st = load_state()
    synced = st["synced"]

    todo = []
    for a in acts:
        aid = str(a.get("activityId"))
        if aid in synced or fp(a) in idx:
            continue
        todo.append(a)
    print(f"→ 需处理 {len(todo)} 条\n")
    if not todo:
        print("✅ 没有新活动，收工。")
        # joined 必须显式写 0：工作流据此**不发**成功通知（没内容就别打扰）
        _gh_output(uploaded=0, dup=0, failed=0, total=len(synced))
        return 0
    if max_items:
        todo = todo[:max_items]
        print(f"（--max 限制本次只处理前 {len(todo)} 条）\n")

    os.makedirs(FIT_CACHE, exist_ok=True)
    ok = dup = fail = 0
    failures = []
    ok_items = []          # 本次真正上传成功的条目，用于拼通知文案

    for n, a in enumerate(todo, 1):
        aid = str(a.get("activityId"))
        tag = f"[{n}/{len(todo)}]"
        print(f"{tag} {fmt_act(a)}")
        if dry_run:
            print("        （dry-run 跳过实际上传）")
            continue
        tmpdir = tempfile.mkdtemp(prefix="cnfit_", dir=FIT_CACHE)
        try:
            path = download_fit(intl, aid, tmpdir)
            size = os.path.getsize(path)
            if size > 50 * 1024 * 1024:
                raise RuntimeError(f"FIT {size/1048576:.1f}MB 超过国服 50MB 上限")
            result = cn.import_activity(path)
            succ, fails = parse_import_result(result)
            if fails and not succ:
                raise RuntimeError(f"国服拒绝：{json.dumps(fails, ensure_ascii=False)[:200]}")
            if not succ and not fails:
                print("        ⚠️ 国服响应无明确结论 —— 先按已上传记，收尾时统一回查")
            print(f"        ✅ 已上传 {size/1024:.0f}KB")
            ok += 1
            ok_items.append({
                # 通知里只留「月-日 时:分」，年份是噪音
                "when": str(a.get("startTimeLocal") or a.get("startTimeGMT") or "?")[5:16],
                "km": round((a.get("distance") or 0) / 1000.0, 1),
                "name": (a.get("activityName") or "").strip(),
            })
            synced[aid] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "start": str(a.get("startTimeGMT") or ""),
                "km": round((a.get("distance") or 0) / 1000.0, 2),
                "name": a.get("activityName") or "",
                "fp": list(fp(a)),
                "verified": False,      # 收尾时用指纹回国服核对，见 verify_pending()
            }
        except Exception as e:
            if _is_duplicate_error(e):
                print("        = 国服已存在，按成功记（下次不会再试）")
                dup += 1
                synced[aid] = {"at": datetime.now().isoformat(timespec="seconds"),
                               "note": "duplicate-409",
                               "start": str(a.get("startTimeGMT") or ""),
                               "fp": list(fp(a)),
                               "verified": True}   # 409 本身就是国服背书的「已存在」
            else:
                print(f"        ❌ {type(e).__name__}: {e}")
                fail += 1
                failures.append((aid, fmt_act(a), f"{type(e).__name__}: {e}"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            # ⚠️ 不在这里把指纹写进 idx：失败的活动不该被当成「已处理」，
            # 否则同一批次里会被静默跳过。成功/重复才写（上面各分支已写 synced）。
            save_state(st)              # 每传完一条就落盘，中断也不丢进度

        if n % BATCH_PAUSE_EVERY == 0 and n < len(todo):
            print(f"        … 批次休息 {BATCH_PAUSE_SEC:.0f}s（降低风控风险）")
            time.sleep(BATCH_PAUSE_SEC)
        elif n < len(todo):
            time.sleep(THROTTLE_SEC + random.uniform(0, 0.8))

    st["stats"] = {
        "total_synced": len([k for k, v in synced.items() if v.get("note") != "duplicate-409"]),
        "last_batch": {"ok": ok, "dup": dup, "fail": fail},
    }
    save_state(st)

    print(f"\n{'─'*52}\n完成：成功 {ok} · 已存在 {dup} · 失败 {fail}")

    # 收尾回查：`import_activity` 的返回值不可信（见 verify_pending 的说明）。
    # 刚传的活动可能还在国服入库队列里，查不到的只是被剔除，不报错 —— 下次再核。
    if ok:
        print("→ 回查国服是否真的收下…（刚上传的可能有延迟）")
        time.sleep(5)
        v_ok, v_gone = verify_pending(cn, st)
        print(f"   确认 {v_ok} 条已在国服" + (f"，{v_gone} 条暂未查到（下次运行会重试）" if v_gone else ""))

    print(f"累计已同步 {st['stats']['total_synced']} 条 → {STATE_FILE}")

    # 成功通知的原料：工作流靠 uploaded 决定发不发（0 条不值得每天打扰）
    _gh_output(uploaded=ok, dup=dup, failed=fail,
               total=st["stats"]["total_synced"])
    if summary_out:
        write_summary(summary_out, ok_items, st["stats"]["total_synced"], dup, fail)
        print(f"（通知摘要已写到 {summary_out}）")

    if failures:
        print("\n失败清单（下次重跑会自动重试）：")
        for aid, desc, err in failures:
            print(f"  {aid}  {desc}\n      {err}")
        return 2
    return 0


def cmd_verify() -> int:
    """全量核对：状态文件里记的每一条，国服到底有没有。

    用途：
    - 回填老记录（早期版本没存指纹，`verified` 字段缺失）；
    - 状态文件被删/换机器后重建信任；
    - 怀疑「说传了其实没进库」时排查。
    """
    st = load_state()
    synced = st.get("synced") or {}
    if not synced:
        print("状态文件是空的，没什么可核的。")
        return 0

    days = sorted(str(v.get("start") or "")[:10] for v in synced.values()
                  if str(v.get("start") or "")[:10])
    if not days:
        print("⚠️ 状态里没有可用的开始时间，无法核对。")
        return 1
    start, end = days[0], days[-1]
    print(f"核对窗口：{start} → {end}（依据状态里记录的开始日期）\n")

    intl = connect_intl()
    cn = connect_cn()
    i_acts = intl.get_activities_by_date(start, end) or []
    byid = {str(a.get("activityId")): a for a in i_acts}
    idx = list_cn_index(cn, start, end)
    print(f"国际服 {len(i_acts)} 条 | 国服同窗口指纹 {len(idx)} 个\n")

    ok = gone = orphan = 0
    for aid, v in list(synced.items()):
        a = byid.get(aid)
        if a is None:
            # 国际服已经查不到这条（活动被删？窗口太窄？）—— 不动它，只报告
            orphan += 1
            print(f"  ？ {aid}  国际服查不到，跳过（{str(v.get('name',''))[:30]}）")
            continue
        v["fp"] = list(fp(a))
        v["start"] = v.get("start") or str(a.get("startTimeGMT") or "")
        if tuple(v["fp"]) in idx:
            v["verified"] = True
            ok += 1
            print(f"  ✅ {str(a.get('startTimeLocal') or '?')[:16]}  "
                  f"{(a.get('distance') or 0)/1000:6.1f}km  {str(a.get('activityName',''))[:26]}")
        else:
            v["verified"] = False
            gone += 1
            print(f"  ❌ {str(a.get('startTimeLocal') or '?')[:16]}  "
                  f"{(a.get('distance') or 0)/1000:6.1f}km  国服没有 → 下次 sync 会重传")
    save_state(st)
    print(f"\n确认 {ok} 条 · 缺失 {gone} 条 · 无法判定 {orphan} 条")
    return 0 if gone == 0 else 3


def _gh_output(**kw) -> None:
    """把结果写进 GitHub Actions 的 step output（本地跑时静默跳过）。

    工作流靠 `uploaded` 决定要不要发成功通知 —— 「0 条新活动」不值得每天打扰。
    """
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kw.items():
                f.write(f"{k}={v}\n")
    except OSError as e:
        print(f"   ⚠️ 写 GITHUB_OUTPUT 失败：{e}")


def write_summary(path: str, ok_items: list[dict], total: int,
                  dup: int, fail: int) -> None:
    """拼一段适合企业微信的短消息（markdown 子集：加粗 / 引用 / 换行）。"""
    head = f"**🚴 国服同步完成 —— 上传 {len(ok_items)} 条**"
    lines = [head, ""]
    show = ok_items[:8]
    for it in show:
        name = f" · {it['name']}" if it["name"] else ""
        lines.append(f"· {it['when']}  {it['km']}km{name}")
    if len(ok_items) > len(show):
        lines.append(f"· …另有 {len(ok_items) - len(show)} 条")
    lines.append("")
    tail = f"累计已同步 {total} 条"
    if dup:
        tail += f" · 国服已存在 {dup} 条"
    if fail:
        tail += f" · **失败 {fail} 条**（下次会自动重试）"
    lines.append(tail)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _remind_token_convergence() -> None:
    """本地跑完联网操作后提醒回写 secret。

    Garmin 的 refresh token **轮转只有一条链**：本地刷新一次，云端 secret 里
    那颗就作废了。云端的 access token 可能还没过期，所以不会立刻报错 ——
    等它过期或跑 keepalive 时才 401，而且隔了一天才暴露，极难归因。
    2026-10-20 实测撞上过一次（本地 11:28 刷新，云端仍在用旧 token）。
    """
    if os.getenv("GITHUB_ACTIONS") == "true":
        return                       # CI 里由工作流自己回写
    print("\n💡 如果刚才触发了 token 刷新，云端 secret 那颗已经作废了。"
          "\n   想把云端救回来，在项目根执行："
          "\n     gh secret set GARMIN_TOKENS_B64 "
          '--body "$(base64 < ~/.garminconnect/garmin_tokens.json | tr -d \'\\n\')"'
          "\n     gh secret set GARMIN_CN_TOKENS_B64 "
          '--body "$(base64 < ~/.garminconnect-cn/garmin_tokens.json | tr -d \'\\n\')"')


def cmd_status() -> int:
    st = load_state()
    synced = st.get("synced", {})
    real = {k: v for k, v in synced.items() if v.get("note") != "duplicate-409"}
    unverified = [k for k, v in real.items() if v.get("verified") is False]
    print(f"状态文件：{STATE_FILE}")
    print(f"已同步活动：{len(real)} 条（另有 {len(synced)-len(real)} 条记为「已存在」）")
    if unverified:
        print(f"⚠️ 未核实  ：{len(unverified)} 条（上传返回值不可信，跑 --verify 核对）")
    print(f"上次运行  ：{st.get('last_run')}")
    if st.get("stats"):
        print(f"统计      ：{json.dumps(st['stats'], ensure_ascii=False)}")
    if not synced:
        print("\n（还是空的 —— 先 --auth，再 --probe）")
        return 0
    items = sorted(real.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)[:10]
    print("\n最近 10 条（传的时间 | 活动开始时间 UTC | 距离 | 名称）：")
    for aid, v in items:
        print(f"  {v.get('at','?')[:16]}  {v.get('start','?')[:16]}  "
              f"{v.get('km','?')}km  {str(v.get('name',''))[:30]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Garmin 国际服 → 国服 活动单向复制",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="首次使用顺序：--auth → --probe → --sync")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--auth", action="store_true", help="交互登录国服并落 token")
    g.add_argument("--probe", action="store_true", help="只读体检：看清待传/会跳过什么")
    g.add_argument("--sync", action="store_true", help="执行同步")
    g.add_argument("--verify", action="store_true", help="全量核对状态文件里每一条是否真在国服")
    g.add_argument("--status", action="store_true", help="查看同步进度")

    ap.add_argument("--days", type=int, default=7, help="最近 N 天（默认 7）")
    ap.add_argument("--from", dest="date_from", help="起始日 YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="结束日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--only-cycling", action="store_true", help="只同步骑行")
    ap.add_argument("--dry-run", action="store_true", help="演练：下载但不上传")
    ap.add_argument("--max", type=int, dest="max_items", help="本次最多处理 N 条")
    ap.add_argument("--summary-out", metavar="PATH",
                    help="把本次结果写成一段适合发企业微信的摘要（供工作流发成功通知）")
    args = ap.parse_args()

    if args.auth:
        rc = cmd_auth()
        if rc == 0:
            _remind_token_convergence()
        return rc
    if args.status:
        return cmd_status()
    if args.verify:
        rc = cmd_verify()
        _remind_token_convergence()
        return rc

    # sync / probe 的窗口
    if args.date_from:
        start = date.fromisoformat(args.date_from)
        end = date.fromisoformat(args.date_to) if args.date_to else date.today()
    else:
        end = date.fromisoformat(args.date_to) if args.date_to else date.today()
        start = end - timedelta(days=args.days - 1)
    if start > end:
        sys.exit("❌ 起始日晚于结束日")
    s, e = start.isoformat(), end.isoformat()

    if args.probe:
        rc = cmd_probe(args.days if not args.date_from else (end - start).days + 1,
                       args.only_cycling)
    else:
        rc = cmd_sync(s, e, args.only_cycling, args.dry_run, args.max_items,
                      args.summary_out)
    _remind_token_convergence()
    return rc


if __name__ == "__main__":
    sys.exit(main())

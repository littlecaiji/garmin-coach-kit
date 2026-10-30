#!/usr/bin/env python3
"""跨周教练状态 —— 让分析与排课真正「看完成情况」而不是每次重新套模板。

在设计上这是整套系统的记忆体：

    analyze_training.py  →  读上周完成度 → advance() → 写回 state → 生成报告
    push_plan_to_connect.py →  读 state → 生成课表（结构/负重/时长全部来自 state）

因此**唯一真相源是 state 文件**，不再有硬编码的负重、组数、瓦数或周 TSS。
state 落在 state/training_state.json 并随报告一起提交回仓库，所以每一周的
决策都能被下一周读到，也能在 git 历史里复盘「当时为什么这么排」。

规则引擎是**确定性的**：同样的输入必然得到同样的输出，可以离线单测
（见 work/_test_coach_state.py）。大模型只负责把决策翻译成人话，
不参与任何数值决策 —— 详见 coach_llm.py 顶部的说明。
"""
import copy
import json
import os
import re
from datetime import date as _date
from datetime import datetime

# ── 数据目录（state 属于个人健康数据，与代码物理分离）─────────────────────
# 解析顺序（第一条命中即用）：
#   1. DATA_DIR 环境变量            —— CI 与显式调用，最权威
#   2. <代码目录>/data/state        —— 代码仓库里的子目录布局（CI/新仓库）
#   3. <代码目录>/../data/state     —— 本地开发布局（代码在 cloud/，数据在 ../data）
#   4. <代码目录>/state             —— 旧布局兜底，找不到数据仓库时行为不变
# 规则：只在这里解析一次，其余模块一律 import STATE_PATH —— 别在别处再拼
#   "state/..."，否则两处路径会各说各话（历史上有过 201/204/214W 三方不一致）。
def _resolve_data_root() -> str:
    code_dir = os.path.dirname(os.path.abspath(__file__))
    env = (os.getenv("DATA_DIR") or "").strip()
    if env:
        return os.path.abspath(env)
    for cand in (os.path.join(code_dir, "data"),
                 os.path.join(code_dir, os.pardir, "data")):
        if os.path.isdir(os.path.join(cand, "state")):
            return os.path.abspath(cand)
    return code_dir


DATA_DIR = _resolve_data_root()
STATE_DIR = os.path.join(DATA_DIR, "state")
STATE_PATH = os.path.join(STATE_DIR, "training_state.json")
# 力量补录日志 —— 见本文件「力量训练手动补录」一节
STRENGTH_LOG_PATH = os.path.join(STATE_DIR, "strength_log.md")

SCHEMA_VERSION = 1

# ── 负重的递进规则（kg）────────────────────────────────────────────────────
# 只有「连续两周按计划完成」才加，步长刻意保守 —— 力量是这个用户最大的短板，
# 也是反复断档的项目，宁可加得慢也不要一次加崩。
WEIGHT_RULES = {
    "bridge":      {"inc": 2.5, "max": 60.0, "label": "臀桥"},
    "rdl":         {"inc": 2.5, "max": 60.0, "label": "罗马尼亚硬拉"},
    "split_squat": {"inc": 1.0, "max": 20.0, "label": "保加利亚分腿蹲（每手）"},
    "hipflex":     {"inc": 0.5, "max": 6.0,  "label": "髂腰肌离心（踝沙袋）"},
    "sl_bridge":   {"inc": 2.5, "max": 25.0, "label": "单腿臀桥"},
    "sl_rdl":      {"inc": 1.0, "max": 16.0, "label": "单腿硬拉"},
}

# ── 质量课的递进阶梯 (组数, 每组分钟) ───────────────────────────────────────
# 阈值走「先加时长再加组数」，VO2max 只加组数（单组 3min 已接近 Z5 耐受上限）。
QUALITY_LADDERS = {
    "threshold": [(3, 10), (3, 12), (4, 10), (4, 12)],
    "vo2":       [(5, 3), (6, 3), (7, 3)],
}

# 长骑时长按目标阶段封顶（分钟）。基准值只随「真的完成了长骑」向上爬。
PHASE_LONG_CAP = {"base": 180, "build": 240, "peak": 300, "taper": 150, "post": 150}

# ── 力量课的三种档位 ──────────────────────────────────────────────────────
# 每个档位包含「一周要几次」和「用哪套动作表」两件事：
#   full    → 2 次/周，A（主）+B（辅）—— 教练理想形态
#   single  → 1 次/周，只做 A（主）—— 现实频率，A 含臀桥/分腿蹲/髂腰肌离心，
#             是 fitting 结论（臀大肌+髂腰肌+核心）的完整落点
#   minimal → 1 次/周，只做 B 精简版（核心+髋外展+单腿）—— 真断档后的习惯重建
# ⚠️ 从 single 不会自动升到 full：那需要「一周真的做满 2 次」这个事实，
# 由人自己决定（改 state 里的 mode），不能让日历替身体做主。
STRENGTH_TARGET = {"full": 2, "single": 1, "minimal": 1}

# 连续多少周「0 次」才算真的断档。1 周空缺可能只是漏记（这个用户是自练、
# 不打卡），2 周才是真的停了 —— 降级与专项准入门槛共用这个阈值。
STRENGTH_BREAK_WEEKS = 2

PHASE_LABEL = {
    "base": "基础铺垫期", "build": "专项建设期", "peak": "专项巅峰期",
    "taper": "减量期", "post": "目标已完成",
}

# 周骑行 TSS 上限：平时 330，进入 11 月挑战专项期放宽到 450
TSS_CAP_DEFAULT = 330
TSS_CAP_CHALLENGE = 450

DEFAULT_STATE = {
    "version": SCHEMA_VERSION,
    "updated": None,
    "ftp": {
        # current 是**实际用于计算功率区间的值**，必须由你自己校准后填。
        # 200W 只是占位（开源版本不再带任何个人校准值）。
        # estimate 是脚本每次从功率数据估出来的值，只做记录与偏差提示，
        # 不会自动覆盖 current（报告估算系统性偏高约 5W）。
        "current": 200.0,
        "estimate": None,
        "history": [],
    },
    "strength": {
        "weights": {
            "bridge": 20.0, "rdl": 20.0, "split_squat": 8.0,
            "hipflex": 2.0, "sl_bridge": 10.0, "sl_rdl": 8.0,
        },
        "consecutive_missed": 0,
        "consecutive_done": 0,
        # full = 力量A+B 两次；single = 单次主课力量A；minimal = 断档后只做力量B精简版
        # 次数的唯一来源是 mode（见 STRENGTH_TARGET），不另设字段以免两处漂移。
        "mode": "full",
    },
    "quality": {
        "threshold": {"sets": 3, "reps": 10, "consecutive_ok": 0},
        "vo2":       {"sets": 5, "reps": 3, "consecutive_ok": 0},
    },
    "endurance": {
        # 周六长骑的当前基准时长；由「是否真的骑完」驱动增长
        "long_ride_min": 135,
        "consecutive_long_ok": 0,
        # 最近一次长骑的实际时长 —— 爬坡专项的准入门槛（≥90min 才允许上专项）
        "last_long_min": 0,
    },
    "goal": {
        # 首次运行前请改成你自己的目标：赛历由 goal.date 驱动
        # （base / build / peak / taper 全部由「离目标还有几周」算出来）。
        "name": "示例：单日 200km",
        "date": "2099-01-01",
        "phase": "base",
    },
    # last_advanced_week 是「同一周只推进一次」的幂等标记（ISO 年-周）。
    # 没有它，同周重复运行会把档位/负重反复顶上去 —— 见 advance() 里的说明。
    "last_advanced_week": None,
    "decisions": [],
}


# ═══════════════════════════════════════════════════════════════════════════
#  读写
# ═══════════════════════════════════════════════════════════════════════════

def _deep_merge(base, override):
    """把已存盘的 state 合并进默认结构 —— 新增字段自动获得默认值，
    旧文件不会因为缺字段而炸掉（向前兼容）。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(path=STATE_PATH):
    """读取 state；文件不存在时返回默认值（首次运行的冷启动路径）。"""
    try:
        with open(path) as f:
            return _deep_merge(DEFAULT_STATE, json.load(f))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT_STATE)
    except Exception as e:
        print(f"⚠️ 状态文件读取失败，回退到默认值: {e}")
        return copy.deepcopy(DEFAULT_STATE)


def save(state, path=STATE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ═══════════════════════════════════════════════════════════════════════════
#  力量训练手动补录（2026-10-12）
#
#  为什么需要它：Garmin 里只有 `strength_training` 类型的活动才算力量，而这个
#  用户的力量课是自练的、不上表。结果系统连续 9 周读到「0 次」，把「没记录」
#  当成了「没在做」——由此衍生出 mode=minimal、专项负荷被 readiness_gaps()
#  一票否决，而这些结论的前提本身是假的（实测 80 天里只有 2 条力量活动）。
#
#  设计取向：**一行一次课，写完就算数**。行首是日期，后面随便写，不做任何字段
#  解析。代价是必须容忍各种写法，收益是人真的会去写 —— 补录工具的成败全在这里。
#  真的无法识别日期的行会被收进 warnings 并在报告里说出来，绝不静默丢弃：
#  写了却不算数，人只会以为系统坏了。
# ═══════════════════════════════════════════════════════════════════════════

# 行首日期的可接受写法（按顺序尝试，第一个匹配者胜出）
_LOG_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"), ("y", "m", "d")),
    (re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})(?!\d)"), ("y", "m", "d")),
    (re.compile(r"^(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})日?"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})-(\d{1,2})(?!\d)"), ("m", "d")),
    (re.compile(r"^(\d{1,2})/(\d{1,2})(?!\d)"), ("m", "d")),
    (re.compile(r"^(\d{1,2})月(\d{1,2})日?"), ("m", "d")),
]

_LOG_BULLET = re.compile(r"^[-*+>]\s*")
_LOG_BLOCK_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)


def _strip_block_comments(text):
    """去掉 `<!-- ... -->` 块，但**保留行数**。

    日志头部是一整块 HTML 注释（在 Markdown 里天然隐藏），那几十行如果漏进解析器
    会变成一堆「无法识别日期」的警告。而警告里带行号、是给人按图索骥用的，
    所以不能直接把块删掉 —— 换成等量的空行，行号才对得上原文。
    """
    return _LOG_BLOCK_COMMENT.sub(
        lambda m: "\n" * m.group(0).count("\n"), text or "")


def _log_line_date(line, today):
    """从一行里取出行首日期。取不到返回 (None, 剩余文本)。"""
    m = None
    for pat, order in _LOG_DATE_PATTERNS:
        m = pat.match(line)
        if m:
            break
    if not m:
        return None, line
    vals = dict(zip(order, (int(g) for g in m.groups())))
    y = vals.get("y")
    if y is None:
        # 只写了月日：跨年时按「未来的日期属于去年」推断
        y = today.year - 1 if vals["m"] > today.month else today.year
    try:
        return _date(y, vals["m"], vals["d"]).isoformat(), line[m.end():]
    except ValueError:
        return None, line


def parse_strength_log(text, today=None):
    """把补录日志解析成条目。返回 (entries, warnings)。

    entries: [{"date": "YYYY-MM-DD", "note": "...", "line": 行号}]，按日期升序。
    一行 = 一次课（同一天做 A+B 就写两行）。空行、`#` 行注释、`<!-- -->` 块注释
    （日志头部那一大段说明）都会跳过，块注释按原行数占位以保证行号准确。
    """
    today = today or datetime.now()
    entries, warnings, seen = [], [], {}
    for i, raw in enumerate(_strip_block_comments(text).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _LOG_BULLET.sub("", line).strip()
        if not line:
            continue
        d, note = _log_line_date(line, today)
        if not d:
            warnings.append(f"第 {i} 行没有可识别的日期，已跳过：{line[:40]}")
            continue
        note = note.strip().lstrip("｜|:：,，-— \t").strip()
        key = (d, note)
        if key in seen:
            # 同一行内容粘了两遍（复制粘贴最容易发生），按 1 次计
            warnings.append(f"第 {i} 行与第 {seen[key]} 行完全重复，只计 1 次")
            continue
        seen[key] = i
        entries.append({"date": d, "note": note, "line": i})
    entries.sort(key=lambda e: (e["date"], e["line"]))
    return entries, warnings


def read_strength_log(path=None, today=None):
    """读补录日志 → (entries, warnings)。文件不存在时返回空（还没开始记 ≠ 记错了）。"""
    path = path or STRENGTH_LOG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            return parse_strength_log(f.read(), today=today)
    except FileNotFoundError:
        return [], []
    except Exception as e:
        return [], [f"力量补录日志读取失败（按 0 次计）：{e}"]


def count_strength_sessions(entries, start, end):
    """[start, end] 闭区间内的补录条目数（日期一律 'YYYY-MM-DD' 字符串）。"""
    lo, hi = str(start)[:10], str(end)[:10]
    return len([e for e in entries if lo <= e["date"] <= hi])


# ═══════════════════════════════════════════════════════════════════════════
#  便捷取值（给排课脚本用，全部走 state，不再有第二份常量）
# ═══════════════════════════════════════════════════════════════════════════

def ftp_of(state):
    v = state.get("ftp", {}).get("current")
    if v:
        return float(v)
    v = state.get("ftp", {}).get("estimate")
    return float(v) if v else 0.0


def weights_of(state):
    return state["strength"]["weights"]


def quality_of(state, kind):
    """返回 (sets, reps)。"""
    q = state["quality"][kind]
    return int(q["sets"]), int(q["reps"])


# ═══════════════════════════════════════════════════════════════════════════
#  Phase 2：目标周期化
# ═══════════════════════════════════════════════════════════════════════════

def goal_phase(state, today=None):
    """按距目标日期的周数划分 base / build / peak / taper / post。

    分界点刻意留得比较宽（>7 周才开始 build），因为这个用户刚从 7 周零力量的
    断档里出来，直接跳进大负荷专项期只会重演「过山车」。
    """
    today = today or datetime.now()
    gd = state.get("goal", {}).get("date")
    if not gd:
        return "base"
    try:
        target = datetime.strptime(gd, "%Y-%m-%d")
    except ValueError:
        return "base"
    weeks = (target - today).days / 7.0
    if weeks < 0:
        return "post"
    if weeks > 7:
        return "base"
    if weeks > 4.5:
        return "build"
    if weeks > 2.5:
        return "peak"
    return "taper"


def planned_long_ride_min(state, phase):
    """本周六长骑的目标时长（分钟）——受阶段上限约束。"""
    base = int(state["endurance"]["long_ride_min"])
    cap = PHASE_LONG_CAP.get(phase, 180)
    return min(base, cap)


def strength_target(state):
    """本周要求完成的力量课次数（由 mode 决定，不另设字段）。"""
    return STRENGTH_TARGET.get(state["strength"].get("mode"), 1)


def readiness_gaps(state, tsb):
    """距目标已近、但基础存在缺口 —— 返回缺口描述列表（空 = 可以上专项）。

    专项负荷只对「基础还在维持」的人开放。日历进入 build/peak 不代表身体
    准备好了：一个断了 8 周力量、上周连长骑都没有的人，直接上 5-7km 坡重复
    只会重演「过山车」。所以三条准入条件：力量没有整体断档、最近有长骑、
    没有深度疲劳。

    ⚠️ 力量这条曾经看 `mode == "minimal"`，那是错的：mode 只回答「用哪套动作
    表」，一旦降级就再也不会自动回上去（恢复成 full 需要「一周做满 2 次」，而
    这个用户的现实频率是 1 次/周），于是专项被无限期锁死。真正该问的是
    「力量还在不在练」——那是 consecutive_missed 的事。
    """
    gaps = []
    missed = int(state["strength"].get("consecutive_missed") or 0)
    if missed >= STRENGTH_BREAK_WEEKS:
        gaps.append(f"力量已断档 {missed} 周（连续 {STRENGTH_BREAK_WEEKS} 周 0 次）")
    if float(state["endurance"].get("last_long_min") or 0) < 90:
        gaps.append("近期没有 ≥90min 长骑")
    if tsb <= -15:
        gaps.append(f"TSB {tsb:+.0f} 疲劳偏高")
    return gaps


# ══ 自主神经闸门（HRV / RHR）═══════════════════════════════════════════════════
# 为什么必须有它：TSB 完全由训练负荷（TSS）算出，**看不到自主神经状态**。
# 2026-10-12 真实踩到 —— HRV 夜均 46（Garmin 判 LOW，基线告警线 60）、静息心率
# 从 50 单调升到 59、且休息整周也不恢复，而报告仍在说「TSB +16，状态窗口打开，
# 建议上 VO2max」。**负荷低 ≠ 可以上强度**，这一条必须由 HRV/RHR 来管。
#
# 反向也成立：TSB 为负但 HRV 正常 = 正常训练疲劳，不必恐慌 —— 所以闸门只在
# 自主神经一侧异常时触发，不做「TSB 低就一起拦」的粗暴处理。
HRV_FALLBACK_LOW = 60.0     # 拿不到 Garmin baseline 时的兜底告警线
RHR_ELEVATION_BPM = 5.0     # 静息心率高于近期最低值多少算异常抬升
RHR_RISE_DAYS = 3           # 连续上升多少天算异常


def _rising_streak(values):
    """末尾连续上升的步数（values 按时间正序、已剔除空值）。"""
    if len(values) < 2:
        return 0
    n = 0
    for i in range(len(values) - 1, 0, -1):
        if values[i] > values[i - 1]:
            n += 1
        else:
            break
    return n


def autonomic_gate(hrv_last=None, hrv_status=None, hrv_baseline=None,
                   rhr_series=None):
    """判断「现在能不能上强度」。返回 {"blocked", "reasons", "rhr_rise"}。

    hrv_baseline 用 Garmin 自己给的 `baseline`（lowUpper / balancedLow /
    balancedUpper）—— 比自己拍一个阈值靠谱。rhr_series 为 [(date, rhr), ...]。
    """
    reasons = []
    base = hrv_baseline or {}
    low_upper = base.get("lowUpper") or HRV_FALLBACK_LOW
    bal_low = base.get("balancedLow") or low_upper
    status_low = bool(hrv_status) and str(hrv_status).upper() == "LOW"

    # HRV 三条判据互斥，按严重度取第一条 —— 不必把同一件事说三遍。
    if hrv_last is not None and hrv_last < low_upper:
        reasons.append(f"HRV 夜均 {hrv_last:.0f}，已跌破告警线 {low_upper:.0f}")
    elif status_low:
        reasons.append("Garmin HRV 状态 = LOW")
    elif hrv_last is not None and hrv_last < bal_low:
        reasons.append(f"HRV 夜均 {hrv_last:.0f}，低于平衡区下限 {bal_low:.0f}")

    vals = [v for _, v in (rhr_series or []) if v is not None]
    rise = _rising_streak(vals)
    if rise >= RHR_RISE_DAYS:
        reasons.append(f"静息心率连续 {rise} 天上升")
    elif vals and vals[-1] - min(vals) >= RHR_ELEVATION_BPM:
        reasons.append(f"静息心率 {vals[-1]:.0f}，比近期最低 {min(vals):.0f} "
                       f"高 {vals[-1] - min(vals):.0f} bpm")

    return {"blocked": bool(reasons), "reasons": reasons, "rhr_rise": rise}


def long_ride_variant(state, phase, tsb):
    """长骑是否插入爬坡专项。

    专项课形态来自挑战路线分析：5-7km@5% 坡重复 4-6 次、功率守在 Z2，而不是
    冲强度 —— 目标是「长时间爬坡下的有氧耐受 + 后半段还能输出」，不是把爬坡
    当间歇骑。准入条件见 readiness_gaps()。
    """
    if phase not in ("build", "peak"):
        return "z2"
    if readiness_gaps(state, tsb):
        return "z2"
    return "hills"


def tss_cap(state, phase):
    """本周骑行 TSS 上限。专项期（build/peak）放宽到 450，其余 330。"""
    return TSS_CAP_CHALLENGE if phase in ("build", "peak") else TSS_CAP_DEFAULT


# ═══════════════════════════════════════════════════════════════════════════
#  周 TSS 由结构算出（不再写死 95+80+45+85）
# ═══════════════════════════════════════════════════════════════════════════

def session_tss(steps):
    """由课的结构估算 TSS。

    steps: [(minutes, fraction_of_ftp), ...]
    TSS ≈ Σ 时长(h) × IF² × 100 —— 这是 TSS 的定义式，所以只要改组数/时长，
    周 TSS 会自动跟着变，不会再出现「文案写 95 而结构其实变了」的错位。
    """
    total = 0.0
    for minutes, pct in steps:
        total += (minutes / 60.0) * (pct ** 2) * 100.0
    return total


# ═══════════════════════════════════════════════════════════════════════════
#  规则引擎
# ═══════════════════════════════════════════════════════════════════════════

def _bump_weights(state, log):
    w = state["strength"]["weights"]
    for k, rule in WEIGHT_RULES.items():
        cur = float(w.get(k, 0) or 0)
        new = min(cur + rule["inc"], rule["max"])
        if new > cur:
            w[k] = new
            log.append(f"🏋️ {rule['label']} 负重 {cur:g}kg → **{new:g}kg**"
                       f"（连续两周按计划完成力量课）")
        elif new == cur == rule["max"]:
            log.append(f"🏋️ {rule['label']} 已到上限 {cur:g}kg，维持")


def _ladder_step(ladder, cur_sets, cur_reps, direction):
    """在阶梯上移动一格。direction=+1 前进，-1 后退。返回 (sets, reps) 或原值。"""
    try:
        idx = ladder.index((cur_sets, cur_reps))
    except ValueError:
        return cur_sets, cur_reps
    nidx = max(0, min(len(ladder) - 1, idx + direction))
    return ladder[nidx]


def _advance_quality(state, kind, sessions, log):
    """按上周该类质量课的实际完成度升降档。

    sessions: _analyze_intervals() 产出的、名字匹配该类型的会话列表
    """
    q = state["quality"][kind]
    label = "阈值课" if kind == "threshold" else "VO2max 课"
    ladder = QUALITY_LADDERS[kind]

    if not sessions:
        q["consecutive_ok"] = 0
        log.append(f"⚪ 上周没有 {label} 记录，进度档位保持 "
                   f"{q['sets']}×{q['reps']}（未完成不加量，也不倒扣）")
        return

    # 取最接近满组的一次会话作为判定依据
    best = max(sessions, key=lambda s: s["done"])
    full = best["done"] >= best["total"]

    if full:
        q["consecutive_ok"] += 1
        if q["consecutive_ok"] >= 2:
            ns, nr = _ladder_step(ladder, q["sets"], q["reps"], +1)
            if (ns, nr) != (q["sets"], q["reps"]):
                log.append(f"📈 {label}连续两周满组达标 → 升档 "
                           f"{q['sets']}×{q['reps']} → **{ns}×{nr}**")
                q["sets"], q["reps"] = ns, nr
            else:
                log.append(f"📈 {label}已在阶梯顶端 {q['sets']}×{q['reps']}，维持")
            q["consecutive_ok"] = 0
        else:
            log.append(f"✅ {label}满组达标 {best['done']}/{best['total']}，"
                       f"再稳一周即升档（当前 {q['sets']}×{q['reps']}）")
    else:
        q["consecutive_ok"] = 0
        blown = [r for r in best["reps"] if r["verdict"] == "blown"]
        if blown:
            ns, nr = _ladder_step(ladder, q["sets"], q["reps"], -1)
            if (ns, nr) != (q["sets"], q["reps"]):
                log.append(f"📉 {label}末组崩盘（{best['done']}/{best['total']}）→ 退档 "
                           f"{q['sets']}×{q['reps']} → **{ns}×{nr}**，先保住完成质量")
                q["sets"], q["reps"] = ns, nr
            else:
                log.append(f"📉 {label}末组崩盘（{best['done']}/{best['total']}），"
                           f"已在最低档 {q['sets']}×{q['reps']}，本周按此执行并加强散热")
        else:
            log.append(f"🟡 {label}完成 {best['done']}/{best['total']}，未满组 —— "
                       f"档位维持 {q['sets']}×{q['reps']}，先连续两周达标再谈加量")


def _advance_strength(state, sessions_done, log):
    """按上周实际完成的力量课次数升降档 / 递进负重。

    sessions_done 的语义是「真的做了几次」，来源有两个：Garmin 里
    `strength_training` 类型的活动 + `state/strength_log.md` 的手动补录
    （这个用户自练、不上表，详见「力量训练手动补录」一节）。

    目标次数由 mode 决定（STRENGTH_TARGET）：full=2，single/minimal=1。
    """
    s = state["strength"]
    mode = s.get("mode") or "full"
    target = STRENGTH_TARGET.get(mode, 1)
    label = {"full": "完整 A+B（2 次）", "single": "单次主课力量A",
             "minimal": "力量B 精简版单次"}.get(mode, mode)

    if sessions_done == 0:
        s["consecutive_missed"] += 1
        s["consecutive_done"] = 0
        missed = s["consecutive_missed"]
        if missed >= STRENGTH_BREAK_WEEKS and mode in ("full", "single"):
            # 逐级降：先减次数（full→single），再减内容（single→minimal）
            s["mode"] = "single" if mode == "full" else "minimal"
            nxt = {"full": "单次主课力量A（周五做，含臀桥/分腿蹲/髂腰肌）",
                   "single": "力量B 精简版单次（核心+髋外展+单腿）"}[mode]
            log.append(f"🔻 力量连续 **{missed} 周 0 次** → 本周降级为「{nxt}」"
                       f"—— 先把「每周做一次」这件事稳定下来再谈负荷")
        elif mode == "minimal":
            log.append(f"🔻 力量仍为 0 次（连续 {missed} 周），维持精简版单次"
                       f"—— 目标只有一个：先做一次")
        else:
            log.append(f"⚠️ 上周力量 0 次（连续 {missed} 周），本周仍按「{label}」安排"
                       f"—— 若其实做了只是没记录，补一行到 state/strength_log.md 即可")
    elif sessions_done >= target:
        s["consecutive_done"] += 1
        s["consecutive_missed"] = 0
        if s["consecutive_done"] >= 2:
            if mode == "minimal":
                s["mode"] = "single"
                log.append("🔺 力量连续两周完成 → 从精简版升回**单次主课力量A**"
                           "（臀桥 + 保加利亚分腿蹲 + 髂腰肌离心 + 核心）")
            else:
                _bump_weights(state, log)
            s["consecutive_done"] = 0
        else:
            tail = ("再稳一周即升回主课力量A" if mode == "minimal"
                    else f"再稳一周即加重（当前臀桥 {s['weights']['bridge']:g}kg）")
            log.append(f"✅ 上周力量 {sessions_done} 次按计划完成，{tail}")
    else:
        # 只有 full（目标 2 次）才可能落到这里：做是做了，但没做满
        s["consecutive_done"] = 0
        s["consecutive_missed"] = 0
        log.append(f"🟡 上周力量 {sessions_done}/{target} 次，负重维持不变，"
                   f"先做到 {target} 次再谈递进")


def _advance_endurance(state, long_km, long_min, planned_min, log):
    """长骑时长只随「真的骑完」向上爬，避免计划先涨上去人跟不上。"""
    e = state["endurance"]
    e["last_long_min"] = round(float(long_min), 1)
    if long_min <= 0:
        log.append("⚪ 上周没有长骑记录 —— 长骑基准维持 "
                   f"{e['long_ride_min']}min（周末长距离是核心课，本周优先补回）")
        return
    ratio = long_min / planned_min if planned_min else 0
    if ratio >= 0.9:
        e["consecutive_long_ok"] += 1
        if e["consecutive_long_ok"] >= 2 and e["long_ride_min"] < 240:
            old = e["long_ride_min"]
            e["long_ride_min"] = min(old + 30, 240)
            e["consecutive_long_ok"] = 0
            log.append(f"📈 长骑连续两周达标 → 基准 {old}min → "
                       f"**{e['long_ride_min']}min**（{long_km:.0f}km 起步，先加时长不加强度）")
        else:
            log.append(f"✅ 上周长骑 {long_min:.0f}min/{long_km:.0f}km 达标，"
                       f"再稳一周把基准从 {e['long_ride_min']}min 上调")
    else:
        e["consecutive_long_ok"] = 0
        log.append(f"🟡 上周长骑 {long_min:.0f}min（计划 {planned_min}min，"
                   f"完成率 {ratio*100:.0f}%）未达标 —— 基准维持 "
                   f"{e['long_ride_min']}min，先稳定完成再加量")


def _advance_ftp(state, estimate, log):
    """FTP 只记录不自作主张上调 —— 报告估算系统性偏高约 5W，上调会让所有
    功率区间跟着虚高，把 Z2 骑成节奏课。偏差大时给提示，由人决定。"""
    f = state["ftp"]
    f["estimate"] = round(estimate, 1) if estimate else None
    cur = f.get("current")
    if not cur:
        f["current"] = round(estimate, 1)
        f["history"].append({"date": datetime.now().strftime("%Y-%m-%d"),
                             "value": round(estimate, 1), "source": "首次采用估算值"})
        log.append(f"📊 首次采用估算 FTP **{estimate:.0f}W** 作为区间基准")
        return
    diff = estimate - cur
    if abs(diff) >= 5:
        arrow = "高于" if diff > 0 else "低于"
        log.append(f"📊 报告估算 FTP {estimate:.0f}W，{arrow}你校准的 {cur:.0f}W "
                   f"{abs(diff):.0f}W —— **不自动调整**（估算偏高属已知偏差）；"
                   f"若连续多周同向偏离再手动改 state 里的 ftp.current")


def week_key(dt):
    """ISO 年-周（如 2026-W42）—— 用于「同一周只推进一次」的幂等保护。"""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def advance(state, compliance, tsb, today=None, gate=None):
    """本周的主入口：吃状态 + 上周完成度，吐出「下周该练什么」+ 决策日志。

    返回值 (new_state, decisions) 里的 decisions 会直接进报告，让每一处
    调整都有可追溯的理由 —— 这是「动态」和「随机」的分界线。

    **幂等性**：同一 ISO 周内第二次调用不会再推进档位/负重/长骑基准。

    gate 是自主神经闸门的结果（`autonomic_gate()`）。它**不改档位推进**（那是负荷
    视角的事），但在两处必须生效，否则报告会跟自己打架：

    1. 决策日志的措辞 —— 那句「TSB 状态窗口打开、适合推进质量课」在闸门触发时是
       明确错误的，必须改口；否则同一份报告里课表说「取消全部强度课」、日志说
       「适合推进质量课档位」。
    2. 长骑形态 —— 必须在这里就退回 "z2" 并写进 state，因为
       `push_plan_to_connect.py` 读的是 state 里的 `long_ride_variant`。只在报告
       渲染时兜底，会变成「报告写纯 Z2、手表推爬坡专项」。
    """
    gate = gate or {}
    today = today or datetime.now()
    week = week_key(today)
    state = copy.deepcopy(state)
    log = []

    # ── 闸门结论必须落进 state ────────────────────────────────────────────
    # 2026-10-19 实测踩到：之前只把「长骑退回 z2」写进 state，周一阈值课 /
    # 周三 VO2max 仍按 state.quality 被推上手表 —— 报告写「全 Z2」、手表却是
    # 强度课，两边分叉。原因就是 push_plan_to_connect.py 没有 TSB/HRV，
    # **只能读 state**，而 state 里压根没有「闸门触发」这个事实。
    # 因此在这里显式记录（含周号，供推送脚本判断新鲜度）。
    state["gate"] = {
        "blocked": bool(gate.get("blocked")),
        "week": week,
        "reasons": list(gate.get("reasons") or []),
    }

    # ── 幂等保护：同一周只推进一次 ────────────────────────────────────────
    # workflow 里「生成周报」是**无条件执行**的（只有归档/提交/推课表/发微信
    # 被 push 事件挡住），而「补跑」对使用者来说是正常操作。没有这层保护时，
    # 同一份「上周完成度」会被反复消费：实测同一周跑 4 次就把阈值课从 3×10
    # 顶到 4×10、VO2max 从 5×3 顶到 7×3、臀桥从 20kg 加到 25kg、长骑基准从
    # 135min 涨到 195min —— 全程零训练。这正是「负荷过山车」的生成机制，
    # 也是「无依据加量」最隐蔽的一种形式。
    if state.get("last_advanced_week") == week:
        log.append(f"🔁 本周（{week}）已经推进过一次档位 —— 判定为同周重复运行"
                   f"（补跑 / 重试）。**档位、负重、长骑基准一律保持不变**，"
                   f"本次只刷新阶段与完成度快照")
    else:
        _advance_ftp(state, compliance.get("ftp_estimate") or 0, log)
        _advance_strength(state, int(compliance.get("strength_sessions") or 0), log)

        q = compliance.get("quality") or {}
        _advance_quality(state, "threshold", q.get("threshold") or [], log)
        _advance_quality(state, "vo2", q.get("vo2") or [], log)

        _advance_endurance(
            state,
            float(compliance.get("long_ride_km") or 0),
            float(compliance.get("long_ride_min") or 0),
            float(compliance.get("planned_long_ride_min") or 0),
            log,
        )
        state["last_advanced_week"] = week

    # ── 阶段判定 + 疲劳封顶 ────────────────────────────────────────────────
    phase = goal_phase(state, today)
    if phase != state["goal"].get("phase"):
        state["goal"]["phase"] = phase
        label = PHASE_LABEL.get(phase, phase)
        gd = state["goal"].get("date", "")
        log.append(f"🎯 距「{state['goal']['name']}」({gd}) 进入**{label}**")

    # 日历进了专项期 ≠ 身体准备好了。把缺口明确说出来，而不是假装计划合理 ——
    # 这份报告的价值就在于不替使用者圆场。
    if phase in ("build", "peak"):
        gaps = readiness_gaps(state, tsb)
        if gaps:
            log.append(f"⚠️ 目标已进入{PHASE_LABEL.get(phase, phase)}，但「{'；'.join(gaps)}」"
                       f" —— 专项负荷暂不叠加，本周仍按基础周推进，先补齐缺口再谈专项")

    cap = tss_cap(state, phase)
    if tsb <= -10:
        log.append(f"🛑 TSB={tsb:+.0f} 疲劳累积 → 本周骑行 TSS 封顶 **{cap}**，"
                   f"先稳住再谈加量")
    elif tsb >= 15:
        if gate.get("blocked"):
            log.append(f"🔴 TSB={tsb:+.0f} 状态窗口本已打开，但自主神经闸门触发 → "
                       f"**本周取消全部强度课**"
                       f"（{'；'.join(gate.get('reasons') or [])}）")
        else:
            log.append(f"⚡ TSB={tsb:+.0f} 状态窗口打开 → 可按上限 {cap} 安排，"
                       f"适合推进质量课档位")

    # 长骑形态在这里定死并写入 state —— 报告与课表推送都读同一个值，
    # 不会出现「报告说加了爬坡专项、手表推的是纯 Z2」这种错位。
    #
    # ⚠️ 闸门必须在这里就生效，不能只靠 _write_weekly_plan 兜底：
    # 2026-10-12 实测踩到 —— 力量断档一解除、专项准入放行，advance() 就写了
    # variant="hills" 并记了一条「插入爬坡专项」，而报告因为闸门把课表渲染成纯
    # Z2，同时 push_plan_to_connect 读 state 里的 "hills" 会把爬坡专项推上手表。
    # 报告与课表就此分叉，而且日志里那句「插入爬坡专项」直接与课表矛盾。
    variant = long_ride_variant(state, phase, tsb)
    if gate.get("blocked") and variant != "z2":
        variant = "z2"
        log.append("🔴 自主神经闸门触发 → 周六长骑的**爬坡专项取消**，退回纯 Z2"
                   f"（{'；'.join(gate.get('reasons') or [])}）")
    elif variant == "hills" and state["goal"].get("long_ride_variant") != "hills":
        # 只在真的插入专项时记一笔；没插入的原因由上方的阶段行或缺口警告说明。
        log.append("🏔️ 基础达标 + 进入专项期 → 周六长骑插入**爬坡专项**"
                   "（5–7km/约5% 坡重复 4–6 次，上坡守 Z2 不冲）")
    state["goal"]["long_ride_variant"] = variant

    state["goal"]["phase"] = phase
    state["updated"] = today.strftime("%Y-%m-%d")
    state["decisions"] = log
    return state, log


def compliance_from(sessions_quality, strength_sessions, long_act, ftp_estimate):
    """把 analyze_training.py 手上的原始素材整理成 advance() 要的完成度结构。

    sessions_quality: _analyze_intervals() 的输出（全部质量课会话）
    long_act: 上周最长的一次骑行活动（dict）或 None
    """
    def pick(keyword):
        return [s for s in sessions_quality if keyword in (s.get("name") or "")]

    return {
        "quality": {
            "threshold": pick("阈值"),
            "vo2": pick("VO2"),
        },
        "strength_sessions": strength_sessions,
        "long_ride_min": (long_act.get("duration", 0) or 0) / 60.0 if long_act else 0,
        "long_ride_km": (long_act.get("distance", 0) or 0) / 1000.0 if long_act else 0,
        "ftp_estimate": ftp_estimate,
    }


def longest_ride(acts):
    """上周（或本周）骑行里距离最长的一次。"""
    rides = [a for a in acts
             if (a.get("distance") or 0) > 0
             and a.get("activityType", {}).get("typeKey", "") not in ("strength_training",)]
    return max(rides, key=lambda a: a.get("distance") or 0) if rides else None


# ═══════════════════════════════════════════════════════════════════════════
#  力量课动作表 —— 报告与课表推送的**唯一真相源**
#
#  此前动作、组数、负重在两处各写一份（报告文案里写死 20kg/8kg，推送脚本里
#  另有 W_* 常量），改一处不会同步另一处，还出现过报告写「侧平板 2×30s」
#  而手表推的是 3×30s 的错位。现在两边都从这里取。
#
#  ex = (category, exerciseName)，绑定 Garmin 官方动作库（手表显示示范动画）；
#  kg_key 指向 state["strength"]["weights"] 里的键。
# ═══════════════════════════════════════════════════════════════════════════

STRENGTH_A = [
    {"ex": ("HIP_RAISE", "HIP_RAISE"), "sets": 3, "reps": 10, "rest": 90,
     "kg_key": "bridge", "name": "臀桥",
     "cue": "臀桥：顶点夹臀停 1s，别用腰顶"},
    {"ex": ("DEADLIFT", "ROMANIAN_DEADLIFT"), "sets": 3, "reps": 8, "rest": 90,
     "kg_key": "rdl", "name": "罗马尼亚硬拉",
     "cue": "罗马尼亚硬拉：髋铰链，腰背中立，腘绳紧就减行程"},
    {"ex": ("LUNGE", "DUMBBELL_BULGARIAN_SPLIT_SQUAT"), "sets": 2, "reps": 10,
     "rest": 60, "kg_key": "split_squat", "name": "保加利亚分腿蹲（每腿）",
     "cue": "保加利亚分腿蹲：左腿先做、可多 1 组（补 48% 短板）"},
    {"ex": ("LEG_RAISE", "LYING_STRAIGHT_LEG_RAISE"), "sets": 3, "reps": 10,
     "rest": 60, "kg_key": "hipflex", "name": "髂腰肌离心",
     "cue": "髂腰肌离心：慢放 3s（练力量同时拉长髋屈肌）"},
    {"ex": ("PLANK", "PLANK"), "sets": 3, "secs": 45, "rest": 45, "name": "平板支撑",
     "cue": "平板支撑：收紧臀腹，别塌腰"},
    {"ex": ("HIP_STABILITY", "DEAD_BUG"), "sets": 3, "reps": 12, "rest": 45,
     "name": "死虫", "cue": "死虫：腰贴地，对侧手脚缓慢伸展"},
]

STRENGTH_B = [
    {"ex": ("PLANK", "SIDE_PLANK"), "sets": 3, "secs": 30, "rest": 45,
     "name": "侧平板", "cue": "侧平板：左右各 30s，臀中肌+核心"},
    {"ex": ("BANDED_EXERCISES", "CLAM_SHELLS"), "sets": 3, "reps": 15, "rest": 45,
     "name": "蚌式（弹力带）", "cue": "蚌式（弹力带）：髋外展，膝盖别外翻代偿"},
    {"ex": ("HIP_RAISE", "SINGLE_LEG_HIP_RAISE"), "sets": 2, "reps": 10, "rest": 60,
     "kg_key": "sl_bridge", "name": "单腿臀桥",
     "cue": "单腿臀桥：左腿先做，两侧都做"},
    {"ex": ("DEADLIFT", "SINGLE_LEG_ROMANIAN_DEADLIFT_WITH_DUMBBELL"),
     "sets": 2, "reps": 10, "rest": 60, "kg_key": "sl_rdl", "name": "单腿硬拉",
     "cue": "单腿硬拉：平衡+足弓稳定，慢放"},
    {"ex": ("HIP_STABILITY", "DEAD_BUG"), "sets": 3, "reps": 10, "rest": 45,
     "name": "死虫", "cue": "死虫：腰贴地，缓慢伸展"},
    {"ex": ("HIP_STABILITY", "QUADRUPED_WITH_LEG_LIFT"), "sets": 3, "reps": 12,
     "rest": 45, "name": "鸟狗", "cue": "鸟狗：四足位对侧手脚伸展，骨盆别晃"},
]

# 连续断档后的降级版：只保留核心 + 髋外展 + 单腿，2 组，目标只有一个 —— 先做一次
STRENGTH_B_MIN = [
    {"ex": ("PLANK", "SIDE_PLANK"), "sets": 2, "secs": 30, "rest": 45,
     "name": "侧平板", "cue": "侧平板：左右各 30s"},
    {"ex": ("BANDED_EXERCISES", "CLAM_SHELLS"), "sets": 2, "reps": 15, "rest": 45,
     "name": "蚌式（弹力带）", "cue": "蚌式：髋外展，慢速控制"},
    {"ex": ("HIP_RAISE", "SINGLE_LEG_HIP_RAISE"), "sets": 2, "reps": 10, "rest": 60,
     "kg_key": "sl_bridge", "name": "单腿臀桥", "cue": "单腿臀桥：左腿先做"},
    {"ex": ("HIP_STABILITY", "DEAD_BUG"), "sets": 2, "reps": 10, "rest": 45,
     "name": "死虫", "cue": "死虫：腰贴地，缓慢伸展"},
]


def _fill_kg(movements, weights):
    """把 kg_key 解析成当前负重。

    ⚠️ 没有负重的动作必须**完全不出现 kg 键** —— 推送脚本用
    ``if kg is not None`` 判断是否切换到 WEIGHTED_* 变体，自重动作若带上
    kg=0 会被误判成负重动作，动画就错了。
    """
    out = []
    for m in movements:
        m = dict(m)
        key = m.pop("kg_key", None)
        if key:
            m["kg"] = float(weights.get(key, 0))
        out.append(m)
    return out


def strength_plan(state):
    """本周实际要做的力量课。

    返回 {"mode", "target", "A", "B"}：
      * target = 本周要求的次数（1 或 2）
      * A/B = 各自的动作表；某套为空表示本周不做那一套
        - full    → A（周五主）+ B（周日辅）
        - single  → 只有 A（周五），周日改为休息/活动度
        - minimal → 只有 B 精简版（周日）
    调用方请以「A / B 是否为空」判断有没有那节课，不要再去比 mode 字符串 ——
    三种档位下 "== 'minimal'" 这种判断已经不够用了。
    """
    w = state["strength"]["weights"]
    mode = state["strength"].get("mode") or "full"
    if mode == "minimal":
        return {"mode": "minimal", "target": 1,
                "A": [], "B": _fill_kg(STRENGTH_B_MIN, w)}
    if mode == "single":
        return {"mode": "single", "target": 1,
                "A": _fill_kg(STRENGTH_A, w), "B": []}
    return {"mode": "full", "target": 2,
            "A": _fill_kg(STRENGTH_A, w), "B": _fill_kg(STRENGTH_B, w)}


def describe_movements(movements):
    """把动作列表渲染成一行可读文案，供报告复用（与手表推的完全一致）。"""
    parts = []
    for m in movements:
        label = m["name"]
        if "secs" in m:
            body = f"{m['sets']}×{m['secs']}s"
        else:
            body = f"{m['sets']}×{m['reps']}"
        kg = m.get("kg") or 0
        if kg:
            body += f"（**{kg:g}kg**）"
        parts.append(f"{label} {body}")
    return " + ".join(parts)


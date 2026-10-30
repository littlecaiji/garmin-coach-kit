#!/usr/bin/env python3
"""coach_state 规则引擎离线自测 —— 不连网、不依赖 Garmin。

构造几组假的「上周完成度」，验证升档 / 退档 / 降级 / 封顶逻辑符合教练意图。
跑法：python3 test_coach_state.py
"""
import copy
import datetime as _dtime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)   # 本文件与 coach_state.py 同级

import coach_state as cs  # noqa: E402

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def sess(name, done, total, verdicts=None):
    """伪造一条 _analyze_intervals 输出。"""
    verdicts = verdicts or (["ok"] * done + ["faded"] * (total - done))
    return {
        "name": name, "done": done, "total": total,
        "reps": [{"verdict": v, "power": 200, "hr": 160} for v in verdicts],
    }


def base():
    return copy.deepcopy(cs.DEFAULT_STATE)


_WEEK0 = _dtime.datetime(2026, 10, 12, 8, 45)   # 固定周一，保证结果可复现
_CLOCK = {"n": 0}


def run(compliance, tsb=0.0, state=None):
    """每次调用**前进一周**。

    advance() 现在按 ISO 周幂等（同一周重复运行不再推进档位/负重），所以模拟
    「连续多周」的测试必须把日期往前走。否则所有调用都落在同一周里、档位纹丝
    不动 —— 这批测试最初就是这么失败的，那不是引擎的问题，是测试的假设过时了。
    """
    _CLOCK["n"] += 1
    st = state or base()
    today = _WEEK0 + _dtime.timedelta(weeks=_CLOCK["n"] - 1)
    new, log = cs.advance(st, compliance, tsb, today=today)
    return new, log


print("── 1. 力量连续断档应逐级降档 ──")
# 阶梯是 full（A+B 两次）→ single（单次主课力量A）→ minimal（B 精简版）：
# 先减次数，再减内容。一次降到最轻反而会让人直接放弃。
st = base()
c = {"strength_sessions": 0, "quality": {}, "long_ride_km": 0, "long_ride_min": 0}
st, log = run(c, state=st)
check("第1周0次 mode", st["strength"]["mode"], "full")
check("第1周0次 missed", st["strength"]["consecutive_missed"], 1)
check("仅 1 周空缺不降级（可能只是漏记）", st["strength"]["mode"], "full")
st, log = run(c, state=st)
check("第2周0次 → 先减次数（single）", st["strength"]["mode"], "single")
check("降级日志点名力量A", any("单次主课力量A" in x for x in log), True)
st, log = run(c, state=st)
check("第3周0次 missed", st["strength"]["consecutive_missed"], 3)
check("第3周0次 → 再减内容（minimal）", st["strength"]["mode"], "minimal")
st, log = run(c, state=st)
check("第4周仍在 minimal，不越级", st["strength"]["mode"], "minimal")
check("降级后负重不变", st["strength"]["weights"]["bridge"], 20.0)

print("\n── 2. 力量连续两周完成应加重 ──")
c2 = {"strength_sessions": 2, "quality": {}, "long_ride_km": 0, "long_ride_min": 0}
st, log = run(c2, state=base())
check("1周完成 未加重", st["strength"]["weights"]["bridge"], 20.0)
st, log = run(c2, state=st)
check("2周完成 → 臀桥 22.5", st["strength"]["weights"]["bridge"], 22.5)
check("2周完成 → 硬拉 22.5", st["strength"]["weights"]["rdl"], 22.5)
check("2周完成 → 分腿蹲 9", st["strength"]["weights"]["split_squat"], 9.0)
check("2周完成 → 髂腰肌 2.5", st["strength"]["weights"]["hipflex"], 2.5)

print("\n── 3. 阈值课满组两周升档 ──")
c3 = {"strength_sessions": 2, "quality": {"threshold": [sess("阈值课 3×10min", 3, 3)]},
      "long_ride_km": 0, "long_ride_min": 0}
st, log = run(c3, state=base())
check("1周满组 不变", (st["quality"]["threshold"]["sets"], st["quality"]["threshold"]["reps"]), (3, 10))
st, log = run(c3, state=st)
check("2周满组 → 3×12", (st["quality"]["threshold"]["sets"], st["quality"]["threshold"]["reps"]), (3, 12))
# 升档后连续计数归零，所以下一档又要「连续两周」，第 3 周保持、第 4 周才再升
st, log = run(c3, state=st)
check("第3周保持 3×12", (st["quality"]["threshold"]["sets"], st["quality"]["threshold"]["reps"]), (3, 12))
st, log = run(c3, state=st)
check("第4周 → 4×10", (st["quality"]["threshold"]["sets"], st["quality"]["threshold"]["reps"]), (4, 10))

print("\n── 4. 末组崩盘应退档 ──")
st = base()
st["quality"]["threshold"] = {"sets": 4, "reps": 12, "consecutive_ok": 0}
c4 = {"strength_sessions": 2, "quality": {"threshold": [sess("阈值课", 3, 4, ["ok", "ok", "ok", "blown"])]},
      "long_ride_km": 0, "long_ride_min": 0}
st, log = run(c4, state=st)
check("崩盘 4×12 → 4×10", (st["quality"]["threshold"]["sets"], st["quality"]["threshold"]["reps"]), (4, 10))
check("退档日志存在", any("退档" in x for x in log), True)

print("\n── 5. 崩盘但已在最低档不倒扣 ──")
st = base()
c5 = {"strength_sessions": 2, "quality": {"threshold": [sess("阈值课", 1, 3, ["ok", "blown", "blown"])]},
      "long_ride_km": 0, "long_ride_min": 0}
st, log = run(c5, state=st)
check("最低档维持 3×10", (st["quality"]["threshold"]["sets"], st["quality"]["threshold"]["reps"]), (3, 10))

print("\n── 6. VO2max 只加组数 ──")
c6 = {"strength_sessions": 2, "quality": {"vo2": [sess("VO2max间歇", 5, 5)]},
      "long_ride_km": 0, "long_ride_min": 0}
st, log = run(c6, state=base())
st, log = run(c6, state=st)
check("VO2 升到 6×3", (st["quality"]["vo2"]["sets"], st["quality"]["vo2"]["reps"]), (6, 3))

print("\n── 7. 长骑时长只随完成度上爬 ──")
c7 = {"strength_sessions": 2, "quality": {}, "long_ride_km": 85, "long_ride_min": 140,
      "planned_long_ride_min": 135}
st, log = run(c7, state=base())
check("1周达标 基准不变", st["endurance"]["long_ride_min"], 135)
st, log = run(c7, state=st)
check("2周达标 → 165", st["endurance"]["long_ride_min"], 165)
check("未达标不涨", run({"strength_sessions": 2, "quality": {}, "long_ride_km": 20,
                        "long_ride_min": 40, "planned_long_ride_min": 165},
                       state=st)[0]["endurance"]["long_ride_min"], 165)

print("\n── 8. FTP 不被自动上调 ──")
st, log = run({"strength_sessions": 2, "quality": {}, "long_ride_km": 0,
               "long_ride_min": 0, "ftp_estimate": 225.0})
check("current 保持不被覆盖", st["ftp"]["current"], cs.DEFAULT_STATE["ftp"]["current"])
check("estimate 被记录", st["ftp"]["estimate"], 225.0)
check("给出偏差提示", any("不自动调整" in x for x in log), True)

print("\n── 9. TSB 封顶与状态窗口 ──")
_, log = run({"strength_sessions": 2, "quality": {}, "long_ride_km": 0, "long_ride_min": 0}, tsb=-20)
check("TSB-20 触发封顶提示", any("封顶" in x for x in log), True)
_, log = run({"strength_sessions": 2, "quality": {}, "long_ride_km": 0, "long_ride_min": 0}, tsb=20)
check("TSB+20 状态窗口", any("状态窗口" in x for x in log), True)

print("\n── 10. 阶段判定 ──")
from datetime import datetime, timedelta  # noqa: E402
today = datetime(2026, 10, 9)
for days, want in [(70, "base"), (45, "build"), (25, "peak"), (12, "taper"), (-1, "post")]:
    st = base()
    st["goal"]["date"] = (today + timedelta(days=days)).strftime("%Y-%m-%d")
    check(f"距目标 {days} 天", cs.goal_phase(st, today), want)

print("\n── 11. 长骑专项的准入条件 ──")
ready = base()
ready["endurance"]["last_long_min"] = 140
check("base 阶段", cs.long_ride_variant(base(), "base", 0), "z2")
check("build + 有长骑基础", cs.long_ride_variant(ready, "build", 0), "hills")
check("peak + 有长骑基础", cs.long_ride_variant(ready, "peak", 0), "hills")
check("peak 疲劳时", cs.long_ride_variant(ready, "peak", -20), "z2")
check("build 但无长骑基础 → 不上专项", cs.long_ride_variant(base(), "build", 0), "z2")
_nos = copy.deepcopy(ready)
_nos["strength"]["mode"] = "minimal"
_nos["strength"]["consecutive_missed"] = 4
check("力量断档 → 不上专项", cs.long_ride_variant(_nos, "peak", 0), "z2")
check("缺口：默认仅缺长骑", len(cs.readiness_gaps(base(), 0)), 1)
check("缺口：疲劳时两条", len(cs.readiness_gaps(base(), -20)), 2)
check("缺口：就绪时为空", len(cs.readiness_gaps(ready, 5)), 0)

print("\n── 12. 周 TSS 由结构算出 ──")
t = cs.session_tss([(10, 0.50), (30, 0.97), (15, 0.45)])
check("TSS 计算合理", 30 < t < 60, True)

print("\n── 13. 状态文件向前兼容 ──")
import json  # noqa: E402
import tempfile  # noqa: E402
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"ftp": {"current": 200.0}}, f)
    p = f.name
st = cs.load(p)
check("旧文件缺字段补默认", st["strength"]["weights"]["bridge"], 20.0)
check("旧文件保留已存值", st["ftp"]["current"], 200.0)
check("缺失字段补默认", st["endurance"]["long_ride_min"], 135)
os.unlink(p)

print("\n── 14. 同周重复运行必须幂等（补跑不能顶档位）──")
# 这是真实踩到的坑：workflow 里「生成周报」无条件执行，而补跑是正常操作。
# 没有幂等保护时，同一份「上周完成度」会被反复消费，档位和负重一路往上爬，
# 全程零训练 —— 正是「负荷过山车」的生成机制。
_repeat = {
    "quality": {"threshold": [sess("阈值课", 3, 3)],
                "vo2": [sess("VO2max间歇", 5, 5)]},
    "strength_sessions": 2,
    "long_ride_min": 150.0,
    "long_ride_km": 90.0,
    "planned_long_ride_min": 135.0,
    "ftp_estimate": 217.0,
}
_mon = _dtime.datetime(2026, 10, 12, 8, 45)   # ISO 2026-W42
_thu = _dtime.datetime(2026, 10, 15, 20, 0)   # 同属 W42（补跑）
_nxt = _dtime.datetime(2026, 10, 19, 8, 45)   # W43，跨周


def _snap(st):
    return (st["quality"]["threshold"]["sets"], st["quality"]["threshold"]["reps"],
            st["quality"]["vo2"]["sets"], st["quality"]["vo2"]["reps"],
            st["strength"]["weights"]["bridge"], st["endurance"]["long_ride_min"])


_st = base()
_st, _ = cs.advance(_st, _repeat, 18.0, today=_mon)
_wk1 = _snap(_st)

_st, _ = cs.advance(_st, _repeat, 18.0, today=_mon)     # 同一天重试
_st, _log_thu = cs.advance(_st, _repeat, 18.0, today=_thu)  # 同周补跑
check("同周重复运行不推进档位/负重", _snap(_st), _wk1)
check("同周补跑日志说明了原因",
      any("同周重复运行" in x for x in _log_thu), True)
check("同周重复运行不刷新周标记", _st["last_advanced_week"], cs.week_key(_mon))

_st, _ = cs.advance(_st, _repeat, 18.0, today=_nxt)     # 跨周后必须恢复推进
check("跨周后周标记更新", _st["last_advanced_week"], cs.week_key(_nxt))
check("跨周后累计正常推进（VO2 升到 6×3）", _st["quality"]["vo2"]["sets"], 6)

# 旧文件里没有 last_advanced_week 这个字段时，必须照常推进（向前兼容）
_st2 = base()
_st2.pop("last_advanced_week", None)
_st2, _ = cs.advance(_st2, _repeat, 18.0, today=_nxt)
check("旧文件无此字段仍能推进（向前兼容）",
      _st2["last_advanced_week"], cs.week_key(_nxt))

# ══ §15 自主神经闸门（HRV / RHR）═══════════════════════════════════════════
# 用例数字全部取自 2026-10-12 的真实数据 —— 那天报告给出「TSB +16 状态窗口打开，
# 建议上 VO2max」，而实际 HRV 已跌破告警线、静息心率升了 9 bpm。闸门必须拦住它。
_BASE_HRV = {"lowUpper": 60, "balancedLow": 63, "balancedUpper": 78}


def gate(hrv=None, status=None, rhr=None, base_hrv=None):
    return cs.autonomic_gate(
        hrv_last=hrv, hrv_status=status,
        hrv_baseline=_BASE_HRV if base_hrv is None else base_hrv,
        rhr_series=[(f"d{i}", v) for i, v in enumerate(rhr or [])])


_g = gate(46, "LOW", [50, 55, 53, 56, 57, 58, 58, 59])
check("闸门：10/12 真实数据必须触发", _g["blocked"], True)
check("闸门：真实数据给出 2 条理由", len(_g["reasons"]), 2)
check("闸门：理由点出 HRV 数值", any("HRV 夜均 46" in r for r in _g["reasons"]), True)
check("闸门：理由点出 RHR 抬升", any("高 9 bpm" in r for r in _g["reasons"]), True)

# 健康场景：绝不能误伤 —— 误拦会让用户平白丢掉训练周
check("闸门：HRV 正常 + RHR 平稳 → 放行",
      gate(70, "BALANCED", [52, 51, 53, 52, 51])["blocked"], False)

# 边界：用 < 而不是 <=，恰好压在平衡区下限上算通过
check("闸门：HRV 恰好等于平衡区下限 → 放行",
      gate(63, "BALANCED", [50, 50])["blocked"], False)
check("闸门：HRV 低于平衡区下限一档 → 拦",
      gate(62, "BALANCED", [50, 50])["blocked"], True)
check("闸门：只有 status=LOW、没有数值 → 也拦",
      gate(None, "LOW", [50, 50])["blocked"], True)

# RHR 两条判据：连续上升 / 相对近期最低值的抬升
check("闸门：RHR 连续上升 2 天 → 不拦（未到 3 天）",
      gate(70, "BALANCED", [50, 51, 52])["blocked"], False)
check("闸门：RHR 连续上升 3 天 → 拦",
      gate(70, "BALANCED", [50, 51, 52, 53])["blocked"], True)
check("闸门：RHR 抬高 5 bpm（非连续）→ 拦",
      gate(70, "BALANCED", [50, 55])["blocked"], True)
check("闸门：RHR 抬高 4 bpm → 不拦",
      gate(70, "BALANCED", [50, 54])["blocked"], False)

# 缺数据绝不能当成「异常」—— Garmin 抽风那天不该平白取消整周训练
check("闸门：全空数据 → 放行（缺数据 ≠ 异常）",
      gate(None, None, [])["blocked"], False)
check("闸门：拿不到 baseline 时回落到兜底告警线 60",
      gate(59, None, [50, 50], base_hrv={})["blocked"], True)

# _rising_streak 决定「连续上升」的判定，单独钉住语义
check("_rising_streak：单调升 3 步", cs._rising_streak([50, 51, 52, 53]), 3)
check("_rising_streak：末尾持平 → 0（当天没升）", cs._rising_streak([50, 51, 52, 52]), 0)
check("_rising_streak：末尾下降 → 0", cs._rising_streak([50, 51, 50]), 0)
check("_rising_streak：单点 → 0", cs._rising_streak([50]), 0)
check("_rising_streak：空 → 0", cs._rising_streak([]), 0)

# 决策日志的措辞也必须跟着闸门改口。10/12 云端实测踩到：课表已经改成「取消全部
# 强度课」，而同一份报告的「本周决策日志」里还留着「状态窗口打开 → 适合推进质量课
# 档位」—— 同一份报告自己跟自己打架。档位推进本身不受闸门影响，只有措辞要改。
_s3 = base()
_s3, _log_gated = cs.advance(_s3, _repeat, 18.0, today=_WEEK0,
                             gate={"blocked": True, "reasons": ["HRV 夜均 46 已跌破告警线 60"]})
check("闸门触发时决策日志改口（提到闸门）",
      any("自主神经闸门触发" in x for x in _log_gated), True)
check("闸门触发时不再说「适合推进质量课档位」",
      any("适合推进质量课档位" in x for x in _log_gated), False)

_s4 = base()
_s4, _log_plain = cs.advance(_s4, _repeat, 18.0, today=_WEEK0)
check("闸门未触发时保留原「状态窗口打开」说法",
      any("状态窗口打开" in x for x in _log_plain), True)

# ══ §16 力量补录日志（state/strength_log.md）═══════════════════════════════
# 存在的理由：Garmin 里只有 strength_training 活动才算力量，而用户是自练不上表，
# 于是系统连续 9 周读到 0 次，把「没记录」当成「没在做」。补录是人肉信号源，
# 所以解析必须宽容 —— 但**不能静默丢行**，写了却不算数比不写更伤人。
print("\n── 16. 力量补录日志解析 ──")
_LOG_TODAY = _dtime.datetime(2026, 10, 13, 9, 0)
_LOG_TEXT = """
<!-- 力量训练手动补录 -->
# 这一行是注释
2026-10-06  力量A 臀桥 3×10@20kg + 保加利亚蹲 2×10@8kg
2026/10/09  力量B
- 10/11 力量A（补做）
10月12日 力量B
12/25 去年的记录
这一行没有日期，应当被跳过
2026-10-06  力量A 臀桥 3×10@20kg + 保加利亚蹲 2×10@8kg
"""
_entries, _warns = cs.parse_strength_log(_LOG_TEXT, today=_LOG_TODAY)
check("补录：识别出 5 条", len(_entries), 5)
check("补录：ISO 日期", any(e["date"] == "2026-10-06" for e in _entries), True)
check("补录：斜杠日期", any(e["date"] == "2026-10-09" for e in _entries), True)
check("补录：项目符号 + 月/日", any(e["date"] == "2026-10-11" for e in _entries), True)
check("补录：中文月日", any(e["date"] == "2026-10-12" for e in _entries), True)
check("补录：月日早于当前月 → 归到去年",
      any(e["date"] == "2025-12-25" for e in _entries), True)
check("补录：备注保留原文", any("保加利亚蹲" in e["note"] for e in _entries), True)
check("补录：粘贴重复行只计 1 次",
      sum(1 for w in _warns if "完全重复" in w), 1)
check("补录：无日期的行进 warnings，不被静默吞掉",
      any("没有可识别的日期" in w for w in _warns), True)
check("补录：注释行不进 warnings",
      any("这一行是注释" in w for w in _warns), False)
check("补录：按周区间计数（10/5–10/11 共 3 条）",
      cs.count_strength_sessions(_entries, "2026-10-05", "2026-10-11"), 3)
check("补录：区间端点外的不计",
      cs.count_strength_sessions(_entries, "2026-10-07", "2026-10-07"), 0)
check("补录：文件不存在 = 空，不是错误",
      cs.read_strength_log("/nonexistent/strength_log.md"), ([], []))
# 块注释（日志头部那几十行说明）必须整块跳过，且保留行数以便警告里的行号对得上
_blk, _bw = cs.parse_strength_log(
    "<!--\n说明第一行\n说明第二行\n-->\n2026-10-11 力量A\n", today=_LOG_TODAY)
check("补录：块注释不产生假条目", [e["date"] for e in _blk], ["2026-10-11"])
check("补录：块注释不产生假警告", _bw, [])
check("补录：块注释后行号仍准确（正文在第 5 行）", _blk[0]["line"], 5)
# 真正随仓库发布的那个日志文件必须干净解析 —— 头部写坏了会每周报一堆假警告
_live, _lw = cs.read_strength_log()
check("补录：仓库里的真实日志无解析警告", _lw, [])
check("补录：真实日志条目都在合法的日期字符串上",
      all(len(e["date"]) == 10 and e["date"][4] == "-" for e in _live), True)

# ══ §17 力量「单次档位」的推进（这是用户的现实频率）═════════════════════════
# 这个用户实际就是每周 1 次（自练、不进 Garmin）。旧规则只在 sessions_done >= 2
# 时才认「完成」，于是 1 次/周永远被算成未达标 → mode 卡死在 minimal、负重永不推进、
# 专项被 readiness_gaps 永久否决。single 档位就是为这个频率准备的。
print("\n── 17. 力量单次档位（每周 1 次）的推进 ──")
_1x = {"strength_sessions": 1, "quality": {}, "long_ride_km": 0, "long_ride_min": 0}
_s5 = copy.deepcopy(cs.DEFAULT_STATE)
_s5["strength"]["mode"] = "single"
_s5, _l5 = run(_1x, state=_s5)
check("single + 1 次 → 计入连续完成", _s5["strength"]["consecutive_done"], 1)
check("single + 1 次 → 断档清零", _s5["strength"]["consecutive_missed"], 0)
check("single + 1 次 未加重", _s5["strength"]["weights"]["bridge"], 20.0)
_s5, _l5 = run(_1x, state=_s5)
check("single 连续两周 → 负重 +2.5", _s5["strength"]["weights"]["bridge"], 22.5)
check("single 满两周也不会自动跳到 A+B（要人自己定）",
      _s5["strength"]["mode"], "single")
_s5, _l5 = run(_1x, state=_s5)
check("加重后连续计数归零", _s5["strength"]["consecutive_done"], 1)
check("single + 0 次 → 只记断档，不降级", 
      run({"strength_sessions": 0, "quality": {}, "long_ride_km": 0, "long_ride_min": 0},
          state=_s5)[0]["strength"]["mode"], "single")

# ══ §18 专项准入门槛只看「还在不在练」，不看「用哪套动作表」══════════════════
# 真实处境：连续 9 周被判 0 次 → mode=minimal → readiness_gaps() 一票否决专项。
# 而 mode 一旦降级就再不会自动回上去（回 full 需要「一周做满 2 次」），专项被
# 无限期锁死。所以门槛改看 consecutive_missed。
print("\n── 18. 专项准入门槛与档位解耦 ──")
_fix = copy.deepcopy(cs.DEFAULT_STATE)
_fix["strength"]["mode"] = "single"
_fix["strength"]["consecutive_missed"] = 0
_fix["endurance"]["last_long_min"] = 140
check("补录后准入缺口为空", cs.readiness_gaps(_fix, 0), [])
check("补录后 peak 期能上爬坡专项", cs.long_ride_variant(_fix, "peak", 0), "hills")
_broken = copy.deepcopy(_fix)
_broken["strength"]["consecutive_missed"] = 9
check("断档真的存在时仍拦（阈值没失效）",
      any("断档" in g for g in cs.readiness_gaps(_broken, 0)), True)
_1miss = copy.deepcopy(_fix)
_1miss["strength"]["consecutive_missed"] = 1
check("只缺 1 周不算断档（可能是漏记）", cs.readiness_gaps(_1miss, 0), [])

# ══ §19 三档位的力量课表形状 ═════════════════════════════════════════════════
print("\n── 19. 三档位的力量课表形状 ──")
_fp = cs.strength_plan(copy.deepcopy(cs.DEFAULT_STATE))
check("full：A+B 都在，目标 2 次",
      (bool(_fp["A"]), bool(_fp["B"]), _fp["target"]), (True, True, 2))
_ss = copy.deepcopy(cs.DEFAULT_STATE)
_ss["strength"]["mode"] = "single"
_sp2 = cs.strength_plan(_ss)
check("single：只有主课 A，目标 1 次",
      (bool(_sp2["A"]), bool(_sp2["B"]), _sp2["target"]), (True, False, 1))
_mm = copy.deepcopy(cs.DEFAULT_STATE)
_mm["strength"]["mode"] = "minimal"
_mp2 = cs.strength_plan(_mm)
check("minimal：只有 B 精简版，目标 1 次",
      (bool(_mp2["A"]), bool(_mp2["B"]), _mp2["target"]), (False, True, 1))
check("single 的主课含保加利亚分腿蹲（用户实际在练的动作）",
      any("保加利亚" in m["name"] for m in _sp2["A"]), True)
check("single 的主课含髂腰肌离心（fitting 认定的短板）",
      any("髂腰肌" in m["name"] for m in _sp2["A"]), True)

# ══ §20 闸门必须拦住长骑专项 —— 而且要拦在 state 里 ═════════════════════════
# 2026-10-12 实测踩到：力量断档一解除、专项准入放行之后，advance() 立刻写了
# variant="hills" 并记了一条「插入爬坡专项」，而报告因为闸门把课表渲染成纯 Z2，
# 同时 push_plan_to_connect 读 state 里的 "hills" 会把爬坡专项推上手表 ——
# 报告与课表分叉，日志还跟课表自相矛盾。所以闸门必须在 advance() 里就生效。
print("\n── 20. 闸门触发时长骑专项必须退回 Z2 ──")
_gh = copy.deepcopy(cs.DEFAULT_STATE)
_gh["strength"]["consecutive_missed"] = 0
_gh["endurance"]["last_long_min"] = 140
# 阶段由 goal.date 算出来，不是写 goal.phase —— DEFAULT_STATE 里那个占位日期
# 是「远期」，会被判成 base 从而根本不进专项分支。这里显式给一个距 _WEEK0
# 三周的目标（2.5 < 3 ≤ 4.5 → peak），让测试不依赖任何个人数据。
_gh["goal"]["date"] = (_WEEK0 + _dtime.timedelta(days=21)).strftime("%Y-%m-%d")
_gh["goal"]["phase"] = "peak"
_gh["goal"]["long_ride_variant"] = "z2"
_gh_cmp = {"strength_sessions": 1, "quality": {}, "long_ride_km": 100,
           "long_ride_min": 150, "planned_long_ride_min": 135}

_gh_on, _lg_on = cs.advance(copy.deepcopy(_gh), _gh_cmp, 6.0, today=_WEEK0,
                            gate={"blocked": True, "reasons": ["HRV 夜均 46"]})
check("闸门触发 → state 里长骑退回 z2",
      _gh_on["goal"]["long_ride_variant"], "z2")
check("闸门触发 → 日志说明专项已取消",
      any("爬坡专项取消" in x for x in _lg_on), True)
check("闸门触发 → 日志不再说「插入爬坡专项」",
      any("插入**爬坡专项**" in x for x in _lg_on), False)

_gh_off, _lg_off = cs.advance(copy.deepcopy(_gh), _gh_cmp, 6.0, today=_WEEK0)
check("未触发时仍照常插入爬坡专项",
      _gh_off["goal"]["long_ride_variant"], "hills")
check("未触发时日志保留原文",
      any("插入**爬坡专项**" in x for x in _lg_off), True)

# 闸门结论必须落进 state —— push_plan_to_connect 没有 TSB/HRV，只能读 state。
# 10/19 实测：不写这一项时，报告渲染成全 Z2、手表仍推阈值课 + VO2max。
check("闸门触发 → state.gate.blocked 落盘",
      _gh_on.get("gate", {}).get("blocked"), True)
check("闸门触发 → state.gate 记录周号",
      _gh_on.get("gate", {}).get("week"), cs.week_key(_WEEK0))
check("闸门触发 → state.gate 记录原因",
      _gh_on.get("gate", {}).get("reasons"), ["HRV 夜均 46"])
check("未触发 → state.gate.blocked 为 False",
      _gh_off.get("gate", {}).get("blocked"), False)
check("未触发 → state.gate 原因清空（不残留上周理由）",
      _gh_off.get("gate", {}).get("reasons"), [])

print("\n" + "=" * 56)
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("✅ 全部通过")

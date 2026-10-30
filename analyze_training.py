#!/usr/bin/env python3
"""Weekly Garmin training data analyzer — enhanced edition."""
import os
import json
import sys
import urllib.request
from math import exp
from garminconnect import Garmin
from datetime import datetime, timedelta
from collections import defaultdict

# 跨周教练状态（负重 / 质量课档位 / 长骑基准 / 目标阶段）——唯一真相源
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coach_state  # noqa: E402

# ── Configuration ──────────────────────────────────────────────────────────
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
TRAINING_PLAN_ID = 42487676
FETCH_LIMIT = 150
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Central thresholds — tweak here instead of hunting through code.
CONFIG = {
    # CTL / ATL decay windows (days)
    "ctl_window": 42,
    "atl_window": 7,
    # Intensity Factor zones
    "if_anaerobic": 0.90,     # >= 0.90 -> threshold+
    "if_threshold": 0.80,     # >= 0.80 -> threshold
    "if_tempo":      0.70,    # >= 0.70 -> tempo
    "if_endurance":  0.55,    # >= 0.55 -> base endurance
    # HR zone percentages (of activity max HR)
    "hr_anaerobic":  0.90,
    "hr_threshold":  0.80,
    "hr_tempo":      0.70,
    "hr_endurance":  0.60,
    # TSB status thresholds
    "tsb_overtraining": -30,  # <= this -> overtraining risk
    "tsb_fatigue":      -10,  # <= this -> fatigue accumulation
    "tsb_fresh":          0,  # <= this -> normal recovery
    "tsb_supercomp":     15,  # >  this -> supercompensation
    # Coach recommendations
    "tss_high_week": 400,
    "tss_low_week":  150,
    "tss_jump_pct":   30,     # week-over-week increase warning
    "tss_drop_pct":  -50,     # week-over-week drop warning
    "min_weekly_sessions": 3,
    # Interval (lap-level) quality detection
    "work_pct_min":    0.88,  # %FTP to recognise a lap as a completed work rep
    "work_pct_ok":     0.97,  # >= target_low * this -> rep completed
    "work_pct_faded":  0.80,  # >= target_low * this -> attempted but faded
    "cooldown_pct":    0.60,  # below this %FTP a trailing rep looks like recovery
    "cooldown_hr_drop": 12,   # ... and HR this far below the completed reps
}

CYCLING_TYPES = [
    "indoor_cycling", "cycling", "road_biking", "virtual_ride",
    "mountain_biking", "gravel_cycling",
]
ALL_TYPES = CYCLING_TYPES + ["strength_training"]

# Workout-name keyword -> (low, high) target as a fraction of FTP.
# Mirrors the polarized plan pushed by push_plan_to_connect.py.
PLAN_TARGETS = {
    "阈值": (0.94, 1.01),
    "VO2max": (1.10, 1.18),
}


# ── Utility functions ──────────────────────────────────────────────────────

def get_total(acts, key):
    """Sum a numeric field across activities, treating None as 0."""
    return sum((x.get(key, 0) or 0) for x in acts)


def zone_info(if_val):
    """Intensity Factor -> (zone_label, emoji)."""
    if if_val >= CONFIG["if_anaerobic"]:
        return "阈值以上", "🔴"
    elif if_val >= CONFIG["if_threshold"]:
        return "阈值", "🟠"
    elif if_val >= CONFIG["if_tempo"]:
        return "节奏", "🟡"
    elif if_val >= CONFIG["if_endurance"]:
        return "基础有氧", "🟢"
    elif if_val > 0:
        return "恢复", "🔵"
    return "-", "⚪"


def hr_zone_label(avg_hr, max_hr):
    """Classify avg HR as % of activity max HR into a zone."""
    if max_hr <= 0:
        return "-", "⚪"
    pct = avg_hr / max_hr
    if pct >= CONFIG["hr_anaerobic"]:
        return "阈值以上", "🔴"
    if pct >= CONFIG["hr_threshold"]:
        return "阈值", "🟠"
    if pct >= CONFIG["hr_tempo"]:
        return "节奏", "🟡"
    if pct >= CONFIG["hr_endurance"]:
        return "基础有氧", "🟢"
    return "恢复", "🔵"


def calc_ctl_atl_tsb(daily_tss, today):
    """Calculate CTL (42d EWMA), ATL (7d EWMA), TSB from daily TSS dict."""
    if not daily_tss:
        return 0, 0, 0
    earliest = min(daily_tss.keys())
    start = datetime.strptime(earliest, "%Y-%m-%d")
    if start > today:
        return 0, 0, 0
    seq = []
    cw = CONFIG["ctl_window"]
    aw = CONFIG["atl_window"]
    for i in range((today - start).days + 1):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        seq.append(daily_tss.get(d, 0))
    ctl = atl = 0.0
    for ts in seq:
        ctl = ctl * exp(-1 / cw) + ts * (1 - exp(-1 / cw))
        atl = atl * exp(-1 / aw) + ts * (1 - exp(-1 / aw))
    return ctl, atl, ctl - atl


def fmt_tsb(tsb):
    """Format TSB into status string with emoji."""
    if tsb > CONFIG["tsb_supercomp"]:
        return "状态良好（超补偿）", "🟢"
    if tsb > CONFIG["tsb_fresh"]:
        return "正常恢复", "🔵"
    if tsb > CONFIG["tsb_fatigue"]:
        return "轻微疲劳", "🟡"
    if tsb > CONFIG["tsb_overtraining"]:
        return "疲劳累积", "🟠"
    return "过度训练风险", "🔴"


def build_bar(value, max_val, max_len=15):
    """Build an ASCII bar proportional to value/max_val."""
    if max_val <= 0 or value <= 0:
        return ""
    n = max(1, int(value / max_val * max_len))
    return "█" * n


def pct_change(new, old):
    """Calculate percentage change, safe for old=0."""
    if old <= 0:
        return None
    return (new - old) / old * 100


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    client = Garmin()
    try:
        client.login(os.path.expanduser("~/.garminconnect"))
    except Exception as e:
        print(f"❌ 无法连接 Garmin Connect: {e}", file=sys.stderr)
        sys.exit(1)

    today = datetime.now()

    # ── Week boundaries ────────────────────────────────────────────────────
    ws = today - timedelta(days=today.weekday())       # this week Mon
    we = ws + timedelta(days=6)                          # this week Sun
    lws = ws - timedelta(days=7)                         # last week Mon
    lwe = ws - timedelta(days=1)                         # last week Sun

    # ── Fetch activities ───────────────────────────────────────────────────
    try:
        all_activities = client.get_activities(0, FETCH_LIMIT)
    except Exception as e:
        print(f"❌ 获取训练数据失败: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Build daily TSS map for CTL/ATL/TSB ────────────────────────────────
    daily_tss = defaultdict(float)
    for a in all_activities:
        date = (a.get("startTimeLocal") or "")[:10]
        tss = a.get("trainingStressScore", 0) or 0
        if date:
            daily_tss[date] += tss
    ctl, atl, tsb = calc_ctl_atl_tsb(daily_tss, today)

    # ── Filter helpers ─────────────────────────────────────────────────────
    def activity_date(a):
        """Extract YYYY-MM-DD date string from activity, or empty string."""
        return (a.get("startTimeLocal") or "")[:10]

    def get_acts(start_str, end_str, types):
        """Return activities whose date falls in [start_str, end_str] and
        whose typeKey is in *types*."""
        return [
            a for a in all_activities
            if activity_date(a)
            and start_str <= activity_date(a) <= end_str
            and a.get("activityType", {}).get("typeKey", "") in types
        ]

    tw = get_acts(ws.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d"), ALL_TYPES)
    lw = get_acts(lws.strftime("%Y-%m-%d"), lwe.strftime("%Y-%m-%d"), ALL_TYPES)

    # ── Previous weeks trend (5 weeks before last week) ────────────────────
    prev = []
    for i in range(2, 7):
        s = ws - timedelta(days=7 * i)
        e = s + timedelta(days=6)
        a = get_acts(s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d"), ALL_TYPES)
        prev.append({
            "label": s.strftime("W%V"),
            "cnt": len(a),
            "min": get_total(a, "duration") / 60,
            "tss": get_total(a, "trainingStressScore"),
        })

    # ── Training plan items (handle cross-month spans correctly) ───────────
    plan_items = []
    seen_months = {(lws.year, lws.month), (lwe.year, lwe.month),
                   (ws.year, ws.month), (we.year, we.month)}
    all_items = []
    for year, month in seen_months:
        try:
            sched = client.get_scheduled_workouts(year, month)
            all_items.extend(sched.get("calendarItems", []))
        except Exception:
            pass

    range_start = lws.strftime("%Y-%m-%d")
    range_end = we.strftime("%Y-%m-%d")
    plan_items = [
        i for i in all_items
        if i.get("date", "")
        and range_start <= i.get("date", "") <= range_end
        and i.get("trainingPlanId") == TRAINING_PLAN_ID
    ]

    # ══════════════════════════════════════════════════════════════════════
    #  BUILD REPORT
    # ══════════════════════════════════════════════════════════════════════

    report = (
        f"# 周训练报告（上周 {lws.strftime('%Y-%m-%d')}–{lwe.strftime('%m-%d')}"
        f" · 本周计划 {ws.strftime('%Y-%m-%d')}–{we.strftime('%m-%d')}）\n\n---\n\n"
    )

    # ── 1. Training Load Status (CTL / ATL / TSB) ─────────────────────────
    if daily_tss:
        tsb_label, tsb_emoji = fmt_tsb(tsb)
        report += "## 📊 训练负荷状态\n\n"
        report += "| CTL (长期负荷) | ATL (短期疲劳) | TSB (状态) |\n"
        report += "|:---:|:---:|:---:|\n"
        report += f"| {ctl:.0f} | {atl:.0f} | {tsb:+.0f} {tsb_emoji} |\n\n"
        report += f"> **TSB {tsb_emoji} = {tsb:+.0f} — {tsb_label}**"
        if tsb < CONFIG["tsb_fatigue"]:
            report += " — 疲劳积累较多，注意恢复减量"
        elif tsb > CONFIG["tsb_supercomp"]:
            report += " — 状态回升，适合高质量训练和比赛"
        report += "\n\n"

    # ── 1b. Body / recovery metrics ────────────────────────────────────────
    body = None
    try:
        body = _fetch_body_metrics(client, lws, lwe)
    except Exception as e:
        print(f"⚠️ 身体数据拉取失败（不影响训练报告）: {e}", file=sys.stderr)
    if body:
        report += _write_body_section(body)

    # ── 1c. 自主神经闸门（HRV / RHR）────────────────────────────────────────
    # TSB 只由训练负荷算出，**看不到自主神经状态**。这里独立取最近 14 天，
    # 两者矛盾时以闸门为准 —— 详见 coach_state.autonomic_gate 的说明。
    gate = {"blocked": False, "reasons": [], "rhr_rise": 0}
    try:
        _auto = _fetch_autonomic(client, 14)
        gate = coach_state.autonomic_gate(
            hrv_last=_auto.get("hrv_last"),
            hrv_status=_auto.get("hrv_status"),
            hrv_baseline=_auto.get("hrv_baseline"),
            rhr_series=_auto.get("rhr_series"))
    except Exception as e:
        print(f"⚠️ 自主神经数据拉取失败（闸门按未触发处理）: {e}", file=sys.stderr)
    if gate["blocked"]:
        print(f"::warning::自主神经闸门触发：{'；'.join(gate['reasons'])}")

    # ── 2. Last-week details ──────────────────────────────────────────────
    if lw:
        report += _write_detail(lw, f"📋 上周训练回顾 ({lws.strftime('%-m/%-d')} - {lwe.strftime('%-m/%-d')})",
                                plan_items)
        report += _write_hr_dist(lw, "🎯 上周心率区间分布")
        report += _write_type_mix(lw, f"🚴 上周训练类型 ({lws.strftime('%-m/%-d')} - {lwe.strftime('%-m/%-d')})")
    else:
        report += _write_detail(tw, f"📋 本周训练一览 ({ws.strftime('%-m/%-d')} - {we.strftime('%-m/%-d')})",
                                plan_items)

    # ── 3. Rest days (pick the right range) ────────────────────────────────
    if lw:
        report += _write_rest_days(lws, lwe, lw)
    else:
        report += _write_rest_days(ws, we, tw)

    # ── 4. Training load trend (with ASCII visual) ────────────────────────
    if prev:
        report += _write_trend(prev, lw, tw)

    # ── 4b. Heat / humidity impact ────────────────────────────────────────
    heat = _analyze_heat(client, lw)
    if heat:
        report += _write_heat_section(heat)

    # ── 4c. Interval (lap-level) quality breakdown ────────────────────────
    # 逐组判定要用**与手表一致的 FTP**（= state 校准值），否则会出现
    # 「报告说你没达标、但手表上的目标其实达成了」这种自相矛盾。
    ftp_est = _estimate_ftp(lw or tw)
    state = coach_state.load()
    ftp = coach_state.ftp_of(state) or ftp_est
    quality = []
    for act_set in (lw, tw):
        if act_set:
            quality += _analyze_intervals(client, act_set, ftp)
    if quality:
        report += _write_interval_sessions(quality)

    # ── 4d. 跨周状态推进（闭环的核心）────────────────────────────────────
    # 读上周完成度 → 规则引擎推进 → 写回 state。push_plan_to_connect.py
    # 随后读同一份 state 生成课表，所以报告里说的和手表上推的必然一致。
    #
    # 力量次数 = Garmin 记录 + 手动补录。这个用户的力量课是自练的、不上表，
    # 只看 Garmin 会永远读到 0 次，把「没记录」当成「没在做」（实测 80 天里
    # 只有 2 条 strength_training）。补录日志见 state/strength_log.md。
    _acts_last = lw or tw
    strength_garmin = len([
        a for a in _acts_last
        if a.get("activityType", {}).get("typeKey", "") == "strength_training"])
    log_entries, log_warns = coach_state.read_strength_log(today=today)
    strength_manual = coach_state.count_strength_sessions(
        log_entries, lws.strftime("%Y-%m-%d"), lwe.strftime("%Y-%m-%d"))
    strength_n = strength_garmin + strength_manual
    for _w in log_warns:
        print(f"::warning::力量补录日志：{_w}", file=sys.stderr)

    planned_long = coach_state.planned_long_ride_min(
        state, state["goal"].get("phase") or "base")
    compliance = coach_state.compliance_from(
        quality, strength_n, coach_state.longest_ride(_acts_last), ftp_est)
    compliance["planned_long_ride_min"] = planned_long
    state, decisions = coach_state.advance(state, compliance, tsb, gate=gate)
    coach_state.save(state)

    # 补录日志本身要出现在报告里 —— 看不见反馈的功能没人会用
    report += _write_strength_log_section(
        log_entries, log_warns, strength_garmin, strength_manual,
        lws.strftime("%Y-%m-%d"), lwe.strftime("%Y-%m-%d"))

    # ── 5. Coach recommendations ──────────────────────────────────────────
    report += _write_recommendations(lw, prev, ctl, atl, tsb, heat, body, quality,
                                     state, decisions, gate, strength_n=strength_n)

    # ── 5b. 模型点评（可选层：没 key / 超时 / 报错都只少一段话）──────────
    try:
        import coach_llm
        ctx = _llm_context(state, lw or tw, quality, strength_n, ctl, atl, tsb,
                           body, heat, prev, heat, gate)
        report += coach_llm.render_section(coach_llm.coach_narrative(state, ctx))
    except Exception as e:
        print(f"::warning::模型点评生成失败（不影响报告其余部分）: {e}",
              file=sys.stderr)

    report += "\n---\n*报告自动生成 " + today.strftime("%Y-%m-%d %H:%M") + "*"

    fp = os.path.join(OUTPUT_DIR, "training_report.md")
    with open(fp, "w") as f:
        f.write(report)
    print(report)


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION BUILDERS
# ═══════════════════════════════════════════════════════════════════════════

def _write_detail(acts, title, plan_items):
    """Weekly activity detail table + plan comparison + per-activity lines."""
    report = f"\n## {title}\n\n"

    # Summary
    tss = get_total(acts, "trainingStressScore")
    dur = get_total(acts, "duration") / 60
    dist_km = get_total(acts, "distance") / 1000
    high_cnt = sum(1 for a in acts
                   if (a.get("intensityFactor", 0) or 0) >= CONFIG["if_threshold"])
    report += "| 次数 | 总时长 | 总TSS | 总距离 | 高强度次 |\n"
    report += "|:---:|:---:|:---:|:---:|:---:|\n"
    report += f"| {len(acts)} | {dur:.0f}min ({dur/60:.1f}h) | {tss:.0f} | {dist_km:.1f}km | {high_cnt} |\n\n"

    # Plan comparison
    if plan_items:
        plan_titles = list(dict.fromkeys([i.get("title", "") for i in plan_items]))
        done_names = [a.get("activityName", "").strip() for a in acts]
        report += "**训练计划完成度:**\n\n"
        ok_count = 0
        for t in plan_titles:
            matched = any(t in d or d in t for d in done_names)
            if matched:
                report += f"  ✅ **{t}**\n"
                ok_count += 1
            else:
                report += f"  ⬜ {t}\n"
        pct = round(ok_count / len(plan_titles) * 100) if plan_titles else 0
        report += f"\n  **完成率 {pct}%**\n\n"

    # Per-activity details
    for a in acts:
        tk = a.get("activityType", {}).get("typeKey", "")
        icon = "🚴" if tk in CYCLING_TYPES else "🏋️"
        name = a.get("activityName", "").strip()
        raw_date = a.get("startTimeLocal", "")
        date = raw_date[:10]
        time_of_day = raw_date[11:16] if len(raw_date) >= 16 else ""
        dur_m = (a.get("duration", 0) or 0) / 60
        dist = (a.get("distance", 0) or 0) / 1000
        tss = a.get("trainingStressScore", 0) or 0
        ifv = a.get("intensityFactor", 0) or 0
        hr = a.get("averageHR", 0) or 0
        hr_mx = a.get("maxHR", 0) or 0
        pwr = a.get("avgPower", 0) or 0
        p_mx = a.get("maxPower", 0) or 0
        np = a.get("normPower", 0) or 0
        cad = a.get("averageBikingCadenceInRevPerMinute", 0) or 0
        elev = a.get("elevationGain", 0) or 0
        cal = a.get("calories", 0) or 0
        ae = a.get("aerobicTrainingEffect", 0) or 0
        an = a.get("anaerobicTrainingEffect", 0) or 0
        zn, zi = zone_info(ifv)
        loc = "🌳 户外" if tk == "road_biking" else "🏠 室内"

        report += f"**{icon} {date} {time_of_day} {name}** {loc}\n\n"
        report += f"| 时长 {dur_m:.0f}min | 距离 {dist:.1f}km | TSS {tss:.0f} | IF {ifv:.2f} {zi}{zn} |\n"
        if tk in CYCLING_TYPES:
            report += f"| 功率 {pwr:.0f}w | NP {np:.0f}w | 最大 {p_mx:.0f}w | 踏频 {cad:.0f}rpm |\n"
        report += f"| 心率 {hr:.0f}/{hr_mx:.0f} | 爬升 {elev:.0f}m | 消耗 {cal:.0f}kcal"
        if ae or an:
            report += f" | TE 有氧 {ae:.1f} 无氧 {an:.1f}"
        report += " |\n\n"

    return report


def _write_hr_dist(acts, title):
    """Heart-rate zone distribution based on avg/max HR of each activity."""
    report = f"\n## {title}\n\n"
    zones = {"阈值以上": 0, "阈值": 0, "节奏": 0, "基础有氧": 0, "恢复": 0}
    zone_emoji = {"阈值以上": "🔴", "阈值": "🟠", "节奏": "🟡", "基础有氧": "🟢", "恢复": "🔵"}
    for a in acts:
        hr = a.get("averageHR", 0) or 0
        hr_mx = a.get("maxHR", 0) or 0
        zn, _ = hr_zone_label(hr, hr_mx)
        if zn in zones:
            zones[zn] += 1
    total = sum(zones.values())
    if total == 0:
        report += "  ℹ️ 本周训练无有效心率数据\n\n"
        return report
    for zn in ["阈值以上", "阈值", "节奏", "基础有氧", "恢复"]:
        c = zones[zn]
        if c > 0:
            em = zone_emoji[zn]
            bar = "█" * max(1, round(c / total * 15))
            report += f"  {em} **{zn}**: {c}次 {bar} {c/total*100:.0f}%\n"
    report += "\n"
    return report


def _write_type_mix(acts, title):
    """Break down cycling vs strength training."""
    report = f"\n## {title}\n\n"
    cycling = [a for a in acts
               if a.get("activityType", {}).get("typeKey", "") in CYCLING_TYPES]
    strength = [a for a in acts
                if a.get("activityType", {}).get("typeKey", "") == "strength_training"]

    c_tss = get_total(cycling, "trainingStressScore")
    s_tss = get_total(strength, "trainingStressScore")
    c_dur = get_total(cycling, "duration") / 60
    s_dur = get_total(strength, "duration") / 60
    c_dist = get_total(cycling, "distance") / 1000

    report += "| 类型 | 次数 | 时长 | TSS |\n|:---:|:---:|:---:|:---:|\n"
    report += f"| 🚴 骑行 | {len(cycling)} | {c_dur:.0f}min ({c_dur/60:.1f}h) | {c_tss:.0f} |\n"
    report += f"| 🏋️ 力量 | {len(strength)} | {s_dur:.0f}min ({s_dur/60:.1f}h) | {s_tss:.0f} |\n\n"
    if cycling and c_dist > 0:
        avg_spd = c_dist / (c_dur / 60) if c_dur > 0 else 0
        report += f"> 骑行总距离 {c_dist:.1f}km，平均速度 {avg_spd:.1f}km/h\n\n"

    return report


def _write_rest_days(start_date, end_date, acts):
    """Identify days in the week with no activities."""
    report = "\n## 📅 休息日\n\n"
    active_dates = {a.get("startTimeLocal", "")[:10] for a in acts}
    rest_days = []
    cur = start_date
    while cur <= end_date:
        ds = cur.strftime("%Y-%m-%d")
        if ds not in active_dates:
            rest_days.append(cur.strftime("%-m/%-d (%a)"))
        cur += timedelta(days=1)
    if rest_days:
        report += "  🛌 " + " | ".join(rest_days) + "\n\n"
    else:
        report += "  全勤，无休息日！注意合理安排恢复\n\n"
    return report


def _write_trend(prev, lw, tw):
    """Training load trend table with ASCII bars and week-over-week deltas."""
    report = "\n## 📈 训练负荷趋势（近5周）\n\n"

    # Gather all weekly TSS values (prev + lw + tw)
    all_tss = [p["tss"] for p in prev]
    lw_tss = get_total(lw, "trainingStressScore")
    tw_tss = get_total(tw, "trainingStressScore")
    all_tss += [lw_tss, tw_tss]
    max_tss = max(all_tss) if all_tss else 1

    lw_min = get_total(lw, "duration") / 60
    tw_min = get_total(tw, "duration") / 60

    report += "| 周 | 次数 | 时长 | TSS | 趋势 |\n"
    report += "|:--:|:---:|:---:|:---:|:---|\n"

    for p in prev:
        bar = build_bar(p["tss"], max_tss)
        report += f"| {p['label']} | {p['cnt']} | {p['min']:.0f}min | {p['tss']:.0f} | {bar} |\n"

    bar = build_bar(lw_tss, max_tss)
    report += f"| **上周** | **{len(lw)}** | **{lw_min:.0f}min** | **{lw_tss:.0f}** | {bar} |\n"
    bar = build_bar(tw_tss, max_tss)
    report += f"| **本周** | **{len(tw)}** | **{tw_min:.0f}min** | **{tw_tss:.0f}** | {bar} |\n\n"

    # Week-over-week change
    prev_tss = prev[-1]["tss"] if prev else 0
    delta_tss = pct_change(lw_tss, prev_tss)
    prev_min = prev[-1]["min"] if prev else 0
    delta_min = pct_change(lw_min, prev_min)

    if delta_tss is not None:
        arrow_tss = "📈" if delta_tss > 0 else "📉" if delta_tss < 0 else "➡️"
        arrow_min = "📈" if delta_min and delta_min > 0 else "📉" if delta_min and delta_min < 0 else "➡️"
        report += f"> **上周 vs 上上周**: TSS {arrow_tss} {delta_tss:+.0f}%"
        if delta_min is not None:
            report += f" ｜ 时长 {arrow_min} {delta_min:+.0f}%"
        report += "\n\n"

    # TSS vs 5-week average
    avg_tss = (sum(p["tss"] for p in prev) + lw_tss) / (len(prev) + 1)
    if lw_tss > avg_tss * 1.2:
        report += f"> ⚠️ 上周TSS({lw_tss:.0f}) 高于近5周均值({avg_tss:.0f})的20%以上，注意疲劳管理\n\n"
    elif lw_tss < avg_tss * 0.5 and len(lw) > 0:
        report += f"> ℹ️ 上周TSS({lw_tss:.0f}) 明显低于近5周均值({avg_tss:.0f})，减量周？\n\n"

    return report


def _estimate_ftp(acts):
    """Estimate FTP from cycling rides using NP / IF where both are valid."""
    estimates = []
    for a in acts:
        tk = a.get("activityType", {}).get("typeKey", "")
        if tk not in CYCLING_TYPES:
            continue
        np = a.get("normPower", 0) or 0
        ifv = a.get("intensityFactor", 0) or 0
        # IF==NP/FTP, but ignore recovery/easy rides (IF<0.6) which bias low
        if np > 0 and 0.60 <= ifv <= 1.05:
            estimates.append(np / ifv)
    if estimates:
        return sum(estimates) / len(estimates)
    return 0


# ── Interval (lap-level) quality analysis ───────────────────────────────
#
# Why this exists: whole-ride Normalized Power is dragged down by warm-up,
# recovery blocks and cool-down — and further depressed when the final rep
# falls apart. Comparing whole-ride NP against the interval target (e.g.
# 169W vs 206-221W) therefore UNDERSTATES the real intensity. The only fair
# judgement is per-rep: what was actually held during each work block.

def _target_for(name):
    """Return (lo, hi) target as fraction of FTP from the activity name."""
    for key, band in PLAN_TARGETS.items():
        if key in name:
            return band
    return (CONFIG["work_pct_min"], 1.20)  # unknown workout: generic work band


def _analyze_intervals(client, acts, ftp):
    """Per-rep analysis of every interval session in *acts*.

    Returns a list of dicts (one per interval session), each with the
    activity meta, the target band, and a per-rep breakdown. Non-interval
    rides (steady efforts) yield nothing and are skipped.
    """
    out = []
    if ftp <= 0:
        return out
    for a in acts:
        tk = a.get("activityType", {}).get("typeKey", "")
        if tk not in CYCLING_TYPES:
            continue
        try:
            splits = client.get_activity_splits(a["activityId"]) or {}
        except Exception:
            continue
        laps = splits.get("lapDTOs") or []
        if len(laps) < 3:
            continue

        pcts = [((l.get("averagePower") or 0) / ftp) for l in laps]
        work_idx = [i for i, p in enumerate(pcts) if p >= CONFIG["work_pct_min"]]
        if len(work_idx) < 2:
            continue  # no interval structure (steady ride)

        # Interval sessions alternate work/recover at a fixed cadence.
        gaps = [work_idx[k + 1] - work_idx[k] for k in range(len(work_idx) - 1)]
        period = max(set(gaps), key=gaps.count) if gaps else 1
        # Look one rep past the last completed one — that is exactly where a
        # blown-up final rep shows up (e.g. 214/213/158W).
        positions = list(range(work_idx[0],
                               min(work_idx[-1] + period + 1, len(laps)),
                               period))

        name = (a.get("activityName") or "").strip()
        lo, hi = _target_for(name)
        reps = []
        for n, i in enumerate(positions, 1):
            l = laps[i]
            pct = pcts[i]
            pwr = l.get("averagePower") or 0
            if pct >= lo * CONFIG["work_pct_ok"]:
                verdict = "ok"
            elif pct >= lo * CONFIG["work_pct_faded"]:
                verdict = "faded"
            else:
                verdict = "blown"
            reps.append({
                "n": n,
                "dur_min": (l.get("duration", 0) or 0) / 60,
                "power": pwr,
                "np": l.get("normalizedPower") or 0,
                "pct": pct,
                "hr": l.get("averageHR") or 0,
                "hr_max": l.get("maxHR") or 0,
                "verdict": verdict,
            })

        # The "one extra rep" lookahead can land on the cool-down block
        # (e.g. 10min @108W, HR 146 after reps ridden at 215W/HR 173).
        # Drop trailing segments that are clearly recovery: low power AND a
        # heart rate well below the completed reps — that is the rider
        # backing off, not blowing up.
        ok_hrs = [r["hr"] for r in reps if r["verdict"] == "ok" and r["hr"]]
        if ok_hrs:
            avg_ok_hr = sum(ok_hrs) / len(ok_hrs)
            reps = [r for r in reps
                    if not (r["verdict"] != "ok"
                            and r["pct"] < CONFIG["cooldown_pct"]
                            and r["hr"]
                            and r["hr"] < avg_ok_hr - CONFIG["cooldown_hr_drop"])]

        done = sum(1 for r in reps if r["verdict"] == "ok")
        out.append({
            "name": name,
            "date": (a.get("startTimeLocal") or "")[:10],
            "indoor": tk != "road_biking",
            "np": a.get("normPower", 0) or 0,
            "if": a.get("intensityFactor", 0) or 0,
            "target": (ftp * lo, ftp * hi),
            "reps": reps,
            "done": done,
            "total": len(reps),
        })
    return out


def _write_interval_sessions(sessions):
    """Render the 🔬 per-rep interval breakdown."""
    if not sessions:
        return ""
    ICON = {"ok": "✅", "faded": "🟡", "blown": "❌"}
    LABEL = {"ok": "达标", "faded": "未维持", "blown": "崩盘"}
    report = "\n## 🔬 质量课分段分析（lap 级，逐组功率）\n\n"
    report += ("> 整节课 NP 会被热身/组间恢复/冷身拉低，末组崩盘会进一步压低它——"
               "**拿整节课 NP 对比间歇目标会低估真实强度**。以下按分段(lap)逐组判定。\n\n")
    for s in sessions:
        loc = "🏠 室内" if s["indoor"] else "🌳 户外"
        tlo, thi = s["target"]
        report += f"**🚴 {s['date']} {s['name']}** {loc} — 整节课 NP {s['np']:.0f}W / IF {s['if']:.2f}，目标 {tlo:.0f}–{thi:.0f}W\n\n"
        report += "| 组 | 时长 | 平均功率 | %FTP | 心率(均/峰) | 判定 |\n|:--:|:--:|:--:|:--:|:--:|:--|\n"
        for r in s["reps"]:
            report += (f"| {r['n']} | {r['dur_min']:.1f}min | {r['power']:.0f}W | {r['pct']*100:.0f}% "
                       f"| {r['hr']:.0f}/{r['hr_max']:.0f} | {ICON[r['verdict']]} {LABEL[r['verdict']]} |\n")
        report += f"\n> 完成度 **{s['done']}/{s['total']}**"
        bad = [r for r in s["reps"] if r["verdict"] != "ok"]
        if bad:
            worst = bad[-1]
            good = [r for r in s["reps"] if r["verdict"] == "ok"]
            if good:
                drop = good[-1]["power"] - worst["power"]
                report += (f"；末组掉功率 **{drop:.0f}W**"
                           f"（{good[-1]['power']:.0f}W → {worst['power']:.0f}W），"
                           f"心率仍 {worst['hr']:.0f} 高位")
            report += " → 属**抗疲劳/热积累**，非目标功率不足\n\n"
        else:
            report += "，全组达标 ✅\n\n"
    return report


# ── Heat / environment analysis ─────────────────────────────────────────

def _ride_streams(det):
    """Extract aligned metric streams (temp/HR/power/RR/cadence/lat/lng) from
    an activity-details payload."""
    keys = [m.get("key") for m in det.get("metricDescriptors", [])]
    idx = {k: i for i, k in enumerate(keys)}
    want = ("directAirTemperature", "directHeartRate", "directPower",
            "directRespirationRate", "directBikeCadence",
            "directLatitude", "directLongitude")
    t_idx = idx.get("sumElapsedDuration")
    short = {"directAirTemperature": "temp", "directHeartRate": "hr",
             "directPower": "pwr", "directRespirationRate": "rr",
             "directBikeCadence": "cad", "directLatitude": "lat",
             "directLongitude": "lon"}
    rows = []
    for m in det.get("activityDetailMetrics", []):
        v = m.get("metrics", [])
        row = {"t": v[t_idx] if t_idx is not None and t_idx < len(v) else None}
        for k in want:
            i = idx.get(k)
            row[short[k]] = v[i] if i is not None and i < len(v) else None
        rows.append(row)
    return rows


def _band_hr(rows, lo=100, hi=150):
    """Average HR while riding in a given power band (default 100-150W)."""
    hrs = [r["hr"] for r in rows
           if r.get("hr") and r.get("pwr") and lo <= r["pwr"] < hi]
    return (sum(hrs) / len(hrs)) if hrs else None


def _avg_metric(rows, key, pred=None):
    vals = [r[key] for r in rows
            if r.get(key) and (pred(r) if pred else True)]
    return (sum(vals) / len(vals)) if vals else None


def _fetch_weather(lat, lon, date):
    """Optional historical weather lookup (open-meteo archive API)."""
    url = ("https://archive-api.open-meteo.com/v1/archive?"
           f"latitude={lat:.3f}&longitude={lon:.3f}"
           f"&start_date={date}&end_date={date}"
           "&hourly=temperature_2m,relativehumidity_2m")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.load(r)
        hourly = data["hourly"]
        temps = [t for t in hourly["temperature_2m"] if t is not None]
        hums = [h for h in hourly["relativehumidity_2m"] if h is not None]
        if not temps:
            return None
        return {
            "tmin": min(temps),
            "tmax": max(temps),
            "hum_avg": sum(hums) / len(hums) if hums else None,
        }
    except Exception:
        return None


def _analyze_heat(client, acts):
    """Analyze heat stress on the week's longest ride. Returns a dict of
    metrics (or None when no temperature data / not hot)."""
    cycling = [a for a in acts
               if a.get("activityType", {}).get("typeKey", "") in CYCLING_TYPES]
    if not cycling:
        return None
    longest = max(cycling, key=lambda a: a.get("duration", 0) or 0)
    aid = longest["activityId"]
    try:
        det = client.get_activity_details(aid)
    except Exception:
        return None
    rows = _ride_streams(det)
    temps = [r["temp"] for r in rows if r.get("temp")]
    if len(temps) < 60:  # no usable temperature stream
        return None
    tavg = sum(temps) / len(temps)
    if tavg < 26:  # not hot enough to matter
        return None

    hrs = [r["hr"] for r in rows if r.get("hr")]
    hr_band = _band_hr(rows)
    rr_band = _avg_metric(rows, "rr", lambda r: r.get("pwr") and 100 <= r["pwr"] < 150)

    # Cardiac drift at matched power: first vs second half of the ride
    half_t = rows[len(rows) // 2].get("t")
    drift = None
    if half_t:
        f = [r["hr"] for r in rows if r.get("t") is not None and r["t"] < half_t
             and r.get("hr") and r.get("pwr") and 100 <= r["pwr"] < 150]
        s = [r["hr"] for r in rows if r.get("t") is not None and r["t"] >= half_t
             and r.get("hr") and r.get("pwr") and 100 <= r["pwr"] < 150]
        if len(f) >= 30 and len(s) >= 30:
            drift = sum(s) / len(s) - sum(f) / len(f)

    z2_share = sum(1 for h in hrs if h <= 140) / len(hrs) if hrs else 0

    # Baseline = coolest-ride HR at the same power band (e.g. indoor rides)
    base = None
    for a in cycling:
        if a["activityId"] == aid:
            continue
        try:
            d2 = client.get_activity_details(a["activityId"])
        except Exception:
            continue
        r2 = _ride_streams(d2)
        if not r2:
            continue
        t2 = [r["temp"] for r in r2 if r.get("temp")]
        t2avg = sum(t2) / len(t2) if t2 else None
        h2 = _band_hr(r2)
        if h2 and (t2avg is None or t2avg < 28):
            base = min(base, h2) if base is not None else h2

    delta = (hr_band - base) if (hr_band and base) else None

    weather = None
    lats = [r["lat"] for r in rows if r.get("lat")]
    lons = [r["lon"] for r in rows if r.get("lon")]
    if lats and lons:
        weather = _fetch_weather(sum(lats) / len(lats), sum(lons) / len(lons),
                                 (longest.get("startTimeLocal") or "")[:10])

    return {
        "aid": aid,
        "name": longest.get("activityName", "").strip(),
        "date": (longest.get("startTimeLocal") or "")[:10],
        "dur_min": (longest.get("duration", 0) or 0) / 60,
        "tmin": min(temps), "tavg": tavg, "tmax": max(temps),
        "hr_band": hr_band, "base_hr": base, "delta": delta,
        "z2_share": z2_share, "drift": drift, "rr_band": rr_band,
        "weather": weather,
    }


def _write_heat_section(heat):
    """Report section explaining heat/humidity impact on HR."""
    report = "\n## 🌡️ 高温/湿热环境影响\n\n"
    report += f"**🚴 {heat['date']} {heat['name']}**（{heat['dur_min']:.0f}min）\n\n"

    w = heat["weather"]
    weather_str = "（未获取到天气数据）"
    if w:
        hum = f"，湿度 {w['hum_avg']:.0f}%" if w.get("hum_avg") else ""
        weather_str = f"{w['tmin']:.0f}–{w['tmax']:.0f}°C{hum}"

    base_str = "—"
    delta_str = "—"
    if heat["base_hr"]:
        base_str = f"{heat['base_hr']:.0f}bpm"
        if heat["delta"]:
            delta_str = f"+{heat['delta']:.0f}bpm"

    rr_str = f"{heat['rr_band']:.0f}次/分" if heat.get("rr_band") else "—"
    hrband_str = f"{heat['hr_band']:.0f}bpm" if heat.get("hr_band") else "—"

    report += "| 码表气温 | 当地天气 | 同功率心率 (100–150W) | 凉爽基准 | 差值 | Z2心率占比 | 呼吸频率 |\n"
    report += "|:---:|:---:|:---:|:---:|:---:|:---:|:---:|\n"
    report += (
        f"| {heat['tmin']:.0f}–{heat['tmax']:.0f}°C（均值{heat['tavg']:.0f}）| {weather_str} | "
        f"{hrband_str} | {base_str} | {delta_str} | "
        f"{heat['z2_share']*100:.0f}% | "
        f"{rr_str} |\n\n")

    drift_str = ""
    if heat["drift"] is not None:
        drift_str = f"，后半程同功率心率漂移 +{heat['drift']:.0f}bpm"
    if heat["delta"] and heat["delta"] >= 10:
        rr_note = (
            f"，且低功率下呼吸频率已达 {heat['rr_band']:.0f}次/分"
            if heat.get("rr_band") else "")
        verdict = (
            f"**高温高湿是心率偏高的主因**：同功率心率比凉爽环境高约 "
            f"{heat['delta']:.0f}bpm{drift_str}{rr_note}，是典型热应激表现。")
    else:
        verdict = (
            "当天偏热，心率有小幅上浮；结合功率判断强度即可。")
    report += f"> {verdict}\n\n"
    report += (
        "  🌡️ **热天配速以功率为准**（不看心率）：Z2 按功率区间骑，"
        "HR 设硬上限 155–160bpm，超过就降功率歇一歇\n"
        "  ⏰ **长骑改清晨**：5:30–6:00 出发、10 点前结束，避开 34°C+ 时段\n"
        "  💧 **补水补电解质**：每小时 500–800ml 水 + 钠 500–1000mg，带冰水/湿毛巾\n\n")
    return report


def _fetch_body_metrics(client, start, end):
    """Pull daily wellness/recovery metrics for [start, end] and aggregate.
    Missing data stays None so the report can skip it gracefully."""
    rhr, min_hr, sleep, stress, bb_h, bb_l = [], [], [], [], [], []
    hrv_weekly = hrv_last = hrv_status = None
    weight = None
    weight_present = False
    d = start
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        try:
            s = client.get_user_summary(ds) or {}
            if s.get("restingHeartRate") is not None:
                rhr.append(s["restingHeartRate"])
            if s.get("minHeartRate") is not None:
                min_hr.append(s["minHeartRate"])
            if s.get("sleepingSeconds"):
                sleep.append(s["sleepingSeconds"] / 60.0)
            if s.get("averageStressLevel") is not None:
                stress.append(s["averageStressLevel"])
            if s.get("bodyBatteryHighestValue") is not None:
                bb_h.append(s["bodyBatteryHighestValue"])
            if s.get("bodyBatteryLowestValue") is not None:
                bb_l.append(s["bodyBatteryLowestValue"])
        except Exception:
            pass
        try:
            h = client.get_hrv_data(ds) or {}
            hsum = h.get("hrvSummary") or {}
            if hsum.get("weeklyAvg") is not None:
                hrv_weekly = hsum["weeklyAvg"]
            if hsum.get("lastNightAvg") is not None:
                hrv_last = hsum["lastNightAvg"]
            if hsum.get("status") is not None:
                hrv_status = hsum["status"]
        except Exception:
            pass
        try:
            b = client.get_body_composition(ds) or []
            if isinstance(b, list):
                for ent in b:
                    wl = ent.get("dateWeightList") or []
                    if wl:
                        weight_present = True
                        weight = wl[-1].get("weight")
        except Exception:
            pass
        d += timedelta(days=1)
    avg = lambda x: (sum(x) / len(x)) if x else None
    return {
        "rhr_avg": avg(rhr), "rhr_min": min(rhr) if rhr else None,
        "rhr_max": max(rhr) if rhr else None,
        "min_hr": min(min_hr) if min_hr else None,
        "sleep_avg_min": avg(sleep), "stress_avg": avg(stress),
        "bb_high": max(bb_h) if bb_h else None, "bb_low": min(bb_l) if bb_l else None,
        "hrv_weekly": hrv_weekly, "hrv_last": hrv_last, "hrv_status": hrv_status,
        "weight": weight, "weight_present": weight_present,
    }


def _fetch_autonomic(client, days=14):
    """取最近 days 天的 HRV / RHR 逐日序列，供自主神经闸门判断趋势。

    为什么不复用 `_fetch_body_metrics`：那个函数的窗口是「上周 Mon–Sun」，而且
    每天只留最后一个值 —— 既看不出形状，也不含今早刚出的 HRV。而闸门要的恰恰
    是「最近几天的趋势」。宁可多几次 API 调用也不去动它，免得改坏报告里
    「上周值」那张表（那是给人看的，语义必须稳定）。
    """
    end = datetime.now().date()
    start = end - timedelta(days=days)
    hrv_series, rhr_series = [], []
    hrv_last = hrv_status = hrv_baseline = None
    d = start
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        try:
            s = client.get_user_summary(ds) or {}
            rhr_series.append((ds, s.get("restingHeartRate")))
        except Exception:
            pass
        try:
            hsum = ((client.get_hrv_data(ds) or {}).get("hrvSummary") or {})
            if hsum.get("lastNightAvg") is not None:
                hrv_series.append((ds, hsum["lastNightAvg"]))
                hrv_last = hsum["lastNightAvg"]
                hrv_status = hsum.get("status")
                hrv_baseline = hsum.get("baseline")
        except Exception:
            pass
        d += timedelta(days=1)
    return {
        "hrv_last": hrv_last, "hrv_status": hrv_status,
        "hrv_baseline": hrv_baseline,
        "hrv_series": hrv_series, "rhr_series": rhr_series,
    }


def _write_body_section(body):
    """Render the 🩺 body-recovery section; only show fields that have data."""
    if not body:
        return ""
    f_ = lambda x: f"{x:.0f}" if isinstance(x, (int, float)) else "—"
    rows = []
    rhr = body.get("rhr_avg")
    rhr_j = ("正常偏低，恢复良好" if rhr and rhr < 60
             else "偏高，关注恢复" if rhr else "—")
    rows.append(f"| 静息心率(夜间均值) | {f_(rhr)} bpm | {rhr_j} |")
    sl = body.get("sleep_avg_min")
    sl_j = ("充足(≥7h)" if sl and sl >= 420 else "偏短(<7h)" if sl else "—")
    rows.append(f"| 平均睡眠 | {f_(sl)} min | {sl_j} |")
    hrvw = body.get("hrv_weekly")
    hrv_j = f"周均 {f_(hrvw)} ms" if hrvw else "—"
    rows.append(f"| 夜间HRV(昨夜) | {f_(body.get('hrv_last'))} ms | {hrv_j} |")
    st = body.get("stress_avg")
    st_j = ("放松" if st is not None and st < 35
            else "偏高" if st and st > 50 else "正常")
    rows.append(f"| 日均压力 | {f_(st)} | {st_j} |")
    rows.append(f"| 身体电量(峰/谷) | {f_(body.get('bb_high'))}/{f_(body.get('bb_low'))} | — |")
    if body.get("weight_present") and body.get("weight") is not None:
        rows.append(f"| 体重 | {body['weight']} kg | 最近一次测量（已同步）|")
    else:
        rows.append("| 体重 | 未记录 | 手动录入引导见下方提示 |")
    out = "\n## 🩺 身体恢复指标\n\n"
    out += "| 指标 | 上周值 | 判读 |\n|:--|:--:|:--|\n" + "\n".join(rows) + "\n"
    if not (body.get("weight_present") and body.get("weight") is not None):
        out += ("\n> 💡 **体重待录入（你已选手动录入路线）**：在 Garmin Connect App 依次点 "
                "「我的」→「体重」→「+ 添加」，每周称一次记录即可；录入后**下周一报告会自动显示**趋势。"
                "（蚂蚁阿福等封闭生态秤无法同步至 Garmin，已排除自动同步路径。）\n")
    alerts = []
    if rhr and body.get("rhr_max") and body.get("rhr_min") is not None \
            and body["rhr_max"] - body["rhr_min"] >= 8:
        alerts.append("⚠️ 周内静息心率波动 ≥8 bpm，恢复可能不稳定")
    if st is not None and st > 55:
        alerts.append("⚠️ 日均压力偏高，建议增加睡眠与放松")
    if alerts:
        out += "\n> " + "  ".join(alerts) + "\n"
    return out + "\n"


def _pz(ftp, lo, hi):
    """Format a power zone range from FTP (percentages)."""
    if ftp <= 0:
        return "（需功率计估算FTP）"
    if lo == hi:
        return f"{ftp * lo / 100:.0f}W"
    return f"{ftp * lo / 100:.0f}–{ftp * hi / 100:.0f}W"


def _write_recommendations(acts, prev, ctl, atl, tsb, heat=None, body=None,
                           quality=None, state=None, decisions=None, gate=None,
                           strength_n=None):
    """Generate detailed, actionable coach-style recommendations.

    strength_n 是「Garmin 力量活动 + 手动补录」的合计次数。传 None 时回落到
    只看 Garmin —— 但对这个用户那等于永远读到 0 次（自练不上表），所以
    真实调用路径一定会传进来。
    """
    report = "\n## 💡 教练建议\n\n"
    quality = quality or []
    gate = gate or {}
    blocked = bool(gate.get("blocked"))

    lw_tss = get_total(acts, "trainingStressScore")
    high = sum(1 for a in acts
               if (a.get("intensityFactor", 0) or 0) >= CONFIG["if_threshold"])
    mid = sum(1 for a in acts
              if (a.get("intensityFactor", 0) or 0) >= CONFIG["if_tempo"])
    z2 = sum(1 for a in acts
             if a.get("activityType", {}).get("typeKey", "") in CYCLING_TYPES
             and CONFIG["if_endurance"] <= (a.get("intensityFactor", 0) or 0) < CONFIG["if_tempo"])
    cycling_acts = [a for a in acts
                    if a.get("activityType", {}).get("typeKey", "") in CYCLING_TYPES]
    strength_acts = [a for a in acts
                     if a.get("activityType", {}).get("typeKey", "") == "strength_training"]

    # FTP 有两个值，用途不同，务必分清：
    #   ftp_est —— 报告自己的估算，只用于「估算FTP≈NNNW」这句提示
    #   ftp     —— 实际用于算功率区间的值，**以 state 的校准值为准**
    # 之前两处混用会让报告里的课表瓦数与手表上推的不一致（同一份报告出现
    # 201–216W 和 204–219W 两个区间），所以这里统一到 state。
    ftp_est = _estimate_ftp(acts)
    ftp = coach_state.ftp_of(state) if state else ftp_est
    if not ftp:
        ftp = ftp_est
    ftp_str = f"（估算FTP≈{ftp_est:.0f}W）" if ftp_est > 0 else ""
    recs = []

    # — 自主神经闸门：优先于一切训练负荷判断 —
    # 放在最前面，因为它是「本周能不能上强度」的一票否决项。后面那些基于 TSB /
    # TSS / IF 的建议都是负荷视角的，只有这一条看的是身体本身。
    if blocked:
        recs.append(
            "🔴 **自主神经告警 —— 本周取消全部强度课**: "
            + "；".join(gate.get("reasons") or [])
            + "。TSB 只反映训练负荷、看不到自主神经状态，所以**即使 TSB 看着很好也不上强度**"
            "（2026-10-12 那次误判就是这么来的：TSB +16 但 HRV 已 LOW）。"
            "恢复判据：HRV 回到平衡区、静息心率回落，再谈质量课。"
            "⚠️ 若伴随咽痛/鼻塞/乏力/低烧，应**完全休息**，而不是「降级成 Z2 继续练」。")

    # — Recovery / wellness signals (body metrics) —
    if body:
        if body.get("hrv_last") is not None and body.get("hrv_weekly") is not None \
                and body["hrv_last"] < body["hrv_weekly"] * 0.85:
            recs.append(
                "🩺 **恢复信号**: 昨夜 HRV 低于周均 15%+，提示副交感恢复不足；"
                "若今日安排质量课，强度可下调 5-10% 或改为 Z2 有氧。")
        if body.get("rhr_avg") and body.get("rhr_max") and body.get("rhr_min") is not None \
                and body["rhr_max"] - body["rhr_min"] >= 8:
            recs.append(
                "🩺 **静息心率波动**: 周内静息心率起伏 ≥8 bpm，结合训练量关注恢复是否充分，"
                "必要时多排 1 天恢复日。")
        if body.get("stress_avg") and body["stress_avg"] > 55:
            recs.append(
                "🩺 **压力偏高**: 日均压力 >55，建议增加睡眠与放松，避免再叠加高强度课。")

    # — CTL / TSB based —
    if ctl > 0:
        tsb_label, tsb_emoji = fmt_tsb(tsb)
        if tsb < CONFIG["tsb_fatigue"]:
            recs.append(
                f"🔴 **疲劳管理**: TSB={tsb:+.0f}（{tsb_label}），短期疲劳超过长期负荷。"
                "本周安排1-2天完全休息，其余以Z1-Z2低强度有氧为主（功率≤{z2}），"
                "严禁连续高强度；优先保证每晚≥8h睡眠、每日1.5–2g/kg蛋白质摄入。"
                .format(z2=_pz(ftp, 56, 75)))
        elif tsb > CONFIG["tsb_supercomp"]:
            if blocked:
                recs.append(
                    f"⚡ **状态窗口被否决**: TSB={tsb:+.0f}（{tsb_label}）按负荷本该上强度，"
                    "但自主神经指标未恢复 → **本周不安排质量课**，见上方告警。"
                    "别把「新鲜」当成「可以练」。")
            else:
                recs.append(
                    f"⚡ **状态窗口**: TSB={tsb:+.0f}，疲劳已充分消除、进入超补偿。"
                    "这是安排高质量课（VO2max间歇或比赛模拟）的最佳时机，"
                    "建议本周做1次全力测试/比赛 + 2次节奏/阈值课，抓住状态高峰。")
        else:
            recs.append(
                f"🔵 **状态判断**: TSB={tsb:+.0f}，处于「正常恢复」区间（CTL {ctl:.0f} / ATL {atl:.0f}）。"
                "说明上周负荷已被身体吸收、短期疲劳基本消退，可按原计划推进；"
                "本周可小幅提升质量课比例，但不宜连续两周加码，避免疲劳堆积。")

    # — Interval execution quality (lap-level, most trustworthy signal) —
    for s in quality:
        bad = [r for r in s["reps"] if r["verdict"] != "ok"]
        if not bad:
            continue
        good = [r for r in s["reps"] if r["verdict"] == "ok"]
        if not good:
            recs.append(
                f"🔬 **质量课未达标（{s['date']} {s['name']}）**: 目标 "
                f"{s['target'][0]:.0f}–{s['target'][1]:.0f}W，{s['total']} 组全部未达到目标下沿"
                f"（最高 {max(r['power'] for r in s['reps']):.0f}W）。"
                "下周间歇段把功率顶到目标下沿，**看功率不看心率**——心率高是热天正常现象。")
            continue
        drop = good[-1]["power"] - bad[-1]["power"]
        # 「减组数」和「保底功率」都从上一次的档位算出，不再写死数字
        cur = state["quality"] if state else coach_state.DEFAULT_STATE["quality"]
        alt_thr = max(2, cur["threshold"]["sets"] - 1)
        alt_vo2 = max(3, cur["vo2"]["sets"] - 1)
        recs.append(
            f"🔬 **末组维持能力（{s['date']} {s['name']}）**: 完成度 {s['done']}/{s['total']}，"
            f"前 {s['done']} 组达标（{good[0]['power']:.0f}–{good[-1]['power']:.0f}W），"
            f"末组掉到 {bad[-1]['power']:.0f}W（-{drop:.0f}W）但心率仍 {bad[-1]['hr']:.0f} 高位 → "
            "**抗疲劳/热积累，不是功率没顶上去**。下周优先把末组做完："
            f"① 减组数保质量（阈值课先 {alt_thr}×10min、VO2max 先 {alt_vo2}×3min）"
            f"② 末组设保底功率（阈值 {_pz(ftp, 90, 94)} / VO2max {_pz(ftp, 105, 108)}）"
            "③ 室内加强散热（风扇直吹、提前降温）。")

    # — Intensity balance —
    if acts:
        # A ride with >=1 completed interval rep counts as high-intensity work,
        # even when whole-ride IF stays below the threshold (recovery blocks
        # drag IF down the same way they drag NP down).
        hit_quality = any(s["done"] > 0 for s in quality)
        if high < 1 and not hit_quality and not blocked:
            recs.append(
                f"⚡ **缺少高强度**: 上周 {len(acts)} 次训练全部落在节奏区间（IF 0.70-0.80），"
                "无阈值以上刺激，长期会钝化无氧能力。本周建议插入 1 次阈值/间歇质量课：\n"
                f"   •  方案A（阈值）: 热身15min + **3×10min @ {_pz(ftp, 95, 105)}**（Z4，组间慢骑5min） + 放松10min\n"
                f"   •  方案B（VO2max）: 热身15min + **5×3min @ {_pz(ftp, 110, 120)}**（Z5，组间3min恢复） + 放松10min\n"
                "   目标：单课 TSS 90-110，ETE（无氧训练效果）≥ 3.0。")
        if mid >= 1 and high < 1 and not hit_quality and not blocked:
            recs.append(
                "🟡 **强度结构（非极化）**: 上周以「节奏（Tempo / Z3）」为主、缺乏极化——"
                "Z3 是性价比最低的灰色区（既没拿到低强度恢复、也没拿到高强度刺激）。"
                "建议改为极化分配：约 80% 骑行量压到 Z2 有氧、仅 1–2 次集中冲高（阈值/间歇，"
                "每周高强度≤2 次且与长骑隔天），形成「低-高」两极分布，比长期卡在 Z3 更能提升阈值功率与 VO2max。")
        elif z2 > max(len(acts) * 0.6, 2):
            recs.append("⚖️ **强度结构**: 基础有氧占比偏高，适当加入高强度间歇（如1-2组5-10分钟阈值）提升效率")

    # — Training volume & frequency —
    if acts:
        if lw_tss > CONFIG["tss_high_week"]:
            recs.append(f"📊 **大负荷周**: TSS {lw_tss:.0f}，训练量较大。保证每天≥8h睡眠，注意营养补充与下肢放松")
        elif lw_tss < CONFIG["tss_low_week"]:
            if blocked:
                recs.append(
                    f"📈 **低负荷周**: TSS {lw_tss:.0f}，训练量偏低 —— 但本次**不要补量**。"
                    "自主神经没恢复时加量只会把恢复期拖更长，先把 HRV 养回来。")
            else:
                recs.append(f"📈 **低负荷周**: TSS {lw_tss:.0f}，训练量偏低。如身体无疲劳可适当增加时长或强度")

        if len(acts) < CONFIG["min_weekly_sessions"]:
            recs.append(
                f"📋 **训练频率**: 上周仅 {len(acts)} 次训练（建议每周 4-5 次）。"
                "过低频率会让每次骑行被迫加量、恢复不足。建议组合为：3次骑行 + 1-2次力量 + 1-2次短有氧。")
        # Detect consecutive rest-day clusters
        rest_cluster = _longest_rest_cluster(acts, prev)
        if rest_cluster >= 3:
            recs.append(
                f"🗓️ **训练分布**: 上周出现连休 {rest_cluster} 天，负荷「断崖式」集中在其余几天。"
                "建议把训练均匀铺开（例如隔天一练），维持 CTL 稳定性、减少单次疲劳峰值。")

    # — Strength balance —
    _st_n = len(strength_acts) if strength_n is None else int(strength_n)
    if len(cycling_acts) >= 3 and _st_n == 0:
        # 动作与负重从 state 取，和手表上推的完全一致
        _st = state or coach_state.DEFAULT_STATE
        _sp = coach_state.strength_plan(_st)
        _mv = _sp["A"] or _sp["B"]
        recs.append(
            f"🏋️ **力量训练缺失**: 上周 0 次力量（Garmin 无力量活动 + 补录日志无条目，"
            f"已连续 {_st['strength']['consecutive_missed']} 周为空）。"
            "骑行会消耗核心稳定性，长期缺力量易引发下背/膝痛。"
            f"本周安排：{coach_state.describe_movements(_mv)}；"
            "安排在强度骑行后的次日或独立日，避免与阈值课同日。"
            "⚠️ **如果你其实做了只是没记录** —— 往 `state/strength_log.md` 补一行"
            "（行首写日期即可），系统下一周就会把它算进来，别让「没记录」继续被当成「没在做」。"
            "（动作按 9/27 fitting 结论选配：臀大肌+髂腰肌+核心为主，暂用髋铰链替代深蹲，减少膝内侧负荷。）")
    elif len(cycling_acts) == 0 and _st_n >= 2:
        recs.append("🚴 **骑行训练**: 上周无骑行。建议本周逐渐恢复骑行，先从低强度有氧开始")

    # — Duration balance —
    if acts:
        for a in acts:
            dur = (a.get("duration", 0) or 0) / 60
            tk = a.get("activityType", {}).get("typeKey", "")
            if tk in CYCLING_TYPES and dur > 180:
                heat_note = ""
                if heat and heat["aid"] == a.get("activityId"):
                    heat_note = (
                        f" 高温天（{heat['tavg']:.0f}°C）注意：以功率控制强度、"
                        "HR 上限 155–160bpm，每 30min 补水并补电解质")
                recs.append(
                    f"🏔️ **长距离骑行**: {a.get('activityName','').strip()} 时长 {dur:.0f}min，"
                    "次日DOMS与中枢疲劳风险高。建议配合 15min 泡沫轴/拉伸、冷热水交替浴，并保证当日碳水 6-8g/kg。"
                    + heat_note)
                break

    # — Heat stress —
    if heat and heat.get("delta") and heat["delta"] >= 10:
        recs.append(
            f"🌡️ **高温/湿热**: 上周最长骑行码表气温 {heat['tavg']:.0f}°C"
            f"（最高 {heat['tmax']:.0f}°C），同功率心率比凉爽基准高约 {heat['delta']:.0f}bpm。"
            "热天以功率为准配速、心率只作上限参考，长骑改清晨并加强补水。")

    # — Week-over-week change warning —
    if prev:
        prev_tss = prev[-1]["tss"]
        delta = pct_change(lw_tss, prev_tss)
        if delta and delta > CONFIG["tss_jump_pct"]:
            recs.append(f"📈 **负荷跳跃**: 上周TSS环比+{delta:.0f}%，增幅超过{CONFIG['tss_jump_pct']}%。本周建议维持或轻微减量，避免过度疲劳")
        elif delta and delta < CONFIG["tss_drop_pct"] and lw_tss > 0:
            recs.append(f"📉 **负荷骤降**: 上周TSS环比{delta:.0f}%。如非计划减量，本周可逐步恢复训练量")

    # — Fallback —
    if not recs:
        if not acts:
            recs.append("📌 本周刚开始，建议按Garmin计划完成训练")
        else:
            prev_tss = prev[-1]["tss"] if prev else 0
            if 0 < lw_tss <= CONFIG["tss_high_week"]:
                recs.append("✅ **均衡状态**: 训练量与强度搭配合理，继续保持当前节奏")

    for r in recs:
        report += f"  {r}\n"

    # — Concrete weekly schedule (only when we have enough signal) —
    if ctl > 0 and cycling_acts:
        report += _write_state_section(state, decisions)
        report += _write_weekly_plan(ftp, tsb, len(strength_acts), ftp_str, heat,
                                     state, gate)

    return report


# ═══════════════════════════════════════════════════════════════════════════
#  自适应决策透明化
# ═══════════════════════════════════════════════════════════════════════════

PHASE_LABEL = coach_state.PHASE_LABEL


def _write_strength_log_section(entries, warns, garmin_n, manual_n, start, end):
    """🏋️ 力量补录 —— 把「人肉信号源」的状态摊开给人看。

    没有这一段补录就是黑箱：写了不知道算没算上、漏了不知道系统看到了什么。
    另外把解析失败的行明确列出来 —— 静默丢弃会让人以为系统坏了。
    """
    out = "\n## 🏋️ 力量补录（手动日志）\n\n"
    out += ("> 力量课自练不上表，Garmin 里看不到。**做完往 `state/strength_log.md` 写一行**"
            "（行首是日期，后面随便写 —— 动作/负重/组数都行），系统就会把它算进上周完成度，"
            "并参与「连续几周完成 → 加重」的推进。\n\n")
    out += f"| 来源 | 上周（{start[5:]} – {end[5:]}）次数 |\n|:--|:--:|\n"
    out += f"| Garmin 力量活动 | {garmin_n} |\n"
    out += f"| 手动补录 | {manual_n} |\n"
    out += f"| **合计** | **{garmin_n + manual_n}** |\n\n"
    if entries:
        out += "**最近记录**（最新在前）\n\n"
        for e in reversed(entries[-5:]):
            out += f"- `{e['date']}` {e['note'] or '（只写了日期）'}\n"
        out += "\n"
    else:
        out += ("> 日志里还没有任何条目。格式就一行，例如：\n"
                f"> `{datetime.now().strftime('%Y-%m-%d')} 力量A 臀桥 3×10@20kg "
                "+ 保加利亚蹲 2×10@8kg`\n\n")
    if warns:
        out += "⚠️ **下面这些行没有被计入** —— 缺日期或格式无法识别：\n\n"
        for w in warns[:5]:
            out += f"- {w}\n"
        if len(warns) > 5:
            out += f"- …另有 {len(warns) - 5} 行同类问题\n"
        out += "\n"
    return out


def _write_state_section(state, decisions):
    """🎛️ 本周调整依据 —— 把规则引擎的决策日志摊开给人看。

    这是「动态」和「随机」的分界线：每一处调整都必须能追溯到上周的一条
    实际完成数据。若某项没变，也明确写出来为什么没变。
    """
    if not state:
        return ""
    phase = state["goal"].get("phase", "base")
    thr_s, thr_r = coach_state.quality_of(state, "threshold")
    vo2_s, vo2_r = coach_state.quality_of(state, "vo2")
    w = coach_state.weights_of(state)
    e = state["endurance"]
    s = state["strength"]
    st_ftp = coach_state.ftp_of(state)

    out = "\n## 🎛️ 本周调整依据（自适应决策）\n\n"
    out += ("> 以下是相对上周的**实际变化**，每条都由上周的真实完成数据触发。"
            "状态存于 `state/training_state.json`，随报告一起进版本库，可回溯每一周的决策。\n\n")

    out += "| 项目 | 当前值 | 上周完成情况 |\n|:--|:--|:--|\n"
    out += (f"| 目标阶段 | {PHASE_LABEL.get(phase, phase)} "
            f"（{state['goal'].get('name','')} · {state['goal'].get('date','')}） | — |\n")
    out += (f"| 阈值课 | {thr_s}×{thr_r}min @{_pz(st_ftp, 94, 101)} "
            f"| 连续满组 {state['quality']['threshold']['consecutive_ok']} 周 |\n")
    out += (f"| VO2max | {vo2_s}×{vo2_r}min @{_pz(st_ftp, 110, 118)} "
            f"| 连续满组 {state['quality']['vo2']['consecutive_ok']} 周 |\n")
    _mode_label = {"full": "完整 A+B（2 次/周）", "single": "单次主课力量A（1 次/周）",
                   "minimal": "B 精简版单次（1 次/周）"}.get(s.get("mode"), s.get("mode"))
    out += (f"| 力量模式 | {_mode_label} "
            f"| 断档 {s['consecutive_missed']} 周 / 连续完成 {s['consecutive_done']} 周 |\n")
    out += (f"| 长骑基准 | {e['long_ride_min']}min "
            f"| 连续达标 {e['consecutive_long_ok']} 周 |\n")
    out += (f"| 负重 | 臀桥 {w['bridge']:g}kg · 硬拉 {w['rdl']:g}kg · "
            f"分腿蹲 {w['split_squat']:g}kg/手 | 见下方决策 |\n")
    out += f"| FTP | {st_ftp:.0f}W | 本次估算 {state['ftp'].get('estimate') or '—'} |\n\n"

    if decisions:
        out += "**本周决策日志**\n\n"
        for d in decisions:
            out += f"- {d}\n"
        out += "\n"
    return out


def _longest_rest_cluster(acts, prev):
    """Heuristic: longest run of consecutive rest days in the analysed week."""
    # Reconstruct from activities we have; requires date on each act.
    active = sorted({a.get("startTimeLocal", "")[:10] for a in acts
                     if a.get("startTimeLocal")})
    if not active:
        return 0
    # Determine week window from min/max active date
    from datetime import datetime
    d0 = datetime.strptime(active[0], "%Y-%m-%d")
    d1 = datetime.strptime(active[-1], "%Y-%m-%d")
    days = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((d1 - d0).days + 1)]
    longest = cur = 0
    for d in days:
        if d in active:
            longest = max(longest, cur)
            cur = 0
        else:
            cur += 1
    longest = max(longest, cur)
    return longest


def _week_tss(hot, st):
    """各课 TSS 与周合计 —— 由课的结构算出（TSS = Σ 时长(h)×IF²×100）。

    报告正文与模型点评共用同一份，避免两处各算一遍又对不上。改组数或时长，
    这里的结果自动跟着变，不会再出现「文案写 95 而结构其实变了」。
    """
    thr_sets, thr_reps = coach_state.quality_of(st, "threshold")
    vo2_sets, vo2_reps = coach_state.quality_of(st, "vo2")
    lng_min = coach_state.planned_long_ride_min(st, st["goal"].get("phase", "base"))
    t = {
        "threshold": coach_state.session_tss(
            [(15, 0.58), (thr_sets * thr_reps, 0.97), (thr_sets * 5, 0.55), (10, 0.55)]),
        "vo2": coach_state.session_tss(
            [(15, 0.58), (vo2_sets * vo2_reps, 1.14), (vo2_sets * 3, 0.55), (10, 0.55)]),
        "z2": coach_state.session_tss([(10, 0.55), (75, 0.62), (10, 0.55)]),
        "long": coach_state.session_tss(
            [(10, 0.55), (lng_min, 0.57 if hot else 0.62), (10, 0.55)]),
    }
    t["total"] = sum(t.values())
    return t


def _llm_context(state, lw, quality, strength_n, ctl, atl, tsb, body, heat, prev, hot,
                 gate=None):
    """整理给模型的结构化上下文 —— 只放已定稿的结论，不放原始活动流水。

    gate 必须传进来：模型只写散文、不做数值决策，但它很容易照着 TSB 写出
    「状态很好可以上强度」这类和课表相反的话。把闸门结论明确告诉它，
    它才知道这一周已经被定性为「有氧恢复周」。
    """
    phase = state["goal"].get("phase", "base")
    long_act = coach_state.longest_ride(lw)
    planned_long = coach_state.planned_long_ride_min(state, phase)
    gate = gate or {}
    blocked = bool(gate.get("blocked"))

    def qsum(kw):
        ss = [s for s in (quality or []) if kw in (s.get("name") or "")]
        if not ss:
            return "无记录"
        best = max(ss, key=lambda s: s["done"])
        return f"{best['done']}/{best['total']} 组达标"

    cyc = [a for a in (lw or [])
           if a.get("activityType", {}).get("typeKey", "") in CYCLING_TYPES]
    _st_target = coach_state.strength_target(state)
    comp = {
        "骑行次数": len(cyc),
        "力量": f"{strength_n}/{_st_target} 次"
                + ("（含手动补录）" if strength_n else
                   "（Garmin 无力量活动 + 补录日志无条目）"),
        "阈值课": qsum("阈值"),
        "VO2max课": qsum("VO2"),
        "长骑": (f"{(long_act.get('distance') or 0) / 1000:.0f}km / "
                 f"{(long_act.get('duration') or 0) / 60:.0f}min（计划 {planned_long}min）"
                 if long_act else "无记录"),
        "上周骑行TSS": round(get_total(lw or [], "trainingStressScore")),
    }

    b = {}
    if body:
        if body.get("rhr_avg"):
            b["静息心率"] = round(body["rhr_avg"])
        if body.get("sleep_avg_min"):
            b["睡眠小时"] = round(body["sleep_avg_min"] / 60, 1)
        if body.get("stress_avg"):
            b["日均压力"] = round(body["stress_avg"])
        if body.get("hrv_last"):
            b["HRV"] = body["hrv_last"]
        if body.get("weight_present") and body.get("weight"):
            b["体重kg"] = body["weight"]
    b.update({"CTL": round(ctl), "ATL": round(atl), "TSB": round(tsb)})
    if heat:
        b["本周热天"] = "是"

    _t_all = _week_tss(hot, state)
    # 闸门触发时质量课已取消，周 TSS 必须报重算后的值，否则模型会照着旧数字说话
    _wk_tss = (3 * _t_all["z2"] + _t_all["long"]) if blocked else _t_all["total"]
    ctx = {
        "compliance": comp,
        "body": b,
        "tss_cap": coach_state.tss_cap(state, phase),
        "week_tss": round(_wk_tss),
        "weeks": [{"周": x["label"], "次数": x["cnt"], "TSS": round(x["tss"])}
                  for x in reversed(prev or [])],
    }
    if blocked:
        ctx["⚠️ 自主神经闸门"] = (
            "已触发 —— 本周定性为「有氧恢复周」，取消全部强度课，只骑 Z2。"
            "触发原因：" + "；".join(gate.get("reasons") or [])
            + "。TSB 只反映训练负荷，不代表可以上强度，点评不要建议任何质量课。")
    return ctx


def _write_weekly_plan(ftp, tsb, strength_done, ftp_str, heat=None, state=None,
                       gate=None):
    """Emit a detailed, follow-along weekly schedule (polarized 80/20).

    结构与数值全部来自 coach_state 的推进结果：质量课的组×时长、逐动作负重、
    长骑时长与形态、力量模式都由上周完成度决定。这里**不再有任何硬编码的
    瓦数、负重或周 TSS** —— 改结构，TSS 会自动跟着变。

    唯一的例外是自主神经闸门：它触发时会把两节质量课整体换成 Z2（见 gate）。
    这不是「训练安排」，是安全约束，所以允许覆盖上面的结构。
    """
    st = state or coach_state.DEFAULT_STATE
    gate = gate or {}
    blocked = bool(gate.get("blocked"))
    block_why = "；".join(gate.get("reasons") or [])

    # 功率区间一律以 state 的**校准 FTP** 为准。ftp 参数只是没有 state 时的
    # 兜底 —— 不再信任调用方传进来的值，免得又出现「调整依据表一个区间、
    # 课表另一个区间、手表上又是第三个」这种三方不一致。
    _st_ftp = coach_state.ftp_of(st)
    if _st_ftp:
        ftp = _st_ftp

    phase = st["goal"].get("phase", "base")
    thr_sets, thr_reps = coach_state.quality_of(st, "threshold")
    vo2_sets, vo2_reps = coach_state.quality_of(st, "vo2")
    sp = coach_state.strength_plan(st)
    lng_min = coach_state.planned_long_ride_min(st, phase)
    variant = st["goal"].get("long_ride_variant") or \
        coach_state.long_ride_variant(st, phase, tsb)
    if blocked:
        # 爬坡专项虽然功率守在 Z2，但它是「专项负荷」，前提是基础在维持。
        # 自主神经都没恢复还去怼 4–6 段坡，正是闸门要拦的事 → 强制退回纯 Z2。
        variant = "z2"
    tss_cap = coach_state.tss_cap(st, phase)

    hot = heat
    hn = "（热天：长骑按功率配速、心率作安全上限、补水电优先）" if hot else ""

    # ── 功率区间（唯一来源：FTP）────────────────────────────────────────────
    z2r = _pz(ftp, 56, 68)
    z2 = _pz(ftp, 55, 70)
    thr = _pz(ftp, 94, 101)
    vo2 = _pz(ftp, 110, 118)
    lng = _pz(ftp, 50, 64) if hot else z2
    # 区间下沿的实际瓦数 —— 起步档用，不再手写「205–220W」这类会过期的数字
    thr_start = int(round(ftp * 0.94))
    vo2_start = int(round(ftp * 1.10))

    # ── 各课 TSS：由结构算出 ───────────────────────────────────────────────
    _t = _week_tss(hot, st)
    t_thr, t_vo2, t_z2, t_lng = _t["threshold"], _t["vo2"], _t["z2"], _t["long"]
    week_tss = _t["total"]
    if blocked:
        # 闸门触发 → 周一/周三两节质量课整体换成 Z2，周 TSS 必须跟着重算，
        # 否则报告里会留下「已取消的课」的 TSS，和课表自相矛盾。
        t_thr = t_vo2 = 0
        week_tss = 3 * t_z2 + t_lng
    over_cap = week_tss > tss_cap

    report = "\n### 🗓️ 本周建议训练安排（极化 80/20）\n\n"
    if blocked:
        report += (
            "> 🔴 **自主神经闸门已触发 → 本周为「有氧恢复周」，取消全部强度课。**\n"
            f"> 触发原因：{block_why}。\n"
            "> TSB 只反映训练负荷、看不到自主神经状态，所以**即使 TSB 很正也不上强度**。\n"
            "> 恢复判据：HRV 回到平衡区、静息心率回落，再谈质量课。\n"
            "> 每日自检：HRV 回升且静息心率回落 → 可骑 Z2 60–90min；否则休息。\n\n")
    else:
        report += (f"> 基于当前状态（TSB={tsb:+.0f}）与体能 {ftp_str}{hn}。框架：**大量 Z2 有氧打底 "
                   f"+ 每周仅 1–2 次高质量（阈值/间歇），Z3 节奏区尽量砍掉**；高强度≤2 次且与长骑隔天；"
                   f"骑行踏频全程 80–90rpm。可按体感微调。\n\n")

    # ---- overview table (scan-at-a-glance) ----
    thr_dur = f"≈{15 + thr_sets * (thr_reps + 5) + 10}min"
    vo2_dur = f"≈{15 + vo2_sets * (vo2_reps + 3) + 10}min"
    long_dur = f"≈{lng_min}min"
    # 力量三档位。判断一律看「这一档有没有那节课」（A/B 是否为空），不再比
    # mode 字符串 —— single 档下周日是没有力量课的，光判 minimal 会漏。
    has_a, has_b = bool(sp["A"]), bool(sp["B"])
    if sp["mode"] == "minimal":
        fri_type, sun_type = "🚴 Z2 恢复骑", "🏋️ 力量B 精简版（单次）"
        sun_dur = "20–25min"
    elif has_b:
        fri_type, sun_type = "🚴 Z2 恢复骑 + 🏋️力量A", "🏋️ 力量B + 轻量核心"
        sun_dur = "30–40min"
    else:   # single：一周只练一次力量，主课放周五（低负荷日，练腿最合适）
        fri_type, sun_type = "🚴 Z2 恢复骑 + 🏋️力量A（本周唯一）", "🧘 休息 + 活动度"
        sun_dur = "—"
    sat_type = "🚴 长距离 Z2（含爬坡专项）" if variant == "hills" else "🚴 长距离 Z2"

    if blocked:
        mon_row = ("周一", "🚴 Z2 有氧（替代质量课）", "≈75min", z2r, f"{t_z2:.0f}")
        wed_row = ("周三", "🚴 Z2 有氧（替代质量课）", "≈75min", z2r, f"{t_z2:.0f}")
    else:
        mon_row = ("周一", "🚴 阈值 Z4（质量）", thr_dur, thr, f"{t_thr:.0f}")
        wed_row = ("周三", "🚴 VO2max Z5（质量）", vo2_dur, vo2, f"{t_vo2:.0f}")
    ov = [
        mon_row,
        ("周二", "🧘 休息", "—", "完全休息/散步", "0"),
        wed_row,
        ("周四", "🧘 休息", "—", "完全休息/散步", "0"),
        ("周五", fri_type, "≈95min" if has_a else "≈85min",
         z2r, f"{t_z2:.0f}"),
        ("周六", sat_type, long_dur, lng, f"{t_lng:.0f}"),
        ("周日", sun_type, sun_dur,
         "活动度/拉伸" if sp["target"] == 1 and has_a else "核心/髋外展/单腿", "0"),
    ]
    report += "| 日 | 类型 | 时长 | 主强度 | TSS |\n|:--:|:--|:--:|:--:|:--:|\n"
    for d, t, dur, intensity, tss in ov:
        report += f"| {d} | {t} | {dur} | {intensity} | {tss} |\n"

    # ---- detailed day-by-day (follow-along) ----
    details = []
    # 整段描述做成变量：拼接「周三」+「Z2 有氧课」很容易漏掉或多出空格（已踩过）
    _mon_desc = "周一 Z2 有氧课" if blocked else "周一阈值质量课"
    _wed_desc = "周三 Z2 有氧课" if blocked else "周三 VO2max 质量课"
    if blocked:
        details.append(("周一 · 🚴 Z2 有氧（闸门触发 · 原阈值课已取消）", [
            "**目标**：维持有氧底座，**不施加任何强度刺激**。",
            f"**主课 60–90min @ {z2r}（压 Z2）**；平路/缓坡，踏频 85–90rpm。",
            f"**硬上限 {int(round(ftp * 0.70))}W** —— Z2 最容易偷偷骑成 Z2.5，超了就松，宁低勿高。",
            "**心率**：作安全上限，>160 降功率；但别掉到 Z2 功率下限以下（那就成了恢复骑，没有有氧刺激价值）。",
            "**红线**：出现咽痛/鼻塞/乏力/低烧 → **完全休息**，不是「降级骑 Z2」。",
            f"**TSS≈{t_z2:.0f}**。",
        ]))
    else:
        details.append(("周一 · 🚴 阈值 Z4（质量课）", [
            "**目标**：维持/提升乳酸阈值功率，刺激阈值耐受。",
            "**热身 15min**：5min Z1 + 5min Z2 + 5min 含 3×30s 加速唤醒，踏频 85–90。",
            f"**主课**：{thr_sets}×{thr_reps}min @ {thr}"
            f"（从下沿 **{thr_start}W** 起步，验证体感再往上沿靠）；组间 5min 慢骑 Z1 恢复。",
            "**冷身 10min**：Z1 慢骑 + 腿后侧/髋屈肌拉伸 5min。",
            "**踏频**：主课段 85–90rpm。",
            "**心率**：预期 155–170，**以功率为准**——心率偏高勿降功率（热天尤甚）。",
            "**补给**：课前补水 300–500ml，课中每 20min 一口水。",
            f"**TSS≈{t_thr:.0f}**（由 {thr_sets}×{thr_reps}min 结构算出，非固定值）。",
        ]))
    details.append(("周二 · 🧘 休息（完全休息）", [
        f"**目标**：在{_mon_desc}后充分恢复，维持 CTL 稳定而非硬堆量。",
        "**内容**：完全休息，可轻松散步 20–30min 或做轻微拉伸；不做任何 structured 训练。",
        "**优先**：保证睡眠（恢复晴雨表：夜间 RHR 若连续升至 60+ 警惕过度疲劳/生病前兆）。",
    ]))
    if blocked:
        details.append(("周三 · 🚴 Z2 有氧（闸门触发 · 原 VO2max 课已取消）", [
            "**目标**：同日一 —— 只维持有氧，不碰强度。",
            f"**主课 60–90min @ {z2r}（压 Z2）**；踏频 85–90rpm。",
            f"**硬上限 {int(round(ftp * 0.70))}W**。",
            "**别用「多骑一会儿」补回取消的强度** —— 自主神经没恢复时加量只会把恢复期拖更长。",
            f"**TSS≈{t_z2:.0f}**。",
        ]))
    else:
        details.append(("周三 · 🚴 VO2max Z5（间歇，质量课）", [
            "**目标**：拉高最大摄氧量上限（Z5），本周第 1 次高质量刺激。",
            "**热身 15min**：5min Z1 + 5min Z2 + 5min 含 3×30s 加速唤醒，踏频 85–90。",
            f"**主课（间歇）**：{vo2_sets}×{vo2_reps}min @ {vo2}"
            f"（首组从 **{vo2_start}W** 起步，能稳住再往上沿推）；组间 3min 慢骑 Z1 充分恢复。",
            "**冷身 10min**：Z1 慢骑 + 腿后侧/髋屈肌拉伸 5min。",
            "**踏频**：间歇段可略高 90–95rpm。",
            "**心率**：间歇段预期冲到 170–185（接近峰值），**看功率不看心率**。",
            "**补给**：间歇前补电解质，课中每 10–15min 一口。",
            f"**TSS≈{t_vo2:.0f}**。",
        ]))
    details.append(("周四 · 🧘 休息（完全休息）", [
        f"**目标**：在{_wed_desc}后恢复，为周末蓄力。",
        "**内容**：完全休息，可轻松散步或轻微拉伸；不做任何 structured 训练。",
        "**优先**：补水、营养、睡眠到位，让疲劳充分消退。",
    ]))

    # 周五：Z2 恢复骑（力量A 在 full / single 两档下同日进行）
    _st_done_note = ("—— 已按计划完成，负重正在自动推进。"
                     if strength_done >= sp["target"] else
                     "—— 未达计划次数，本周先做到位再谈加重。"
                     "（若你其实做了只是没记录，补一行到 `state/strength_log.md`。）")
    fri_lines = [
        f"**上周力量实际完成 {strength_done}/{sp['target']} 次**{_st_done_note}",
    ]
    if has_a:
        fri_lines.insert(0, "**目标**：有氧积累（Z2）+ 本周力量主课。力量重心按 fitting（9/27）结论排布：**臀大肌 + 髂腰肌 + 核心**，治骨盆后倾→腰酸的本，兼补左腿偏弱（48%）。")
    else:
        fri_lines.insert(0, "**目标**：有氧积累（Z2）。周五不安排力量 —— 本周那一次放在周日（见下）。")
    fri_lines.append(
        f"**主课 60–90min @ {z2r}（压 Z2）**，平路/缓坡，避大起伏；踏频 85–90rpm（室内尤其提到 80+）。")
    if not has_a:
        fri_lines.append(
            f"**力量本周降级**：上周为 0 次（连续 {st['strength']['consecutive_missed']} 周），"
            f"不安排力量A —— 本周只求完成周日那一次精简版，先把习惯接回来。")
    else:
        fri_lines += [
            f"**接力量A（{'主' if has_b else '本周唯一力量课'}，40–50min，组间休 60–90s）**："
            f"{coach_state.describe_movements(sp['A'])}。",
            "**⌚ 已绑定 Garmin 官方动作库 + 目标负重**：每个动作都挂了 `category`/`exerciseName` 与 "
            "`weightValue`，手表上会显示**真实动作名称、示范动画和负重**，并按 reps 计数。",
            f"**负重递进是自动的**：连续两周按计划完成力量课，负重自动上调"
            f"（臀桥/硬拉 +2.5kg，分腿蹲/单腿硬拉 +1kg，髂腰肌 +0.5kg），"
            f"当前臀桥 **{st['strength']['weights']['bridge']:g}kg**。",
            "**为什么去掉深蹲**：fitting 测得股四头肌双侧紧绷、左膝鹅足敏感——用臀桥+髋铰链替代高脚杯深蹲，减少膝内侧与髌股负荷。",
        ]
        if blocked:
            # 闸门拦的是「强度刺激」，但力量也是实打实的负荷。原方案只取消两节
            # 质量课、照常推满量力量A，属于报告自己没说圆 —— 这里补上执行口径。
            fri_lines.append(
                "🔴 **闸门触发期间的力量课降级执行**：组数减半（3 组 → 2 组）、"
                "负重降到平时的 80%，**不做力竭组、不追 PR**；"
                "当天静息心率仍偏高或身体有异样 → **直接跳过**，改做拉伸。"
                "自主神经没恢复时，力量负荷同样要还账。")
    fri_lines += [
        "**心率**：不强制；若 >160 可适当降 5–10W，但别掉出 Z2 功率下限。",
        "**补给**：养成每 20min 喝水的习惯；力量前少量碳水。",
        "**冷身·骑后拉伸流程 9min（已绑定官方动作库）**：弓步式髋屈肌拉伸 → 股四头肌 → 腘绳肌 → 腓肠肌 → 臀大肌 → 梨状肌，每项 90s（左右各 45s）。对应 fitter 的 P0 要求，手表上带示范动画。",
        f"**TSS≈{t_z2:.0f}（骑行，力量不计入）**。",
    ]
    _fri_title = (" + 🏋️ 力量A" + ("（主）" if has_b else "（本周唯一）")) if has_a else ""
    details.append(("周五 · 🚴 Z2 恢复骑" + _fri_title, fri_lines))

    # 周六：长骑（base/taper 纯 Z2；build/peak 插入爬坡专项）
    lng_lines = [
        "**目标**：有氧底座积累——**绝不飘到 Z3**，这是你之前长骑骑成节奏课的纠偏重点。",
    ]
    if blocked:
        lng_lines.append(
            "**⚠️ 本周长骑是「可选」而不是「指标」**：出门前先看表 —— HRV 回升且静息心率回落才去，"
            "否则改成 60min 轻松骑、或者直接休息。**别为了完成度硬上。**")
    if variant == "hills":
        lng_lines.append(
            f"**主课 {lng_min}min — 爬坡专项 @ {lng}**：前段找一条 **5–7km、坡度约 5%** 的坡，"
            f"**重复 4–6 次**；每次上坡功率守在 **Z2 不冲**（不摇车、不踏进 Z4），"
            f"下坡顺骑恢复；后段转平路用剩余时间继续磨 Z2。"
            f"专项目的是「长时间爬坡下的有氧耐受 + 后半段还能输出」，不是把爬坡当间歇骑。")
    else:
        lng_lines.append(f"**主课 {lng_min}min @ {lng}（压 Z2）**；选平坦/缓坡路线，踏频 85–90rpm。"
                         + ("热天下调，按功率配速、心率作上限。" if hot else ""))
    lng_lines += [
        "**心率**：作安全上限，>160 降一点功率，但底线不低于 Z2 功率下限（勿为压心率把功率降到恢复区）。",
        "**补给**：每小时 500–750ml + 电解质；每 45–60min 少量碳水（能量胶/香蕉）。",
        f"**TSS≈{t_lng:.0f}**。" + ("次日（周日）安排轻量力量/核心，长骑后避免高强度课。"
                                 if has_b else
                                 "长骑后次日不安排任何训练 —— 本周力量已放在周五，周日留给恢复。"),
    ]
    details.append(("周六 · 🚴 长距离耐力（死守 Z2）", lng_lines))

    # 周日：minimal → B 精简版（本周唯一）；full → 力量B；single → 无力量课
    if sp["mode"] == "minimal":
        sun_title = "周日 · 🏋️ 力量B 精简版（本周唯一力量课）"
        sun_lines = [
            f"**目标**：力量已连续 {st['strength']['consecutive_missed']} 周为空。"
            f"本周不追负荷，**目标只有一个：做一次**。",
            f"**主课（20–25min）**：{coach_state.describe_movements(sp['B'])}。",
            "**做完即达标** —— 完成这件事本身就是本周的胜利，重量轻无所谓。",
            "**活动度**：髋屈肌/股四/腘绳拉伸 + **鹅足部位按摩球放松**（左膝内侧），每处 60s。",
        ]
        if blocked:
            sun_lines.append(
                "🔴 **闸门触发期间**：就这 20–25min，不加动作、不加重量；"
                "当天静息心率仍高或身体有异样 → 只做拉伸。")
    elif has_b:
        sun_title = "周日 · 🏋️ 力量B + 轻量核心"
        sun_lines = [
            "**目标**：本周第 2 次力量——**核心稳定 + 髋外展（臀中肌）+ 单腿平衡**，护膝并维持躯干稳定；左腿优先。",
            f"**主课（30–40min，轻负荷）**：{coach_state.describe_movements(sp['B'])}。",
            "**活动度**：髋屈肌/股四/腘绳拉伸 + **鹅足部位按摩球放松**（左膝内侧），每处 60s。",
            "**冷身·骑后拉伸流程 9min**：同力量A（弓步式髋屈肌 → 股四 → 腘绳 → 腓肠 → 臀 → 梨状肌，各 90s）+ **鹅足部位按摩球放松**（左膝内侧）。",
            "**注意**：长骑次日，以活动度和轻负荷为主，不冲大重量；动作质量 > 负荷。",
        ]
    else:
        sun_title = "周日 · 🧘 休息 + 活动度"
        sun_lines = [
            "**目标**：本周力量只安排一次（周五力量A），周日不重复练 —— 力量的恢复本身就是训练的一部分。",
            "**内容**：完全休息；或 20–30min 轻松散步 + 髋屈肌/股四/腘绳/腓肠拉伸各 60s。",
            "**鹅足部位按摩球放松**（左膝内侧）60s —— fitting 的 P0 项，不花时间，但不做等于白调。",
            "**若周五力量没做**：周日就是补做窗口，直接照周五那段「力量A」练，不要顺延到下周。",
        ]
    sun_lines.append("**TSS≈0**。")
    details.append((sun_title, sun_lines))

    report += "\n**每日详细（可直接照练）**\n\n"
    for title, lines in details:
        report += f"**{title}**\n"
        for ln in lines:
            report += f"- {ln}\n"
        report += "\n"

    # ---- weekly TSS budget & polarized cap ----
    cap_note = ""
    if tsb <= CONFIG["tsb_fatigue"]:
        cap_note = (f"（TSB={tsb:+.0f} 疲劳累积 → 按你的标准封顶 {tss_cap}，先稳再增量）")
    elif over_cap:
        cap_note = f"（已超阶段上限 {tss_cap}，如体感吃紧优先砍周五 Z2）"
    if sp["target"] == 1:
        strength_note = ("力量 1 次（周五主课力量A）不计入 TSS" if has_a
                         else "力量 1 次（周日 B 精简版）不计入 TSS")
    else:
        strength_note = "力量 2 次（周五主力量A + 周日力量B）不计入 TSS"
    if blocked:
        dist = ("极化分布：本周**全 Z2、零强度**（自主神经闸门触发）。"
                "周二/周四完全休息 —— 这一周的唯一 KPI 是把神经系统的账还清，"
                "不是堆量也不是补课。")
    else:
        dist = ("极化分布：Z2 为底、2 次高质量（周一阈值 + 周三间歇），Z3≈0。"
                "周二/周四完全休息；高强度与长骑隔天安排（周三间歇 → 周六长骑中间隔周四休息+周五Z2），避免连休后集中爆发。")
    report += (
        f"> **周骑行 TSS≈{week_tss:.0f}**{cap_note}（上限 {tss_cap}）；{strength_note}。"
        + dist + "\n\n")
    return report



if __name__ == "__main__":
    main()

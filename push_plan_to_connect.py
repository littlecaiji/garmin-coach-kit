#!/usr/bin/env python3
"""Push the report's recommended weekly plan to Garmin Connect as scheduled
structured workouts.

The plan mirrors analyze_training.py's polarized 80/20 weekly layout exactly:
  Mon 阈值 Z4 | Tue 休息 | Wed VO2max Z5 | Thu 休息
  | Fri Z2 恢复骑 + 力量A | Sat 长距离 Z2 | Sun 力量B
(Tue/Thu have no scheduled workout = rest days.)

Idempotent: reuses existing "建议·" templates, deletes stale ones, and
reschedules this week's placements so each run produces a clean layout.

Dates are computed dynamically (current week Mon–Sun), so it works every week
without manual edits.

Usage: push_plan_to_connect.py [--dry-run]
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta

from garminconnect import Garmin

# 跨周教练状态 —— 组数/负重/长骑时长/力量模式全部由它决定，本文件不再
# 持有任何训练参数的副本（此前 W_* 常量与报告文案各写一份，必然漂移）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coach_state  # noqa: E402


CYCLING = {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2}
STRENGTH = {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5}
NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2}
NAME_PREFIX = "建议"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")


def _step(order, secs, step_key, step_tid, target=None, v1=None, v2=None,
          desc=None):
    s = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": step_tid, "stepTypeKey": step_key, "displayOrder": step_tid},
        "description": desc,
        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time",
                         "displayOrder": 2, "displayable": True},
        "endConditionValue": float(secs),
        "targetType": target or NO_TARGET,
        "targetValueOne": v1,
        "targetValueTwo": v2,
        "strokeType": {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0},
        "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0},
    }
    return s


def _repeat(order, iterations, steps):
    return {
        "type": "RepeatGroupDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        "numberOfIterations": iterations,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations",
                         "displayOrder": 7, "displayable": False},
        "endConditionValue": float(iterations),
        "workoutSteps": steps,
    }


# ---------------------------------------------------------------------------
# Strength steps
#
# Garmin's own exercise library can be attached to a strength step through the
# `category` + `exerciseName` fields, so the watch shows the real movement name
# and its animation instead of a bare text note. Valid values come from the FIT
# profile — `garminconnect.exercises` bundles the catalogue (1527 entries,
# 47 categories) with `resolve(name)` / `find(term)` lookup helpers.
#
# A strength block is a RepeatGroupDTO (sets) wrapping:
#   * an exercise step — endCondition "reps" (id 10), or "time" for holds
#   * a rest step       — stepTypeId 5 ("rest"), time based
# `skipLastRestStep` drops the pointless rest after the final set.
# ---------------------------------------------------------------------------
WEIGHT_KG = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

# ``weightValue`` is in GRAMS tagged with the kilogram unit
# (see garminconnect.workout.WEIGHT_UNIT_KILOGRAM), hence the ×1000 in
# _strength_block.
#
# 负重**不再写在这里** —— 它来自 state/training_state.json，由
# coach_state.advance() 根据「力量课是否连续两周按计划完成」自动递进。
# 想手动改重量：编辑 state 文件里的 strength.weights（键：bridge / rdl /
# split_squat / hipflex / sl_bridge / sl_rdl），报告与课表会同步生效。


# 骑后拉伸流程 —— fitter（9/27）要求每次骑完做 10min 针对性拉伸：
# 髋屈肌 / 股四头肌 / 腘绳肌 / 腓肠肌 / 臀大肌 / 梨状肌。每个动作 90s（左右各 45s）。
# 同样绑定官方动作库，手表上有动作示范。
POST_RIDE_STRETCHES = [
    ("WARM_UP", "STRETCH_LUNGING_HIP_FLEXOR", 90,
     "弓步式髋屈肌拉伸：左右各 45s，骨盆后倾别前倾"),
    ("WARM_UP", "STRETCH_QUAD", 90, "股四头肌拉伸：左右各 45s"),
    ("WARM_UP", "STRETCH_HAMSTRING", 90, "腘绳肌拉伸：左右各 45s，别弓腰代偿"),
    ("WARM_UP", "STRETCH_CALF", 90, "腓肠肌/比目鱼肌拉伸：左右各 45s"),
    ("WARM_UP", "GLUTES_STRETCH", 90, "臀大肌拉伸：左右各 45s"),
    ("WARM_UP", "STRETCH_LYING_PIRIFORMIS", 90, "梨状肌拉伸：左右各 45s"),
]

# 力量课热身 —— 动态激活动作，同样绑定官方库（手表带动画示范）。
# 单组完成，不计入主项组数。力量A 在骑后接做，所以只做"唤醒"不做升温。
STRENGTH_WARMUP_A = [
    ("WARM_UP", "CAT_CAMEL", 8, "猫驼式：脊柱逐节活动，唤醒腰背"),
    ("HIP_STABILITY", "HIP_CIRCLES", 10, "髋绕圈：左右各 10 次，打开髋关节"),
    ("WARM_UP", "FORWARD_AND_BACKWARD_LEG_SWINGS", 10, "前后摆腿：左右各 10 次"),
    ("BANDED_EXERCISES", "LATERAL_BAND_WALKS", 10, "弹力带侧向走：每侧 10 步，唤醒臀中肌"),
]
STRENGTH_WARMUP_B = [
    ("WARM_UP", "CAT_CAMEL", 8, "猫驼式：脊柱逐节活动"),
    ("HIP_STABILITY", "HIP_CIRCLES", 10, "髋绕圈：左右各 10 次"),
    ("WARM_UP", "OPPOSITE_ARM_AND_LEG_BALANCE", 10, "对侧手脚平衡：左右各 10 次，核心+平衡"),
    ("WARM_UP", "WALKING_KNEE_HUGS", 10, "行走抱膝：左右各 10 次，臀+腘绳激活"),
]

# category -> {exercise keys} from the bundled FIT catalogue, used to pick the
# WEIGHTED_* variant of a movement once a load is attached.
_EX_BY_CAT = {}
try:
    from garminconnect import exercises as _gx
    for _e in _gx.EXERCISES:
        _EX_BY_CAT.setdefault(_e["category"], set()).add(_e["exercise"])
except Exception:  # catalogue unavailable -> keep base variants
    pass


def _weighted_variant(category, exercise):
    """Return the ``WEIGHTED_*`` form of a movement when one exists."""
    if exercise.startswith("WEIGHTED_"):
        return exercise
    candidate = "WEIGHTED_" + exercise
    return candidate if candidate in _EX_BY_CAT.get(category, set()) else exercise


def _strength_exercise_step(order, category, exercise, reps=None, secs=None,
                            desc=None, weight_kg=None):
    """One strength movement, bound to Garmin's exercise library.

    Pass ``reps`` for rep-based movements, ``secs`` for time-based holds.
    """
    if secs is not None:
        end = {"conditionTypeId": 2, "conditionTypeKey": "time",
               "displayOrder": 2, "displayable": True}
        value = float(secs)
    else:
        end = {"conditionTypeId": 10, "conditionTypeKey": "reps",
               "displayOrder": 10, "displayable": True}
        value = float(reps)
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "childStepId": 0,
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
        "description": desc,
        "endCondition": end,
        "endConditionValue": value,
        "targetType": NO_TARGET,
        "strokeType": {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0},
        "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0},
        "category": category,
        "exerciseName": exercise,
        "weightValue": float(weight_kg) * 1000.0 if weight_kg is not None else None,
        "weightUnit": dict(WEIGHT_KG) if weight_kg is not None else None,
    }
    return step


def _strength_rest_step(order, secs):
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "childStepId": 0,
        "stepType": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
        "description": None,
        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time",
                         "displayOrder": 2, "displayable": True},
        "endConditionValue": float(secs),
        "targetType": NO_TARGET,
        "strokeType": {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0},
        "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0},
    }


def _strength_block(order, category, exercise, sets, reps=None, secs=None,
                    rest=60, desc=None, weight_kg=None):
    """A "N sets" block. Returns (block, next_free_step_order)."""
    block = {
        "type": "RepeatGroupDTO",
        "stepOrder": order,
        "childStepId": 0,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        "numberOfIterations": sets,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations",
                         "displayOrder": 7, "displayable": False},
        "endConditionValue": float(sets),
        "skipLastRestStep": True,
        "workoutSteps": [
            _strength_exercise_step(order + 1, category, exercise,
                                    reps=reps, secs=secs, desc=desc,
                                    weight_kg=weight_kg),
            _strength_rest_step(order + 2, rest),
        ],
    }
    return block, order + 3


def _strength_steps(order, blocks):
    """Expand a list of block specs into steps, keeping stepOrder unique.

    Each spec is a dict::

        {"ex": (CATEGORY, EXERCISE), "sets": 3, "reps": 10,   # or "secs": 45
         "rest": 90, "kg": 20, "cue": "…"}

    When ``kg`` is set the movement is automatically upgraded to its
    ``WEIGHTED_*`` variant (if the library has one) so the watch animation
    matches the loaded version.
    """
    steps = []
    for spec in blocks:
        category, exercise = spec["ex"]
        kg = spec.get("kg")
        if kg is not None:
            exercise = _weighted_variant(category, exercise)
        kw = {"reps": spec["reps"]} if "reps" in spec else {"secs": spec["secs"]}
        block, order = _strength_block(order, category, exercise, spec["sets"],
                                       rest=spec.get("rest", 60),
                                       desc=spec.get("cue"), weight_kg=kg, **kw)
        steps.append(block)
    return steps, order


def _stretch_steps(order, stretches):
    """Timed stretch steps bound to the library, played as a cooldown flow."""
    steps = []
    for category, exercise, secs, cue in stretches:
        steps.append(_strength_exercise_step(order, category, exercise,
                                             secs=secs, desc=cue))
        order += 1
    return steps, order


def _warmup_steps(order, moves):
    """Single-set activation movements, bound to the library so the watch
    animates them too. Same tuple shape as ``_stretch_steps`` but rep based."""
    steps = []
    for category, exercise, reps, cue in moves:
        steps.append(_strength_exercise_step(order, category, exercise,
                                             reps=reps, desc=cue))
        order += 1
    return steps, order


def _workout(name, desc, sport, steps, duration):
    return {
        "workoutName": name,
        "description": desc,
        "sportType": sport,
        "estimatedDurationInSecs": duration,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": sport,
            "workoutSteps": steps,
        }],
    }


def _zone(ftp, lo, hi):
    """Return (lo_w, hi_w) integers for a FTP percentage band."""
    return round(ftp * lo / 100), round(ftp * hi / 100)


def build_plan(ftp, hot=False, state=None):
    """Return list of (weekday, date_str, workout_payload) for the current week.

    Polarized 80/20 layout with Tue & Thu as FULL REST days (no workout
    scheduled = rest). Mirrors analyze_training.py's detailed weekly plan:
      Mon 阈值 Z4 | Tue 休息 | Wed VO2max Z5 | Thu 休息
      | Fri Z2 恢复骑 + 力量A | Sat 长距离 Z2 | Sun 力量B
    (Tue/Thu are intentionally absent from the plan -> rest days.)

    结构与数值全部来自 *state*（coach_state 的推进结果）：
      * 质量课的组数×时长 —— 连续两周满组才升档，末组崩盘则退档
      * 逐动作负重 —— 连续两周完成力量课才加重
      * 周六长骑时长与形态 —— 随阶段与完成度增长，build/peak 插入爬坡专项
      * 力量模式 —— full（周五 A + 周日 B）／single（只有周五 A）／
        minimal（只有周日 B 精简版）；由 state 决定，本文件只读
    因此本文件不持有任何训练参数，改参数请改 state 文件。
    """
    st = state or coach_state.DEFAULT_STATE
    phase = st["goal"].get("phase", "base")
    thr_sets, thr_reps = coach_state.quality_of(st, "threshold")
    vo2_sets, vo2_reps = coach_state.quality_of(st, "vo2")
    sp = coach_state.strength_plan(st)
    lng_min = coach_state.planned_long_ride_min(st, phase)
    # 长骑形态由 analyze_training.py 在推进状态时定好并写入 state ——
    # 本脚本只读决定，不重算（推送脚本没有 TSB，重算必然与报告不一致）。
    variant = st["goal"].get("long_ride_variant", "z2")

    # 自主神经闸门：advance() 把结论写进 state.gate（见 coach_state）。
    # 只有「本周」的记录才生效 —— 旧周的 blocked 不能残留到新一周。
    _gate = st.get("gate") or {}
    gated = bool(_gate.get("blocked")) and \
        _gate.get("week") == coach_state.week_key(datetime.now())

    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    d = lambda off: (monday + timedelta(days=off)).strftime("%Y-%m-%d")

    thr_lo, thr_hi = _zone(ftp, 94, 101)      # 阈值 Z4
    z2r_lo, z2r_hi = _zone(ftp, 56, 68)       # Z2 恢复骑
    vo2_lo, vo2_hi = _zone(ftp, 110, 118)     # VO2max Z5
    z1_lo, z1_hi = _zone(ftp, 45, 55)         # Z1 恢复/热身前段（Erg 自动跟随用）
    if hot:
        lng_lo, lng_hi = _zone(ftp, 50, 64)   # 长骑 Z2 热天下调
    else:
        lng_lo, lng_hi = _zone(ftp, 55, 70)   # 长骑 Z2 正常

    plan = []

    # 力量三档位：看「这一档有没有那节课」（A/B 是否为空），不再比 mode 字符串。
    _has_a, _has_b = bool(sp["A"]), bool(sp["B"])

    # Mon: threshold quality session — 组×时长与档位来自 state
    plan.append((
        0, d(0),
        _workout(
            f"{NAME_PREFIX}·阈值课 {thr_sets}×{thr_reps}min",
            f"周一：热身15min + {thr_sets}×({thr_reps}min @{thr_lo}-{thr_hi}W + 5min恢复) "
            f"+ 放松10min，从下沿 {thr_lo}W 起步",
            CYCLING,
            [
                _step(1, 480, "warmup", 1, POWER, float(z1_lo), float(z1_hi),
                      desc=f"热身前段 Z1 {z1_lo}-{z1_hi}W 轻松踩踏"),
                _step(2, 420, "warmup", 1, POWER, float(z2r_lo), float(z2r_hi),
                      desc=f"热身后段 Z2 {z2r_lo}-{z2r_hi}W 激活"),
                _repeat(3, thr_sets, [
                    _step(1, thr_reps * 60, "interval", 3, POWER,
                          float(thr_lo), float(thr_hi),
                          desc=f"阈值 {thr_lo}-{thr_hi}W（从下沿起步）"),
                    _step(2, 300, "recovery", 4, POWER, float(z1_lo), float(z1_hi),
                          desc=f"组间恢复 Z1 {z1_lo}-{z1_hi}W"),
                ]),
                _step(4, 600, "cooldown", 2, POWER, float(z1_lo), float(z1_hi),
                      desc=f"放松冷却 Z1 {z1_lo}-{z1_hi}W"),
            ],
            900 + thr_sets * (thr_reps * 60 + 300) + 600,
        ),
    ))

    # Wed: VO2max Z5 intervals (quality session moved here so it sits 2 days
    # before the Saturday long ride, separated by Thu rest + Fri Z2)
    plan.append((
        2, d(2),
        _workout(
            f"{NAME_PREFIX}·VO2max间歇 {vo2_sets}×{vo2_reps}min",
            f"周三：热身15min + {vo2_sets}×({vo2_reps}min @{vo2_lo}-{vo2_hi}W + 3min恢复) "
            f"+ 放松10min，首组从 {vo2_lo}W 起步",
            CYCLING,
            [
                _step(1, 480, "warmup", 1, POWER, float(z1_lo), float(z1_hi),
                      desc=f"热身前段 Z1 {z1_lo}-{z1_hi}W 轻松踩踏"),
                _step(2, 420, "warmup", 1, POWER, float(z2r_lo), float(z2r_hi),
                      desc=f"热身后段 Z2 {z2r_lo}-{z2r_hi}W 激活"),
                _repeat(3, vo2_sets, [
                    _step(1, vo2_reps * 60, "interval", 3, POWER,
                          float(vo2_lo), float(vo2_hi),
                          desc=f"VO2max {vo2_lo}-{vo2_hi}W"),
                    _step(2, 180, "recovery", 4, POWER, float(z1_lo), float(z1_hi),
                          desc=f"组间恢复 Z1 {z1_lo}-{z1_hi}W"),
                ]),
                _step(4, 600, "cooldown", 2, POWER, float(z1_lo), float(z1_hi),
                      desc=f"放松冷却 Z1 {z1_lo}-{z1_hi}W"),
            ],
            900 + vo2_sets * (vo2_reps * 60 + 180) + 600,
        ),
    ))

    # ── 闸门触发：两节质量课整体换成 Z2 ───────────────────────────────────
    # 报告在闸门周渲染的是「周一/周三 Z2 有氧」，手表必须一致。
    # 2026-10-19 实测：只堵住长骑（10/12 的修法）不够，周一阈值 / 周三 VO2max
    # 仍被推上手表 —— 用户早上打开 Garmin 看到的还是强度课。
    if gated:
        def _z2_alt(label: str, note: str):
            return _workout(
                f"{NAME_PREFIX}·Z2有氧（{label}）",
                f"{label}：自主神经闸门触发 → 原质量课取消。热身10min + 50min "
                f"@{z2r_lo}-{z2r_hi}W（压住 Z2，硬上限 150W） + 放松10min；{note}",
                CYCLING,
                [
                    _step(1, 480, "warmup", 1, POWER, float(z1_lo), float(z1_hi),
                          desc=f"热身前段 Z1 {z1_lo}-{z1_hi}W 轻松踩踏"),
                    _step(2, 420, "warmup", 1, POWER, float(z2r_lo), float(z2r_hi),
                          desc=f"热身后段 Z2 {z2r_lo}-{z2r_hi}W 激活"),
                    _step(3, 3000, "main", 8, POWER, float(z2r_lo), float(z2r_hi),
                          desc=f"Z2 {z2r_lo}-{z2r_hi}W（本周不碰强度）"),
                    _step(4, 600, "cooldown", 2, POWER, float(z1_lo), float(z1_hi),
                          desc=f"放松 Z1 {z1_lo}-{z1_hi}W"),
                ],
                4500,
            )

        plan = [p for p in plan if p[0] not in (0, 2)]
        plan.insert(0, (0, d(0), _z2_alt(
            "周一", "HRV/RHR 未恢复，别用加时长补回取消的强度")))
        plan.insert(1, (2, d(2), _z2_alt(
            "周三", "同周一，只维持有氧；心率 >160 降功率，但别掉出 Z2 下限")))

    # Fri: Z2 recovery ride (+ strength A in full / single mode). Two workouts same
    # day in full mode: the Z2 ride then strength A, per the report.
    plan.append((
        4, d(4),
        _workout(
            f"{NAME_PREFIX}·Z2恢复骑",
            f"周五：热身10min + 60-90min @{z2r_lo}-{z2r_hi}W（死守Z2，踏频85-90） + 放松10min"
            + ("；课后接力量A" if _has_a
               else "（本周力量在周日 · B 精简版）"),
            CYCLING,
            [
                _step(1, 360, "warmup", 1, POWER, float(z1_lo), float(z1_hi),
                      desc=f"热身前段 Z1 {z1_lo}-{z1_hi}W 轻松踩踏"),
                _step(2, 240, "warmup", 1, POWER, float(z2r_lo), float(z2r_hi),
                      desc=f"热身后段 Z2 {z2r_lo}-{z2r_hi}W 激活"),
                _step(3, 4500, "main", 8, POWER, float(z2r_lo), float(z2r_hi),
                      desc=f"Z2 {z2r_lo}-{z2r_hi}W（压住Z2）"),
                _step(4, 600, "cooldown", 2, POWER, float(z1_lo), float(z1_hi),
                      desc=f"放松冷却 Z1 {z1_lo}-{z1_hi}W"),
            ],
            5700,
        ),
    ))
    # Fri: strength A (main) — 在 full / single 两档下都推（single 档它就是本周
    # 唯一那一次力量）。每个动作绑定 Garmin 官方动作库，负重来自 state。
    if _has_a:
        warm_A, after_warm_A = _warmup_steps(1, STRENGTH_WARMUP_A)
        sA_steps, next_order = _strength_steps(after_warm_A, sp["A"])
        stretch_A, _ = _stretch_steps(next_order, POST_RIDE_STRETCHES)
        plan.append((
            4, d(4),
            _workout(
                f"{NAME_PREFIX}·力量训练A",
                "周五（主力量A，45-50min，骑后接做）：热身为 4 个官方动态激活动作（各带动画示范）→ "
                + coach_state.describe_movements(sp["A"]).replace("**", "")
                + "，动作质量>负荷；收尾 9min 拉伸流程（髋屈肌/股四/腘绳/腓肠/臀/梨状肌）",
                STRENGTH,
                warm_A + sA_steps + stretch_A,
                2700,
            ),
        ))

    # Sat: long endurance ride, heat-adjusted (Z2 base, never drift to Z3).
    # build/peak phases swap in the hill-repeat specific course.
    if variant == "hills":
        sat_name = f"{NAME_PREFIX}·长距离耐力（爬坡专项）"
        sat_desc = (f"周六：热身10min + 爬坡专项：5-7km/约5% 坡重复 4-6 次，上坡守 Z2 "
                    f"{lng_lo}-{lng_hi}W 不冲，下坡顺骑恢复，后段平路磨完 {lng_min}min "
                    f"+ 放松10min；核心是「长时间爬坡的有氧耐受」")
    else:
        sat_name = f"{NAME_PREFIX}·长距离耐力"
        sat_desc = (f"周六：热身10min + {lng_min}min @{lng_lo}-{lng_hi}W"
                    f"（死守Z2，热天按功率配速HR≤160） + 放松10min")
    # 主体段：纯 Z2 是一整段；爬坡专项则拆成 4 段等长的爬坡（上坡守 Z2）
    if variant == "hills":
        main_step = _repeat(3, 4, [
            _step(1, (lng_min * 60) // 4, "interval", 3, POWER,
                  float(lng_lo), float(lng_hi),
                  desc=f"爬坡段 Z2 {lng_lo}-{lng_hi}W（不冲、不摇车）"),
        ])
    else:
        main_step = _step(3, lng_min * 60, "main", 8, POWER,
                          float(lng_lo), float(lng_hi),
                          desc=f"长距离 Z2 {lng_lo}-{lng_hi}W")

    plan.append((
        5, d(5),
        _workout(
            sat_name, sat_desc, CYCLING,
            [
                _step(1, 360, "warmup", 1, POWER, float(z1_lo), float(z1_hi),
                      desc=f"热身前段 Z1 {z1_lo}-{z1_hi}W 轻松踩踏"),
                _step(2, 240, "warmup", 1, POWER, float(lng_lo), float(lng_hi),
                      desc=f"热身后段 Z2 {lng_lo}-{lng_hi}W 激活"),
                main_step,
                _step(6, 600, "cooldown", 2, POWER, float(z1_lo), float(z1_hi),
                      desc=f"放松冷却 Z1 {z1_lo}-{z1_hi}W"),
            ],
            600 + lng_min * 60 + 600,
        ),
    ))

    # Sun: strength B（full 档）或 B 精简版（minimal 档）；single 档本周只有周五
    # 那一次力量，周日不推任何课 —— 推一节空课/重复课只会让日历变噪音。
    if _has_b:
        warm_B, after_warm_B = _warmup_steps(1, STRENGTH_WARMUP_B)
        sB_steps, next_order_b = _strength_steps(after_warm_B, sp["B"])
        stretch_B, _ = _stretch_steps(next_order_b, POST_RIDE_STRETCHES)
        if sp["mode"] == "minimal":
            sun_name = f"{NAME_PREFIX}·力量训练B（精简版·本周唯一力量课）"
            sun_desc = ("周日（精简版，20-25min）：断档后只求完成一次 → "
                        + coach_state.describe_movements(sp["B"]).replace("**", "")
                        + "；重量轻无所谓，做完即达标")
        else:
            sun_name = f"{NAME_PREFIX}·力量训练B"
            sun_desc = ("周日（力量B，35-40min）：热身为 4 个官方动态激活动作（各带动画示范）→ "
                        "核心稳定 + 髋外展 + 单腿平衡（"
                        + coach_state.describe_movements(sp["B"]).replace("**", "")
                        + "），左腿优先，轻负荷；收尾 9min 拉伸流程 + 鹅足放松")
        plan.append((
            6, d(6),
            _workout(sun_name, sun_desc, STRENGTH, warm_B + sB_steps + stretch_B, 2160),
        ))
    return plan



def _read_report():
    """Return (ftp, hot) parsed from the latest training report."""
    ftp, hot = 218.0, False
    try:
        with open(os.path.join(OUTPUT_DIR, "training_report.md")) as f:
            text = f.read()
        m = re.search(r"估算FTP≈(\d+)W", text)
        if m:
            ftp = float(m.group(1))
        hot = "热天下调" in text  # report uses this phrase only on heat weeks
    except Exception:
        pass
    return ftp, hot


def _login_with_retry(client, attempts=3, backoff=45):
    """Log in, retrying transient Garmin-side failures.

    Garmin's API regularly returns Cloudflare 5xx with ``retryable: true``
    (e.g. 521 origin down) during short outages; a single attempt would make
    the whole weekly push fail. Backs off linearly between attempts.
    """
    last = None
    for n in range(1, attempts + 1):
        try:
            client.login(os.path.expanduser("~/.garminconnect"))
            return True
        except Exception as e:
            last = e
            msg = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
            if n < attempts:
                wait = backoff * n
                print(f"⚠️ 登录失败（第 {n}/{attempts} 次）：{msg}")
                print(f"   {wait}s 后重试（Garmin 侧临时故障通常 1-2 分钟内恢复）…")
                time.sleep(wait)
            else:
                print(f"❌ 登录失败（第 {n}/{attempts} 次）：{msg}", file=sys.stderr)
    print(f"❌ 无法连接 Garmin Connect: {last}", file=sys.stderr)
    return False


def main():
    dry_run = "--dry-run" in sys.argv
    client = Garmin()
    if not _login_with_retry(client):
        sys.exit(1)

    ftp, hot = _read_report()
    state = coach_state.load()

    # FTP：以 state.ftp.current 为准（它是手动校准过的值），报告估算只作兜底。
    # 此前直接用报告估算是错的 —— 估算系统性偏高约 5W，会把 Z2 顶到节奏区。
    st_ftp = coach_state.ftp_of(state)
    if st_ftp:
        if abs(st_ftp - ftp) >= 1:
            print(f"ℹ️ 使用 state 校准的 FTP {st_ftp:.0f}W（报告估算 {ftp:.0f}W）")
        ftp = st_ftp

    phase = state["goal"].get("phase", "base")
    thr_sets, thr_reps = coach_state.quality_of(state, "threshold")
    _gate = state.get("gate") or {}
    _gated_now = bool(_gate.get("blocked")) and \
        _gate.get("week") == coach_state.week_key(datetime.now())
    print(f"ℹ️ 状态：阶段={coach_state.PHASE_LABEL.get(phase, phase)} · "
          f"{'🔴 闸门触发→全 Z2' if _gated_now else f'阈值课 {thr_sets}×{thr_reps}min'} · "
          f"力量模式={state['strength']['mode']} · "
          f"长骑 {coach_state.planned_long_ride_min(state, phase)}min"
          f"/{state['goal'].get('long_ride_variant', 'z2')}")

    plan = build_plan(ftp, hot, state)
    current_names = {p[2]["workoutName"] for p in plan}

    # current week range (Mon–Sun), dynamic
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    start_s, end_s = monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")

    # Existing templates with our prefix. A name can map to MULTIPLE ids
    # (older runs left duplicates behind), so keep every id — otherwise the
    # leftover twin keeps its own schedule and we end up with two sessions
    # on the same day.
    existing = {}       # name -> newest workoutId (for reference)
    existing_all = {}   # name -> [all workoutIds]
    try:
        for w in client.get_workouts(0, 300):
            name = w.get("workoutName", "")
            if name.startswith(NAME_PREFIX):
                existing_all.setdefault(name, []).append(w["workoutId"])
                existing[name] = w["workoutId"]
    except Exception as e:
        print(f"⚠️ 获取已有训练模板失败: {e}")

    our_ids = {wid for ids in existing_all.values() for wid in ids}

    # Remove stale templates (renamed/moved sessions from earlier weeks)
    for name, ids in list(existing_all.items()):
        if name in current_names:
            continue
        for wid in ids:
            if dry_run:
                print(f"🔍 [dry-run] 将删除旧模板 {wid}: {name}")
                continue
            try:
                client.delete_workout(wid)
                print(f"🗑️  已删除旧模板 {wid}: {name}")
            except Exception as e:
                print(f"⚠️ 删除旧模板失败 {name}: {e}")

    # This week's scheduled items (our templates + adaptive plan items).
    # NOTE: get_scheduled_workouts(yr, mo) returns an overlapping window, so
    # the same calendar item can come back twice — dedupe by schedule id.
    week_items = []
    plan_items = {}
    seen_sched = set()
    # Garmin's own adaptive plan ("fbtAdaptiveWorkout", e.g. 基础训练 /
    # 全身循环3 / 核心循环2) also lands on the calendar. It is NOT ours: never
    # unschedule it — but report it, otherwise our session silently collides
    # with it on the same day. One calendar id comes back twice with different
    # payloads (a cycling entry and a strength entry), so dedupe on both.
    seen_fbt = set()
    try:
        months = {(monday.year, monday.month), (sunday.year, sunday.month)}
        for yr, mo in sorted(months):
            sched = client.get_scheduled_workouts(yr, mo)
            for i in sched.get("calendarItems", []):
                date = i.get("date") or ""
                if not (start_s <= date <= end_s):
                    continue
                if i.get("itemType") == "fbtAdaptiveWorkout":
                    key = (i.get("id"), i.get("title"))
                    if key in seen_fbt:
                        continue
                    seen_fbt.add(key)
                    title = i.get("title") or "<无标题>"
                    if title not in plan_items.setdefault(date, []):
                        plan_items[date].append(title)
                    continue
                if i.get("id") in seen_sched:
                    continue
                seen_sched.add(i.get("id"))
                # Only real scheduled workouts are ours to manage. The calendar
                # feed also carries recorded activities (itemType "activity",
                # e.g. a ride Garmin matched to the planned workout — its title
                # becomes the workout name and workoutId is null) and naps.
                # Never try to unschedule those: it would touch the user's
                # recorded data instead of a schedule entry.
                if i.get("itemType") != "workout":
                    continue
                title = i.get("title") or ""
                if i.get("workoutId"):
                    week_items.append(i)
                elif title.startswith(NAME_PREFIX):
                    # Orphan: our session whose template was deleted, so Garmin
                    # dropped the workoutId but kept the calendar entry. It must
                    # be cleared too, otherwise the day shows two sessions.
                    week_items.append(i)
                elif title:
                    plan_items.setdefault(date, []).append(title)
    except Exception as e:
        print(f"⚠️ 获取本周排期失败: {e}")

    # Unschedule our previous placements so the new layout replaces them.
    # Match on our template ids (incl. duplicates) so no stray session
    # survives; orphaned entries (no workoutId, our title prefix) go too.
    for i in week_items:
        wid = i.get("workoutId")
        title = i.get("title") or ""
        if not (wid in our_ids or (wid is None and title.startswith(NAME_PREFIX))):
            continue
        if dry_run:
            print(f"🔍 [dry-run] 将取消排期 {i.get('date')}: {i.get('title')}")
            continue
        try:
            client.unschedule_workout(i["id"])
            print(f"↩️  已取消排期 {i.get('date')}: {i.get('title')}")
        except Exception as e:
            print(f"⚠️ 取消排期失败 {i.get('date')} {i.get('title')}: {e}")

    if plan_items:
        print("\nGarmin 自适应计划本周仍有排期（未改动）:")
        for date in sorted(plan_items):
            print(f"  {date}: {', '.join(plan_items[date])}")

    created = []
    for _, date, payload in plan:
        name = payload["workoutName"]
        if dry_run:
            verb = "重建（内容更新）" if name in existing else "创建"
            print(f"🔍 [dry-run] 将{verb}并排期 {date}: {name}")
            continue
        try:
            # Always re-upload so the workout content (e.g. power targets)
            # stays current; ALL old templates of the same name are removed
            # first so the name can never accumulate duplicates.
            for old in existing_all.get(name, []):
                try:
                    client.delete_workout(old)
                    print(f"🗑️  删除旧模板 {old}：{name}")
                except Exception as e:
                    print(f"⚠️ 删除旧模板失败 {name}: {e}")
            res = client.upload_workout(payload)
            wid = res["workoutId"]
            existing[name] = wid
            existing_all[name] = [wid]
            print(f"✅ 已创建模板 {wid}：{name}")
            client.schedule_workout(wid, date)
            print(f"📅 已排期 {date}: {name}")
            created.append((date, name))
        except Exception as e:
            print(f"❌ 推送失败 {date} {name}: {e}")

    if created:
        # 力量那几天按档位说，别再写死「周五力量A / 周日力量B」——
        # single 档周日是没有课的，写死会让人以为推漏了。
        _strength_days = {
            "full": "周五Z2+力量A / 周日力量B",
            "single": "周五Z2+力量A（本周唯一力量课）",
            "minimal": "周五Z2 / 周日力量B精简版",
        }.get(state["strength"]["mode"], "周五Z2+力量A / 周日力量B")
        _quality_days = ("周一Z2有氧 / 周三Z2有氧（闸门触发·强度课已取消）"
                         if _gated_now else "周一阈值 / 周三VO2max")
        print(f"\n🎉 课表已按极化布局推送（周二/周四休息）："
              f"{_quality_days} / {_strength_days} / 周六长距离")
    elif not dry_run:
        print("\n（没有新增推送）")


if __name__ == "__main__":
    main()

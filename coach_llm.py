#!/usr/bin/env python3
"""大模型教练点评 —— 让报告的「解释」部分也说人话。

═══ 安全边界（这是本模块最重要的设计约束）═══

模型**只写散文，不做任何数值决策**。

所有会影响训练的数值 —— 质量课组数、逐动作负重、长骑时长、周 TSS 上限、
爬坡专项是否插入 —— 全部由 coach_state.py 的确定性规则算出。模型拿到的是
这些**已经算好的结果**，它只负责把它们串成一段有context的说明，指出风险和
优先级。

这样设计的原因很直接：训练负荷是会伤人的。一个会漂移、会幻觉、会因为
prompt 措辞不同而给出不同 5kg 负重的组件，不该出现在「下周推什么课」的
决策链上。它可以是很好的解说员，但不能是教练本人。

模型只在报告里出现一次，输出失败/超时/没有 API key 都只是**少一段话**，
不会影响报告其余部分，更不会影响课表推送。

═══ 思考模式（2026-10-09 起默认开启）═══

DeepSeek V4 **默认就开思考模式**，且思考内容（`reasoning_content`）**按输出
token 计费** —— 实测 `completion_tokens` = 1028 = 思考 820 + 正文 208，
两者都算在输出里。

用环境变量 `LLM_THINKING=enabled|disabled` 切换，默认 `enabled`。做成环境变量
而不是写死，是因为这两种模式在成本/延迟/质量上的取舍以后还会变，不该为了切换
它改一次代码、再走一遍推送。

实测对比（同一份输入）：

                关闭思考    开启思考
    输出 tokens      238      1028（含思考 820）
    耗时             ~4s       5.4s
    单次成本      ¥0.0021    ¥0.0055

═══ 成本（实测口径，2026-10-09 核价）═══

模型 deepseek-flash，空闲时段（缓存未命中）：

    输入 1053 × $0.15/1M ≈ $0.000158
    输出 1028 × $0.60/1M ≈ $0.000617      ← 含思考 820
    单次合计 ≈ $0.00078 ≈ ¥0.0055

每周跑一次 → 一年 52 次 ≈ **¥0.29**。

三点注意：
  * **思考会挤占 max_tokens**。实测思考用掉 820 tokens，所以 MAX_TOKENS 必须
    远大于「正文长度」—— 否则正文会被截断，而截断的表象是「内容为空」，
    很容易被误判成模型没返回。这里留了约 4 倍余量。
  * **思考模式下 `temperature` 无效**（官方：不报错但被忽略）。所以 payload 里
    只在非思考模式才带它 —— 留着一个永远不生效的参数，是给下一个人挖坑。
  * **高峰时段价格翻倍**（UTC 01:00-04:00、06:00-10:00，周一至周五，
    中国法定节假日除外）。云端 cron 定在周一 00:45 UTC，若 GitHub 的
    5-30 分钟漂移把它推到 01:00 之后，就按高峰计费 —— 单次约 ¥0.011，
    差额可忽略，但知道一下没坏处。
  * 每周一次的频率意味着**上下文缓存帮不上忙**（系统提示词那 287 tokens
    虽然每轮完全相同，但缓存早就过期了）。实测缓存命中为 0。
"""
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com"
# ⚠️ 必须用 deepseek-flash。旧的 deepseek-chat / deepseek-reasoner 已于
# 2026-07-24 15:59 UTC 正式下线且**完全不可访问**，用旧名会直接报错。
DEFAULT_MODEL = "deepseek-flash"

# 思考模式。DeepSeek V4 默认开启，这里跟随默认；用 LLM_THINKING=disabled 关掉。
DEFAULT_THINKING = "enabled"
# 官方取值 low / high / max（minimal→low，medium/xhigh→high，ultra→max）。
DEFAULT_REASONING_EFFORT = "high"

# ⚠️ 思考模式下 reasoning_content **按输出计费**，实测占 820 tokens。
# 这个上限必须覆盖「思考 + 正文」的总和，否则正文会被截断，而截断的表象是
# 「内容为空」—— 极容易被误判成模型没返回。实测思考 820，这里留约 4 倍余量。
MAX_TOKENS = 4000
# 思考会拉长延迟（实测 5.4s，非思考约 4s）。90s 给足余量，这一步不在关键路径上。
TIMEOUT = 90
RETRY_DELAYS = (5, 20)

SYSTEM_PROMPT = """你是一位经验丰富的自行车耐力教练，正在给一位有功率计的车手写周报点评。

车手背景（务必纳入判断）：
- 目标是「单日 200km + 3500m 爬坡」，遵循极化 80/20 训练（大量 Z2 + 每周 1-2 次高质量课）。
- 最大短板是力量训练，历史上反复连续数周 0 次；力量课已绑定 Garmin 官方动作库。
- 骑行环境常高温高湿，心率天然偏高，判读强度**一律以功率为准**，不要因为心率高就建议降功率。
- 9/27 完成 bike fitting：座垫升高、落差加大到 73mm（较激进）、曲柄待换 165、把宽待换 380；
  左腿踩踏占比 48%（偏弱），左膝鹅足敏感 → 力量重心是臀大肌+髂腰肌+核心。
- 性格务实、懂功率训练、讨厌空话。直接给结论和优先级。

写作要求：
1. 用简体中文，Markdown，**3-5 条要点**，总长不超过 250 字。
2. 只评论「下方提供的数据」；**绝对不要编造任何数字**，不要给新的功率值、负重值或 TSS 值 ——
   这些已经由系统算好了，你的任务是解释和提醒，不是重新开处方。
3. 如果数据里有风险信号（依从性差、疲劳累积、目标临近但基础缺口大），要**直接点出来**，
   不要粉饰，也不要机械地鼓励。
4. 如果一切正常，简短肯定即可，不要凑字数。
5. 不要复述整张表，只挑最值得注意的。"""


def _api_key():
    return (os.environ.get("LLM_API_KEY") or "").strip()


def build_payload(state, context, model, stream=False):
    """把状态与上周事实整理成给模型的上下文。

    刻意只喂**已定稿的结论**（决策日志 + 关键指标），而不是原始活动流水 ——
    模型能看到的信息和报告读者一致，避免它从原始数据里二次推断出别的结论。
    """
    facts = {
        "目标": {
            "名称": state["goal"].get("name"),
            "日期": state["goal"].get("date"),
            "当前阶段": state["goal"].get("phase"),
            "长骑形态": state["goal"].get("long_ride_variant", "z2"),
        },
        "上周实际完成": context.get("compliance", {}),
        "本周身体状态": context.get("body", {}),
        "本周已定的调整（决策日志，勿推翻，只做解释与提醒）": state.get("decisions", []),
        "当前训练参数": {
            "FTP使用值": state["ftp"].get("current"),
            "报告估算FTP": state["ftp"].get("estimate"),
            "阈值课": f"{state['quality']['threshold']['sets']}×"
                      f"{state['quality']['threshold']['reps']}min",
            "VO2max课": f"{state['quality']['vo2']['sets']}×"
                        f"{state['quality']['vo2']['reps']}min",
            "力量模式": state["strength"]["mode"],
            "力量连续断档周数": state["strength"]["consecutive_missed"],
            "负重kg": state["strength"]["weights"],
            "长骑基准min": state["endurance"]["long_ride_min"],
            "周TSS上限": context.get("tss_cap"),
            "本周周TSS估算": context.get("week_tss"),
        },
        "近几周趋势": context.get("weeks", []),
    }
    user = ("以下是这位车手本周的训练状态与系统已经做出的调整。"
            "请按系统提示的要求，写一段点评。\n\n```json\n"
            + json.dumps(facts, ensure_ascii=False, indent=2)
            + "\n```")
    thinking = (os.environ.get("LLM_THINKING") or DEFAULT_THINKING).strip().lower()
    if thinking not in ("enabled", "disabled"):
        thinking = DEFAULT_THINKING

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "max_tokens": MAX_TOKENS,
        # 官方格式是 ``{"thinking": {"type": "enabled"/"disabled"}}``，默认就是
        # enabled。这里始终**显式**写出来，哪怕值等于默认值 —— 哪天官方改了默认，
        # 我们不会跟着无声漂移。
        "thinking": {"type": thinking},
        "stream": stream,
    }
    if thinking == "enabled":
        effort = (os.environ.get("LLM_REASONING_EFFORT")
                  or DEFAULT_REASONING_EFFORT).strip().lower()
        payload["reasoning_effort"] = effort
        # ⚠️ 官方：思考模式**不支持** temperature / presence_penalty /
        # frequency_penalty，传了不报错但**完全不生效**。所以思考模式下干脆
        # 不带这个字段，免得留一个看着有用、其实被忽略的参数。
    else:
        payload["temperature"] = 0.4
    return payload


def _call(payload, base_url, key, timeout):
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    text = (msg.get("content") or "").strip()
    reasoning = msg.get("reasoning_content") or ""
    return text, data.get("usage") or {}, choice.get("finish_reason"), len(reasoning)


def _log_usage(usage, reasoning_chars=0):
    """把真实用量打进 Actions 日志 —— 账单是抽象的，token 数是具体的。"""
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    if pt is None and ct is None:
        return
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    detail = f"输入 {pt} / 输出 {ct} tokens"
    if hit is not None:
        detail += f"（缓存命中 {hit} / 未命中 {miss}）"
    # 思考 token 就在输出里，单列出来才能一眼看出钱花在哪。
    rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if rt is None:
        rt = usage.get("reasoning_tokens")
    if rt:
        detail += f"，其中思考 {rt} tokens（{reasoning_chars} 字符）"
    print(f"📊 本次用量：{detail}")


def coach_narrative(state, context, prompt_only=False):
    """返回模型的点评（Markdown 片段），失败一律返回空串。

    prompt_only=True 时不发请求，只打印将要发送的 prompt（离线自测用）。
    """
    base_url = os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    payload = build_payload(state, context, model)

    if prompt_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return ""

    key = _api_key()
    if not key:
        print("ℹ️ 未配置 LLM_API_KEY —— 跳过模型点评（报告其余部分不受影响）")
        return ""

    last = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS, start=1):
        if delay:
            print(f"::warning::模型点评第 {attempt - 1} 次失败（{last}），{delay}s 后重试")
            import time
            time.sleep(delay)
        try:
            text, usage, finish, rchars = _call(payload, base_url, key, TIMEOUT)
            _log_usage(usage, rchars)
            if not text:
                # 空正文有两种成因，必须分开说 —— 否则「模型没返回」和
                # 「max_tokens 给少了」长得一模一样，排查会直接跑偏。
                # 两种情况重试都没意义。
                if finish == "length":
                    print(f"::warning::正文为空且被 max_tokens 截断"
                          f"（思考已消耗 {rchars} 字符）—— 调大 MAX_TOKENS，不重试")
                else:
                    print(f"::warning::模型返回了空正文"
                          f"（finish_reason={finish}）—— 跳过，不重试")
                return ""
            print(f"✅ 模型点评已生成（{len(text)} 字）")
            return text
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            last = f"HTTP {e.code} {detail}"
            # 这些错误重试没有意义，而且**必须显式报出来** —— 否则整条链路
            # 是静默降级，使用者只会看到「点评没出现」而不知道是模型名过期了。
            if e.code in (401, 403, 402):
                print(f"::warning::模型点评鉴权/额度失败（{last}）—— 跳过，不重试")
                return ""
            if e.code == 400:
                print(f"::warning::模型点评请求被拒（{last}）——"
                      f"多半是模型名失效或参数不被支持，检查 LLM_MODEL，不重试")
                return ""
            if e.code == 404:
                print(f"::warning::模型或端点不存在（{last}）—— 检查 LLM_MODEL / LLM_BASE_URL，不重试")
                return ""
        except Exception as e:
            last = f"{type(e).__name__}: {e}"

    print(f"::warning::模型点评最终失败（{last}）—— 跳过（报告其余部分不受影响）")
    return ""


def render_section(text):
    """把点评包成报告的独立段落。"""
    if not text:
        return ""
    return ("\n## 🧠 教练点评（模型生成·仅供参考）\n\n"
            "> 以下文字由大模型根据上方**已定的**训练安排生成，只做解释与提醒。\n"
            "> 所有训练数值与结构均由确定性规则算出，模型不参与决策。\n\n"
            + text.strip() + "\n\n")


if __name__ == "__main__":
    # 默认离线：只打印将要发送的 prompt，不发任何请求、不花钱。
    #   python3 coach_llm.py
    # 加 --live 则真调一次 API（需环境变量 LLM_API_KEY），用来在配置 secret
    # 之前先验证三件事：模型名是否有效、密钥是否可用、thinking 是否真的关掉
    # （从返回的 token 数就能看出来 —— 开着思考模式输出会明显偏大）。
    #   LLM_API_KEY=sk-xxx python3 coach_llm.py --live
    live = "--live" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import coach_state as cs  # noqa: E402

    st = cs.load()
    demo = {
        "compliance": {"力量": "0/2 次", "阈值课": "2/3 组达标", "长骑": "无记录"},
        "body": {"静息心率": 55, "睡眠小时": 7.2, "HRV": 68},
        "tss_cap": cs.tss_cap(st, st["goal"].get("phase", "base")),
        "week_tss": 262,
        "weeks": [{"周": "W40", "TSS": 126}, {"周": "W41", "TSS": 0}],
    }
    text = coach_narrative(st, demo, prompt_only=not live)
    if live:
        print(render_section(text) if text else "⚠️ 未生成任何内容")

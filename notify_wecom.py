#!/usr/bin/env python3
"""把周报摘要推送到企业微信群机器人（可经「微信插件」同步到个人微信）。

为什么是企业微信而不是个人微信
--------------------------------
个人微信没有官方推送 API。itchat / wechaty / iPad 协议这类个人号方案违反微信
用户协议、有封号风险，不用。企业微信的群机器人 Webhook 是官方能力，个人也能
免费注册企业微信（无需企业资质）。要在**个人微信**里收到，只需在
【管理后台 → 我的企业 → 微信插件】取二维码用微信扫码关注——官方文档确认
微信插件（原企业号）可以接收「应用消息和会话消息」，群机器人消息属于会话消息。

配置
----
环境变量 `WECOM_WEBHOOK`：形如
    https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-...
在 GitHub 仓库里放成 Actions secret 即可。

**没配也不报错**：打印提示后退出 0，这样在 secret 还没配好之前整条 workflow
依然跑得通，不会因为通知这一步把周报流程搞挂。

用法
----
    WECOM_WEBHOOK='https://...' python notify_wecom.py
    python notify_wecom.py --dry-run          # 只打印不发送
    python notify_wecom.py --no-file          # 只发摘要，不附完整报告文件
    python notify_wecom.py --alert "文本"     # 失败告警（不读报告文件）
    python notify_wecom.py                    # 未配 webhook 时自动等价于 dry-run

为什么还要发一份文件
--------------------
消息里的仓库链接在**手机微信里点不通**：仓库是私有的，未登录 GitHub 的浏览器
访问私有仓库时 GitHub 一律返回 404（刻意不暴露仓库存在性），而微信内置浏览器
没有登录态。所以额外把完整报告作为**文件消息**发进群里，点开即可看全文，
不依赖任何外链、也不用把仓库转公开。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlparse

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
REPORT_PATH = os.path.join(OUTPUT_DIR, "training_report.md")

# 企业微信 markdown 消息体上限 4096 字节（UTF-8，中文一个字 3 字节）。
MAX_BYTES = 4096
# GitHub 上归档报告的地址（reports/latest.md 每周被覆盖）。
# 2026-10-30 起报告归档在**私有数据仓库**里（代码仓库已开源，不含个人数据）。
REPORT_URL = os.environ.get(
    "REPORT_URL",
    "https://github.com/littlecaiji/garmin-training-data/blob/main/reports/latest.md",
)
# 单条建议最多保留多少字符，避免长段落把整条消息撑爆。
ADVICE_MAX_CHARS = 110


# ── 解析 ────────────────────────────────────────────────────────────────────
def _table_row(lines, header_re, max_lookahead=12):
    """找到匹配 header_re 的行，返回其后第一个 markdown 表格数据行。

    分隔行（|:---:|:---:|）会被跳过。找不到返回 None。
    """
    for i, ln in enumerate(lines):
        if re.search(header_re, ln):
            for nxt in lines[i + 1: i + 1 + max_lookahead]:
                s = nxt.strip()
                if not s.startswith("|"):
                    continue
                cells = [c.strip() for c in s.strip("|").split("|")]
                if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                    continue          # 分隔行
                return cells
    return None


def _section(lines, header_re):
    """取出某个 ## 小节到下一个 ## 之间的正文行。"""
    out, on = [], False
    for ln in lines:
        if re.match(r"^#{2,3} ", ln):
            if on:
                break
            on = bool(re.search(header_re, ln))
            continue
        if on:
            out.append(ln)
    return out


def _week_plan(lines):
    """解析「本周建议训练安排」表格 → [(日, 类型, 时长, 强度, TSS), ...]"""
    rows = []
    for i, ln in enumerate(lines):
        if "本周建议训练安排" in ln:
            for nxt in lines[i + 1: i + 30]:
                s = nxt.strip()
                if not s.startswith("|"):
                    if rows:
                        break
                    continue
                cells = [c.strip() for c in s.strip("|").split("|")]
                if len(cells) < 5:
                    continue
                if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                    continue
                if cells[0] in ("日", ""):
                    continue          # 表头
                rows.append(cells[:5])
            break
    return rows


def _advice(lines):
    """解析「教练建议」里的要点行 → [(emoji, 标题, 正文), ...]"""
    out = []
    for ln in _section(lines, r"教练建议"):
        m = re.match(r"^\s*(\S+)\s+\*\*(.+?)\*\*\s*[:：]\s*(.*)$", ln)
        if m:
            out.append((m.group(1), m.group(2), m.group(3).strip()))
    return out


def _truncate(text, limit):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，。、；;, ") + "…"


# ── 摘要 ────────────────────────────────────────────────────────────────────
def _cell(lines, keyword, col=1):
    """在 `| 关键字 | 值 | 说明 |` 这类表格行里取第 col 列。"""
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|") or keyword not in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) > col:
            return cells[col]
    return None


def build_summary(md, with_file=True):
    lines = md.splitlines()

    title = next((l[2:].strip() for l in lines if l.startswith("# ")), "周训练报告")

    load = _table_row(lines, r"CTL\s*\(长期负荷\)") or ["?", "?", "?"]
    rhr = _cell(lines, "静息心率") or "—"
    sleep_val = _cell(lines, "平均睡眠") or "—"
    sleep_note = _cell(lines, "平均睡眠", 2) or ""

    total = _table_row(lines, r"^\|\s*次数\s*\|") or []
    dist_row = next((l for l in lines if l.startswith("> 骑行总距离")), "")
    dist = dist_row.replace("> 骑行总距离", "").split("，")[0].strip() if dist_row else "—"
    rest_line = next((l.strip() for l in lines if "🛌" in l), "")

    plan = _week_plan(lines)
    advice = _advice(lines)

    # ── 拼装（企业微信 markdown 不支持表格，一律用引用行排版）──
    out = [f"🚴 **{title}**", ""]
    out.append(
        f"**负荷**  CTL {load[0]} · ATL {load[1]} · "
        f'<font color="info">TSB {load[2]}</font>'
    )
    sleep_seg = f"睡眠 {sleep_val}"
    if "偏短" in sleep_note:
        sleep_seg += f' <font color="warning">{sleep_note}</font>'
    out.append(f"**恢复**  RHR {rhr} · {sleep_seg}")
    out.append("")

    if total:
        out.append(f"**上周**  {total[0]} 次 · {total[1]} · {total[2]} TSS · {dist}")
    for ln in lines:
        s = ln.strip()
        m = re.match(r"^\|\s*([🚴🏋️]+)\s*(\S+)\s*\|(.+)$", s)
        if not m:
            continue
        kind, cells = f"{m.group(1)} {m.group(2)}", [c.strip() for c in s.strip("|").split("|")]
        times, dur, tss = cells[1], cells[2], cells[3]
        out.append(f"> {kind} {times}次 · {dur} · {tss} TSS")
    if rest_line:
        out.append("> " + rest_line)
    out.append("")

    if plan:
        out.append("**本周计划**")
        for day, kind, dur, pw, tss in plan:
            seg = f"{day} {kind}"
            tail = " · ".join(x for x in (dur, pw) if x and x != "—")
            if tail:
                seg += f" · {tail}"
            if tss and tss != "0":
                seg += f" · TSS {tss}"
            out.append("> " + seg)
        out.append("")

    if advice:
        out.append("**💡 重点**")
        for emoji, head, body in advice[:3]:
            out.append(
                f'> <font color="warning">{emoji} {head}</font> '
                f"{_truncate(body, ADVICE_MAX_CHARS)}"
            )
        out.append("")

    if with_file:
        out.append("📎 完整报告随后以文件发出，点开即可看全文")
    if REPORT_URL:
        out.append(f"[仓库链接（需登录 GitHub）→]({REPORT_URL})")

    return "\n".join(out).strip()


def clamp_bytes(text, limit=MAX_BYTES):
    """按 UTF-8 字节数截断，尽量在行边界上切，并补一行提示。"""
    if len(text.encode("utf-8")) <= limit:
        return text
    suffix = "\n\n…（内容过长已截断，完整报告见随附文件）"
    budget = limit - len(suffix.encode("utf-8"))
    kept, used = [], 0
    for ln in text.splitlines():
        b = len((ln + "\n").encode("utf-8"))
        if used + b > budget:
            break
        kept.append(ln)
        used += b
    return "\n".join(kept) + suffix


# ── 发送 ────────────────────────────────────────────────────────────────────
def send(webhook, content, timeout=15):
    body = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": content}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("errcode") != 0:
        raise RuntimeError(f"企业微信返回错误：{resp}")
    return resp


# 2026-10-09 实测：机器人**刚创建完**的那一小段时间，接口会返回
# `93000 invalid webhook url`（几十秒后同一条 URL 就正常了）。这类抖动不该让
# 无人值守的周任务丢通知，所以统一退避重试，不区分错误码。
RETRY_DELAYS = (5, 15, 45)


def send_with_retry(webhook, content, timeout=15):
    last = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS, start=1):
        if delay:
            print(f"::warning::第 {attempt - 1} 次发送失败（{last}），{delay}s 后重试")
            time.sleep(delay)
        try:
            return send(webhook, content, timeout)
        except Exception as e:          # noqa: BLE001 — 网络类异常一律重试
            last = e
    raise last


# ── 文件消息（完整报告）─────────────────────────────────────────────────────
UPLOAD_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"


def _multipart(field, filename, payload, boundary):
    b = boundary.encode()
    out = b"--" + b + b"\r\n"
    out += (f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\n').encode()
    out += b"Content-Type: application/octet-stream\r\n\r\n"
    out += payload + b"\r\n"
    out += b"--" + b + b"--\r\n"
    return out


def upload_file(webhook, filepath, filename, timeout=30):
    """上传文件拿 media_id（有效期 3 天）。

    ⚠️ filename 必须是**纯 ASCII**。企业微信的 upload_media 不认 RFC 5987 的
    `filename*=UTF-8''...` 写法，用中文文件名会返回 `44001 empty media data`。
    """
    key = parse_qs(urlparse(webhook).query).get("key", [""])[0]
    if not key:
        raise RuntimeError("webhook 里没有 key 参数")
    with open(filepath, "rb") as f:
        payload = f.read()
    boundary = "----wfboundary" + uuid.uuid4().hex
    req = urllib.request.Request(
        f"{UPLOAD_URL}?key={key}&type=file",
        data=_multipart("media", filename, payload, boundary),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("errcode") != 0:
        raise RuntimeError(f"上传失败：{resp}")
    return resp["media_id"]


def send_file(webhook, media_id, timeout=15):
    body = json.dumps({"msgtype": "file", "file": {"media_id": media_id}}).encode()
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("errcode") != 0:
        raise RuntimeError(f"发送文件失败：{resp}")
    return resp


def send_report_file_with_retry(webhook, filepath, filename):
    last = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS, start=1):
        if delay:
            print(f"::warning::第 {attempt - 1} 次发文件失败（{last}），{delay}s 后重试")
            time.sleep(delay)
        try:
            return send_file(webhook, upload_file(webhook, filepath, filename))
        except Exception as e:          # noqa: BLE001
            last = e
    raise last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印不发送")
    ap.add_argument("--no-file", action="store_true",
                    help="只发摘要，不附完整报告文件")
    ap.add_argument("--alert", metavar="TEXT",
                    help="发一条告警消息（不读报告文件）—— 供 workflow 失败时调用")
    ap.add_argument("--message", metavar="TEXT",
                    help="发一条任意 markdown 消息（不带任何预设标题）—— 供成功通知用")
    ap.add_argument("--message-file", metavar="PATH",
                    help="同 --message，但内容从文件读（避免多行内容的 shell 转义问题）")
    args = ap.parse_args()

    # ── 通用消息模式 ─────────────────────────────────────────────────────────
    # 与 --alert 的区别：**不加**任何预设标题，内容由调用方自己组织。
    # 需要它的原因：国服同步成功也要通知，但用 --alert 会硬顶一个
    # 「周报流程失败」上去 —— 那是另一条业务线的文案。
    text = args.message
    if args.message_file:
        with open(args.message_file, encoding="utf-8") as f:
            text = f.read()
    if text is not None:
        webhook = (os.environ.get("WECOM_WEBHOOK") or "").strip()
        if args.dry_run or not webhook:
            print("::notice::未配置 WECOM_WEBHOOK 或 --dry-run，消息未发送")
            print(text)
            return 0
        try:
            send_with_retry(webhook, text.strip())
        except Exception as e:          # noqa: BLE001
            print(f"::error::消息发送失败：{e}")
            return 1
        print("✅ 消息已推送到企业微信")
        return 0

    # ── 告警模式 ─────────────────────────────────────────────────────────────
    # 必须有这条路径：以前报告生成失败时，本脚本找不到报告文件、只打一句
    # warning 就退出，用户**什么都收不到** —— 故障只能靠「没收到报告」发现。
    # 2026-10-12 真实踩到：Garmin token 失效导致整次运行失败，静默无通知。
    if args.alert:
        content = f"🚨 **周报流程失败**\n> {args.alert}"
        webhook = (os.environ.get("WECOM_WEBHOOK") or "").strip()
        if args.dry_run or not webhook:
            print("::notice::未配置 WECOM_WEBHOOK 或 --dry-run，告警未发送")
            print(content)
            return 0
        try:
            send_with_retry(webhook, content)
        except Exception as e:          # noqa: BLE001
            print(f"::error::告警发送失败：{e}")
            return 1
        print("✅ 失败告警已推送到企业微信")
        return 0

    if not os.path.exists(REPORT_PATH):
        print(f"::warning::找不到报告 {REPORT_PATH}，跳过推送")
        return 0

    with open(REPORT_PATH, encoding="utf-8") as f:
        md = f.read()

    content = clamp_bytes(build_summary(md, with_file=not args.no_file))
    size = len(content.encode("utf-8"))
    print(f"摘要已生成：{size} / {MAX_BYTES} 字节")
    print("-" * 60)
    print(content)
    print("-" * 60)

    webhook = (os.environ.get("WECOM_WEBHOOK") or "").strip()
    if args.dry_run or not webhook:
        if not webhook:
            print("::notice::未配置 WECOM_WEBHOOK，本次只演练不发送（不影响周报流程）")
        else:
            print("::notice::--dry-run，未发送")
        return 0

    try:
        send_with_retry(webhook, content)
    except urllib.error.HTTPError as e:
        print(f"::error::推送失败 HTTP {e.code}：{e.read()[:300]!r}")
        return 1
    except Exception as e:
        print(f"::error::推送失败（已重试 {len(RETRY_DELAYS)} 次）：{e}")
        return 1

    print("✅ 摘要已推送到企业微信")

    # 完整报告走文件消息：手机微信里点 GitHub 链接会因为未登录而 404，
    # 附件则不受影响。摘要已经送达，所以文件失败只告警、不算整步失败。
    if not args.no_file:
        fname = f"training-report-{datetime.now().strftime('%Y-%m-%d')}.md"
        try:
            send_report_file_with_retry(webhook, REPORT_PATH, fname)
            print(f"✅ 完整报告已作为文件发送（{fname}）")
        except Exception as e:
            print(f"::warning::完整报告文件发送失败（摘要已送达，不影响）：{e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

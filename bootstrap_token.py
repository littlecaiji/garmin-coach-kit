#!/usr/bin/env python3
"""不用 gh，只用 GitHub Personal Access Token 完成云端部署。

做四件事（全部走 api.github.com，不需要 gh / 不需要 git 凭据助手）：
  1. 校验 token 并检查权限（repo / workflow）
  2. 创建私有仓库
  3. 把 Garmin token 加密后写成 Actions secret
  4. 首次提交并推送，然后跟踪 Actions 运行状态

用法（在自己的终端里跑）：
    python3 bootstrap_token.py

token 通过隐藏输入读取，不会落盘、不会回显、不写进 .git/config。

需要的 token 权限（Classic PAT）：
    repo        → 创建仓库、推送代码
    workflow    → 推送 .github/workflows/ 下的文件（缺这个会被拒）
"""
import base64
import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
CLOUD = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(CLOUD)
TOKEN_FILE = os.path.expanduser("~/.garminconnect/garmin_tokens.json")
REPO_NAME = sys.argv[1] if len(sys.argv) > 1 else "garmin-coach-kit"

TOKEN = ""

# 本机常见代理端口（Clash/V2Ray 等）。gh/git/urllib 都**不读 macOS 系统代理**，
# 只认环境变量，所以这里主动探测一次，避免直连 github.com 超时。
PROXY_CANDIDATES = (7890, 7891, 1087, 10809, 8118, 8889)


def detect_proxy():
    """优先用环境变量；没有就探测本机常见代理端口。返回 None 表示直连。"""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    import socket
    for port in PROXY_CANDIDATES:
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
            return f"http://127.0.0.1:{port}"
        except OSError:
            continue
        finally:
            s.close()
    return None


PROXY = detect_proxy()
if PROXY:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    urllib.request.install_opener(opener)


def api(method, path, body=None, raw=False):
    """调用 GitHub API。raw=True 时返回 (数据, headers)。"""
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "garmin-cloud-bootstrap")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            payload = r.read().decode()
            return (json.loads(payload) if payload.strip() else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} {method} {path}\n{detail}") from None


def sh(cmd, **kw):
    # git 同样不读系统代理，显式带上，否则 push 会卡死
    if PROXY and cmd and cmd[0] == "git":
        cmd = ["git", "-c", f"http.proxy={PROXY}"] + cmd[1:]
    return subprocess.run(cmd, cwd=CLOUD, text=True, capture_output=True, **kw)


def main():
    global TOKEN

    print("=" * 64)
    print("Garmin 周报 · 云端部署（Token 版，不需要 gh）")
    print("=" * 64)
    print(f"网络代理      : {PROXY or '（未检测到，直连）'}")

    # ── 1. 拿 token ────────────────────────────────────────────────────────
    TOKEN = os.environ.get("GITHUB_TOKEN") or getpass.getpass(
        "\n请粘贴 GitHub Personal Access Token（输入时不显示）：").strip()
    if not TOKEN:
        print("❌ 没有拿到 token")
        return 1

    try:
        user, headers = api("GET", "/user")
    except RuntimeError as e:
        print(f"❌ token 校验失败：{e}")
        return 1

    owner = user["login"]
    scopes = headers.get("X-Oauth-Scopes", "")
    print(f"\n✅ token 有效，账号：{owner}")
    print(f"   权限范围：{scopes or '（细粒度 token 不列 scopes）'}")

    missing = [s for s in ("repo", "workflow") if s not in scopes]
    if scopes and missing:
        print(f"\n⚠️  缺少权限：{', '.join(missing)}")
        print("   - 缺 repo     → 无法创建仓库/推送")
        print("   - 缺 workflow → 推送 .github/workflows/ 会被拒")
        print("   请回到 token 设置页勾上再重新生成。")

    # ── 2. 创建私有仓库 ────────────────────────────────────────────────────
    print(f"\n— 创建私有仓库 {owner}/{REPO_NAME} —")
    try:
        api("POST", "/user/repos", {
            "name": REPO_NAME,
            "private": True,
            "description": "Garmin 周训练报告 · 云端只读跑批",
            "auto_init": False,
        })
        print("✅ 已创建")
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 422" in msg:
            print("ℹ️  仓库已存在，继续")
        elif ("createRepository" in msg or "Resource not accessible" in msg
              or "HTTP 403" in msg):
            print("❌ 这枚 token 建不了仓库。")
            print("\n   原因：GitHub 限制 **fine-grained token 无法创建仓库**（只读也建不了）。")
            print("   两个办法任选一个：\n")
            print("   ① 手动建空私有仓库（10 秒，其余全自动）")
            print("      → https://github.com/new")
            print(f"      → 名字必须叫：{REPO_NAME}")
            print("      → 选 Private，**不要**勾 README / .gitignore / license")
            print("      → 建好后重跑本脚本，会自动跳过创建步骤继续\n")
            print("   ② 换一枚 Classic token（注意页面上的 Tokens (classic) 标签）")
            print("      → https://github.com/settings/tokens/new")
            print("      → 勾上 repo + workflow 两个 scope")
            print("\n   提示：若走 ①，这枚 fine-grained token 还需要对该仓库有")
            print("        Contents=Read/Write、Workflows=Read/Write、Secrets=Read/Write，")
            print("        否则推送或写 secret 会再次报权限错误。")
            print("        去 https://github.com/settings/personal-access-tokens 编辑即可。")
            return 1
        else:
            print(f"❌ 创建失败：{e}")
            return 1

    # ── 3. 写入 Garmin token 为 Actions secret ─────────────────────────────
    print("\n— 写入 secret GARMIN_TOKENS_B64 —")
    if not os.path.exists(TOKEN_FILE):
        print(f"❌ 找不到 {TOKEN_FILE}")
        return 1
    try:
        from nacl import encoding, public
    except ImportError:
        print("❌ 缺少 PyNaCl，请先运行：")
        print("   python3 -m pip install pynacl")
        return 1

    payload = base64.b64encode(open(TOKEN_FILE, "rb").read()).decode()
    key_data, _ = api("GET", f"/repos/{owner}/{REPO_NAME}/actions/secrets/public-key")
    pk = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
    sealed = base64.b64encode(
        public.SealedBox(pk).encrypt(payload.encode())).decode()
    api("PUT", f"/repos/{owner}/{REPO_NAME}/actions/secrets/GARMIN_TOKENS_B64", {
        "encrypted_value": sealed,
        "key_id": key_data["key_id"],
    })
    print(f"✅ 已写入（{len(payload)} 字符的 base64，原文不落盘）")

    # ── 4. 推送代码（token 不写进 .git/config） ─────────────────────────────
    print("\n— 提交并推送 —")
    cp = sh(["cp", os.path.join(PROJECT, "analyze_training.py"),
             os.path.join(CLOUD, "analyze_training.py")])
    if cp.returncode:
        print(f"⚠️  同步 analyze_training.py 失败：{cp.stderr}")

    if not os.path.isdir(os.path.join(CLOUD, ".git")):
        if sh(["git", "init", "-b", "main"]).returncode:
            print("❌ git init 失败")
            return 1
        print("   git init 完成")

    ident = []
    r = sh(["git", "config", "user.email"])
    if not r.stdout.strip():
        import socket
        who = getpass.getuser()
        ident = ["-c", f"user.name={who}", "-c", f"user.email={who}@users.noreply.github.com"]

    sh(["git", "add", "-A"])
    if sh(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        c = sh(["git"] + ident + ["commit", "-m", "Garmin 周报 · 云端探针（只读）"])
        print("   " + (c.stdout.strip().splitlines() or ["已提交"])[0])
    else:
        print("   没有新改动，跳过提交")

    push_url = f"https://x-access-token:{TOKEN}@github.com/{owner}/{REPO_NAME}.git"
    p = sh(["git", "push", push_url, "main"])
    if p.returncode:
        print("❌ 推送失败：")
        print("   " + p.stderr.strip().replace(TOKEN, "***"))
        return 1
    print("✅ 已推送")

    sh(["git", "remote", "remove", "origin"])
    sh(["git", "remote", "add", "origin",
        f"https://github.com/{owner}/{REPO_NAME}.git"])

    # ── 5. 跟一下 Actions ──────────────────────────────────────────────────
    repo_url = f"https://github.com/{owner}/{REPO_NAME}"
    actions_url = f"{repo_url}/actions"
    print(f"\n仓库：{repo_url}")
    print(f"Actions：{actions_url}")

    print("\n— 等待 Actions 跑起来 —")
    run = None
    for _ in range(12):
        time.sleep(5)
        try:
            data, _ = api("GET", f"/repos/{owner}/{REPO_NAME}/actions/runs?per_page=1")
            runs = data.get("workflow_runs", [])
            if runs:
                run = runs[0]
                if run["status"] == "completed":
                    break
        except RuntimeError:
            pass

    if run:
        print(f"   状态：{run['status']} / {run.get('conclusion') or '进行中'}")
        print(f"   详情：{run['html_url']}")
        if run["status"] != "completed":
            print("   （还在跑，稍后刷新上面这个链接即可）")
    else:
        print("   还没检测到运行，直接打开 Actions 页面看看")

    print("\n" + "=" * 64)
    print("完成。接下来去 Actions 页面看探针结论：")
    print("  ① Garmin 接受异地 IP 吗  ② 报告能不能正常生成")
    print("✅ 通过 → 取消 weekly-report.yml 里 schedule 的注释，启用每周定时")
    print("=" * 64)
    print("\n安全提示：这个 token 已用于本次操作，用完可在 "
          f"{API.replace('api.', '')}/settings/tokens 立刻吊销或换新。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)

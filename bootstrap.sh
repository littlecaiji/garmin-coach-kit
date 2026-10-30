#!/bin/zsh
# 把 cloud/ 变成私有 GitHub 仓库并推上去（前置条件：gh 已登录、Garmin token 在本机）。
#
# 用法：
#   ./bootstrap.sh                # 仓库名默认 garmin-coach-kit
#   ./bootstrap.sh my-repo-name

set -e

# 仓库根 = 本脚本所在目录（不要再写死绝对路径 —— 那会把本地用户名带进公开仓库）
CLOUD="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$CLOUD/.." && pwd)"
REPO_NAME="${1:-garmin-coach-kit}"
TOKEN_FILE="$HOME/.garminconnect/garmin_tokens.json"

cd "$CLOUD"

# ── 0. 前置检查 ────────────────────────────────────────────────────────────
command -v gh >/dev/null || { echo "❌ 未安装 gh：brew install gh"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "❌ gh 未登录：先跑 gh auth login"; exit 1; }
[ -f "$TOKEN_FILE" ] || { echo "❌ 找不到 $TOKEN_FILE（先在本机跑一次报告）"; exit 1; }

# 网络检查：gh / git 都是 Go/C 程序，**不读 macOS 系统代理**，只认环境变量。
# 直连 github.com 在国内基本必超时，所以这里先探一次并给出明确指引。
if ! curl -sS -o /dev/null -m 8 https://github.com 2>/dev/null; then
  echo "❌ 连不上 github.com。"
  if [ -n "$HTTPS_PROXY$https_proxy" ]; then
    echo "   已设 HTTPS_PROXY=$HTTPS_PROXY$https_proxy，但依然不通——检查代理软件是否在运行。"
  else
    echo "   本机若在跑代理（Clash/V2Ray 常见端口 7890），先执行："
    echo "     export HTTPS_PROXY=http://127.0.0.1:7890"
    echo "     export HTTP_PROXY=http://127.0.0.1:7890"
    echo "   然后重跑本脚本。（gh 不读系统代理设置，必须显式给环境变量）"
  fi
  exit 1
fi

OWNER="$(gh api user --jq .login)"

# ── 1. 同步本地脚本副本（保持与项目里的一致） ──────────────────────────────
echo "→ 同步 analyze_training.py"
cp "$PROJECT/analyze_training.py" "$CLOUD/analyze_training.py"

# ── 2. 本地仓库 ────────────────────────────────────────────────────────────
[ -d .git ] || { echo "→ git init"; git init -b main >/dev/null; }

GIT_ID=()
if [ -z "$(git config user.email || true)" ]; then
  GIT_ID=(-c user.name="$(whoami)" -c user.email="$(whoami)@users.noreply.github.com")
fi

git add -A
if git diff --cached --quiet; then
  echo "→ 无新改动，跳过提交"
else
  git "${GIT_ID[@]}" commit -m "Garmin 周报 · 云端探针（只读）" >/dev/null
  echo "→ 已提交"
fi

# ── 3. 确保远端私有仓库存在（必须先建仓库，secret 才写得上） ────────────────
if gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  echo "→ 仓库已存在：$OWNER/$REPO_NAME"
else
  echo "→ 创建私有仓库 $OWNER/$REPO_NAME"
  gh repo create "$REPO_NAME" --private \
    --description "Garmin 周训练报告 · 云端只读跑批" >/dev/null
fi

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "https://github.com/$OWNER/$REPO_NAME.git"
else
  git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
fi

# ── 4. token 作为 secret 上传（不落盘、不进 git） ──────────────────────────
echo "→ 上传 secret GARMIN_TOKENS_B64"
base64 -i "$TOKEN_FILE" | tr -d '\n' | gh secret set GARMIN_TOKENS_B64 \
  -R "$OWNER/$REPO_NAME" --body -

# ── 5. 推送（首次推送会触发探针工作流） ────────────────────────────────────
echo "→ 推送 main"
git push -u origin main

echo ""
echo "✅ 完成。仓库：https://github.com/$OWNER/$REPO_NAME"
echo ""
echo "下一步："
echo "  1. 看 Actions 跑得怎么样："
echo "     gh run list -R $OWNER/$REPO_NAME -L 5"
echo "     gh run watch -R $OWNER/$REPO_NAME"
echo "  2. 通过后再回来启用每周定时（取消 workflow 里 schedule 的注释）"

#!/bin/zsh
# 推送【代码仓库】到云端，并跟踪 Actions 结果。
# 只依赖一个 token 文件，不碰 gh 登录态、不碰钥匙串。
#
# ⚠️ 2026-10-30 拆库后职责变了：
#   本脚本只推 **garmin-coach-kit（公开代码仓库）**。
#   个人数据（state / reports）在私有仓库 **garmin-training-data**，
#   要推那边请用 ./push_data.sh —— 两个仓库分开推，别混。
#
# 用法：
#   ./push_and_watch.sh                      # 默认读 <项目根>/work/gh_token.txt
#   ./push_and_watch.sh /path/to/token.txt
#
# token 需要：Classic PAT，scope 勾 repo + workflow（推 .github/workflows 必须要 workflow）。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# ⚠️ 默认路径必须是项目里的 work/gh_token.txt。这里原本写的是 ~/Desktop/gh_token.txt，
# 那个文件根本不存在 → 脚本一跑就「找不到 token」。脚本在 cloud/ 下，故用 ../
TOKEN_FILE="${1:-$SCRIPT_DIR/../work/gh_token.txt}"
# ⚠️ 代理端口会变：2026-10-12 实测已是 7891（7890 已 connection refused）。
# 不能只靠改默认值——下次再变又会静默失败，所以下面加一次端口探活。
PROXY="${PROXY:-http://127.0.0.1:7891}"
REPO="littlecaiji/garmin-coach-kit"
DATA_REPO="littlecaiji/garmin-training-data"

[ -f "$TOKEN_FILE" ] || { echo "❌ 找不到 token 文件：$TOKEN_FILE"; exit 1; }
T="$(tr -d '[:space:]' < "$TOKEN_FILE")"
[ -n "$T" ] || { echo "❌ token 文件是空的：$TOKEN_FILE"; exit 1; }

# 代理端口探活：端口变了会表现为 push 卡住/超时，很难排查，这里提前说清楚。
PORT="${PROXY##*:}"
if ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
  echo "⚠️  代理端口 $PORT 没有监听（实际端口可能又变了）。在听的常见代理端口："
  # 只列代理常见的几档，避免把 mysql/redis 之类全打出来
  lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk '{print $9}' \
    | grep -E ':(789[0-9]|108[0-9]{2}|8888|8080|8118|1080)$' | sort -u | sed 's/^/    /' \
    || echo "    （没找到常见代理端口）"
  echo "    可用 PROXY=http://127.0.0.1:<端口> ./push_and_watch.sh 覆盖"
fi
case "$T" in
  ghp_*|gho_*) ;;
  github_pat_*) echo "⚠️  这是 fine-grained token。它能推代码的前提是该仓库在它的"
                echo "    Repository access 白名单里、且 Contents/Workflows 开了写权限，"
                echo "    否则会被拒（fine-grained 也不能创建仓库）。继续尝试…" ;;
  *) echo "⚠️  token 前缀不常见，继续尝试…" ;;
esac

cd "$(dirname "$0")"

# ⚠️ 2026-10-30：state 校验**从这里移除**了。
# 本脚本推的是代码仓库，里面已经没有任何 state 文件，推代码不存在「用旧
# state 覆写云端新 state」的可能。那个风险现在只存在于「推数据仓库」，所以
# 守门员跟着 state 一起搬到了 ./push_data.sh。别再把校验加回来。
#
# 历史上这条校验救过一次命（10/19 用 10-12 的旧 state 覆写了云端 W43 结果，
# 把 last_advanced_week 打回 W42，幂等闸门当场失效）—— 所以它没有被删掉，
# 只是换了个正确的位置。

echo "→ 推送 main"
# git 不读 macOS 系统代理，必须显式给 http.proxy
git -c http.proxy="$PROXY" push \
  "https://x-access-token:$T@github.com/$REPO.git" main
echo "✅ 已推送"

# ⚠️ 对裸 URL push 不更新 remote-tracking ref（git 已知行为），本地 origin/main
# 会停在旧位置。10/14 就因此误判「领先远端 3 个提交」，差点把已推过的提交
# 再折腾一遍 API 重放。推送成功后手动对齐指针，杜绝这种假差异。
git update-ref refs/remotes/origin/main "$(git rev-parse main)" \
  && echo "✅ origin/main 指针已对齐 $(git rev-parse --short main)"

export GH_TOKEN="$T" HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"

echo ""
echo "→ 等待 Actions 起来"
sleep 25
gh run list -R "$REPO" -L 3 2>&1 || echo "（gh 查询失败，直接开页面看）"
echo ""
echo "页面：https://github.com/$REPO/actions"
echo "报告归档在私有数据仓库：https://github.com/$DATA_REPO/tree/main/reports"

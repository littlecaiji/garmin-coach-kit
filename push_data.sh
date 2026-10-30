#!/bin/zsh
# 推送【私有数据仓库】garmin-training-data。
#
# 什么时候用：往 data/state/strength_log.md 补录力量课后，把补录推到云端，
# 否则下周一跑周报时云端看不到这次力量课（云端读的是远端仓库，不是本地文件）。
#
# 用法：
#   ./push_data.sh                 # 提交 data/ 的全部改动并推送
#   ./push_data.sh "力量补录 10/30"  # 自定义提交信息
#
# 与 push_and_watch.sh 的分工（2026-10-30 拆库）：
#   push_and_watch.sh → 公开代码仓库 garmin-coach-kit
#   push_data.sh      → 私有数据仓库 garmin-training-data（本脚本）
#
# ⚠️ state 新鲜度守门员在**这里**，不在推代码那侧 —— 因为只有本仓库里才有
# state，才存在「用旧 state 覆写云端新 state」的风险。10/19 事故就是这件事。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR_PATH="${DATA_DIR:-$PROJECT_DIR/data}"
TOKEN_FILE="${TOKEN_FILE:-$PROJECT_DIR/work/gh_token.txt}"
PROXY="${PROXY:-http://127.0.0.1:7891}"
DATA_REPO="littlecaiji/garmin-training-data"
MSG="${1:-数据更新 $(date +%Y-%m-%d\ %H:%M)}"

[ -d "$DATA_DIR_PATH/.git" ] || {
  echo "❌ 找不到数据仓库：$DATA_DIR_PATH"
  echo "   先 clone：git clone https://github.com/$DATA_REPO.git \"$DATA_DIR_PATH\""
  exit 1
}
[ -f "$TOKEN_FILE" ] || { echo "❌ 找不到 token 文件：$TOKEN_FILE"; exit 1; }
T="$(tr -d '[:space:]' < "$TOKEN_FILE")"
[ -n "$T" ] || { echo "❌ token 文件是空的：$TOKEN_FILE"; exit 1; }

# ── 守门员：本地 state 是否比云端旧 ────────────────────────────────────────
# 云端周报每周会自己 commit state 回来，本地不会自动跟进。若本地这份更旧，
# 推上去就把云端算好的跨周记忆（档位/负重/长骑基准/闸门结论）整体覆写回去，
# last_advanced_week 随之倒退 —— 幂等闸门失效，本周再跑一次就会无依据加量。
if [ "${SKIP_STATE_GUARD:-0}" = "1" ]; then
  echo "⚠️  SKIP_STATE_GUARD=1，跳过 state 新鲜度校验（确认你知道后果）"
else
  python3 "$SCRIPT_DIR/state_guard.py" \
      --state "$DATA_DIR_PATH/state/training_state.json" \
      --token-file "$TOKEN_FILE" || {
    echo ""
    echo "❌ 已中止推送（state 比云端旧）。按上面的提示先拉云端真值再改。"
    echo "   确实要强行覆盖：SKIP_STATE_GUARD=1 ./push_data.sh"
    exit 3
  }
fi

cd "$DATA_DIR_PATH"

if [ -z "$(git status --porcelain)" ]; then
  echo "✅ 数据仓库没有改动，收工。"
  exit 0
fi

echo ""
echo "→ 将要提交："
git status --short | sed 's/^/    /'
git add -A
git commit -m "$MSG"
echo "→ 推送 $DATA_REPO"
git -c http.proxy="$PROXY" push \
  "https://x-access-token:$T@github.com/$DATA_REPO.git" main
# 裸 URL push 不更新 remote-tracking ref（git 已知行为），手动对齐，
# 否则下次会误判「领先远端 N 个提交」（10/14 踩过）。
git update-ref refs/remotes/origin/main "$(git rev-parse main)"
echo "✅ 已推送 $(git rev-parse --short main)"
echo ""
echo "报告归档：https://github.com/$DATA_REPO/tree/main/reports"

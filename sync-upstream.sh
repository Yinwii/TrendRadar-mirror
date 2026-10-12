#!/usr/bin/env bash
# 一键同步上游 sansan0/TrendRadar 的源码更新到本 fork
#
# 用法：
#   ./sync-upstream.sh          检查并合并上游 master
#   ./sync-upstream.sh --check  只列出上游有哪些新提交，不做任何改动
#
# 合并并 push 到 origin/master 后，GitHub Actions 会自动构建新镜像，
# 再到 VPS 执行：git pull && docker compose pull && docker compose up -d

set -euo pipefail

UPSTREAM_URL="https://github.com/sansan0/TrendRadar.git"
UPSTREAM_BRANCH="master"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

cd "$(git rev-parse --show-toplevel)"

# 1. 确保 upstream 远端存在，并禁用推送（防止误推到原作者仓库）
if ! git remote get-url upstream >/dev/null 2>&1; then
  git remote add upstream "$UPSTREAM_URL"
  echo "已添加远端 upstream -> $UPSTREAM_URL"
fi
git remote set-url --push upstream "DISABLED_防止误推到原作者仓库"

# 2. 工作区必须干净，否则合并会污染现场
if [ -n "$(git status --porcelain)" ]; then
  echo "工作区有未提交改动，请先提交或 git stash 后再同步："
  git status -s
  exit 1
fi

# 3. 拉取上游
echo "正在拉取上游 $UPSTREAM_BRANCH ..."
git fetch upstream --tags

COUNT=$(git rev-list --count "HEAD..upstream/$UPSTREAM_BRANCH")
if [ "$COUNT" -eq 0 ]; then
  echo "已是最新，上游没有新提交"
  exit 0
fi

echo "上游有 $COUNT 个新提交："
git log --oneline --no-decorate "HEAD..upstream/$UPSTREAM_BRANCH" | head -30

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "（--check 模式，未做任何改动）"
  exit 0
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo
echo "即将把上游合并进当前分支：$BRANCH"
read -r -p "继续？(y/N) " ans
if [ "$ans" != "y" ]; then
  echo "已取消"
  exit 0
fi

# 4. 合并
if git merge "upstream/$UPSTREAM_BRANCH" --no-edit; then
  echo
  echo "合并完成。下一步："
  echo "  git push origin $BRANCH"
  echo "  等 Actions 构建完成后，在 VPS 执行："
  echo "  git pull && docker compose pull && docker compose up -d"
else
  echo
  echo "出现冲突，需手动解决以下文件："
  git diff --name-only --diff-filter=U
  echo "解决后执行： git add <文件> && git commit"
  echo "放弃本次合并： git merge --abort"
  exit 1
fi

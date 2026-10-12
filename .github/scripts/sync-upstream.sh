#!/usr/bin/env bash
# =============================================================================
# sync-upstream.sh —— 「每日同步上游」CI 脚本（Yinwii/TrendRadar-mirror 专用）
# -----------------------------------------------------------------------------
# 运行环境：GitHub Actions (ubuntu-latest)，工作目录 = 本仓库的 clone
# 依赖：git、gh（GH_TOKEN 由 workflow 注入）
#
# ■ 安全设计（核心，别改）
#   本脚本【只做 git merge】，绝不 reset / checkout / force 覆盖本仓库的定制提交。
#     · 上游改动与本地定制不重叠 → 自动合并 → 推送 → 显式派发镜像构建（零手动）
#     · 上游改动与本地定制冲突   → 立刻 merge --abort → 不推送、不出镜像
#       （线上继续跑上一个好版本）→ 开 issue 告警，等人工解决
#   ⇒ 本脚本不可能把定制的功能与样式静默替换掉。
#
# ■ 为什么必须显式派发构建
#   用 GITHUB_TOKEN 推送的提交【不会】触发 push 类 workflow（GitHub 的硬性规定），
#   所以合并成功后必须自己 gh workflow run，否则镜像永远不会重建。
#
# ■ 为什么上游的 .github/workflows/ 改动不采纳
#   GITHUB_TOKEN 是 GitHub App 身份、没有 workflows 权限；只要 push 的 ref 最终差异
#   里含 workflow 文件，服务端就会拒绝整个 push（实测报错：refusing to allow a
#   GitHub App to create or update workflow ... without `workflows` permission）。
#   合并后把该目录还原成「合并前版本」即可放行 —— 已用三次探针实测：哪怕中间提交
#   改过 workflow 文件，只要最终 ref 差异里没有 workflow 改动，push 照样通过。
#   这些上游 workflow 只服务上游自己的发布/试用流程，与本仓库部署无关。
#
# 环境变量：
#   UPSTREAM_URL / UPSTREAM_BRANCH  上游地址与分支（默认 sansan0/TrendRadar master）
#   BRANCH                          本仓库分支（默认 master）
#   TARGET_WORKFLOW                 要派发的构建 workflow 文件名（默认 build-image.yml）
#   DRY_RUN=1                       只检查、只报告，不做任何改动
#
# 退出码：0 = 无更新 / 同步成功   1 = 冲突或失败（workflow 标红并留 issue）
# =============================================================================
set -uo pipefail

UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/sansan0/TrendRadar.git}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-master}"
BRANCH="${BRANCH:-master}"
TARGET_WORKFLOW="${TARGET_WORKFLOW:-build-image.yml}"
DRY_RUN="${DRY_RUN:-0}"

REPO="${GITHUB_REPOSITORY:-}"
if [ -z "$REPO" ]; then
  REPO="$(git remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]##; s#\.git$##')"
fi

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$*"; }

# ------------------------------------------------------------------ issue 通知
# 每个标签最多保持一个打开中的 issue，避免每天刷屏；同一问题只追加评论。
open_issue_or_comment() { # $1=label  $2=color  $3=title  $4=body -> 0=成功
  gh label create "$1" --repo "$REPO" --color "$2" \
     --description "上游同步需要人工处理" --force >/dev/null 2>&1 || true
  local n
  n="$(gh issue list --repo "$REPO" --state open --label "$1" --limit 1 \
        --json number --jq '.[0].number // empty' 2>/dev/null || true)"
  if [ -n "${n:-}" ]; then
    gh issue comment "$n" --repo "$REPO" --body "$4" >/dev/null 2>&1 || return 1
    log "已追加评论到 issue #$n"
  else
    gh issue create --repo "$REPO" --title "$3" --label "$1" --body "$4" >/dev/null 2>&1 || return 1
    log "已创建 issue：$3"
  fi
  return 0
}

# ------------------------------------------------------------------ 自检模式
# 只演练两个「外部调用」是否可用（gh 派发构建 + gh 写 issue），
# 不拉上游、不合并、不推送。仅在收到 workflow_dispatch 且 probe=true 时进入。
if [ "${PROBE:-0}" = "1" ]; then
  log "PROBE=1 自检模式：只演练外部调用，不拉上游、不合并、不推送"
  ok=1
  if gh workflow run "$TARGET_WORKFLOW" --repo "$REPO" --ref "$BRANCH" >/dev/null 2>&1; then
    log "✅ 自检通过：gh workflow run（派发镜像构建）可用"
  else
    log "✗ 自检失败：gh workflow run 不可用"; ok=0
  fi
  if open_issue_or_comment "upstream-sync-selftest" "1d76db" \
       "[自检] 上游同步外部调用自检" \
       "这是 **自检** 用的 issue，用于验证 issue 写入通道可用，可随时关闭/删除。"; then
    log "✅ 自检通过：gh issue 写入可用"
  else
    log "✗ 自检失败：gh issue 写入不可用"; ok=0
  fi
  [ "$ok" = 1 ] && { log "🎉 自检全部通过"; exit 0; } || { log "✗ 自检存在失败项"; exit 1; }
fi

# ------------------------------------------------------------------ 0. 准备
git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

if git remote get-url upstream >/dev/null 2>&1; then
  git remote set-url upstream "$UPSTREAM_URL"
else
  git remote add upstream "$UPSTREAM_URL"
fi
# 双保险：禁止误推到原作者仓库
git remote set-url --push upstream "DISABLED_禁止推送到上游仓库"

log "仓库：$REPO　分支：$BRANCH"
log "拉取上游 $UPSTREAM_URL ($UPSTREAM_BRANCH) ..."
if ! git fetch --no-tags upstream "$UPSTREAM_BRANCH" 2>&1 | sed 's/^/    /'; then
  log "✗ fetch 上游失败，本次跳过"
  exit 1
fi
git rev-parse --verify -q "upstream/$UPSTREAM_BRANCH" >/dev/null || { log "✗ 上游 ref 不存在"; exit 1; }

BEHIND="$(git rev-list --count "HEAD..upstream/$UPSTREAM_BRANCH")"
log "落后上游 ${BEHIND} 个提交"
if [ "$BEHIND" -eq 0 ]; then
  log "✅ 上游没有新提交，无需同步"
  exit 0
fi

# ------------------------------------------------------------------ 1. 算出「重叠文件」
#   BASE   = 上次合并点（HEAD 与上游的共同祖先）
#   OURS   = 相对 BASE，被我们定制过的文件
#   THEIRS = 相对 BASE，上游本次改动的文件
#   两者交集 = 上游也动了我们的定制文件 → 合并后需要人工确认语义
BASE="$(git merge-base HEAD "upstream/$UPSTREAM_BRANCH")"
git diff --name-only "$BASE" "upstream/$UPSTREAM_BRANCH" | sort -u > /tmp/upstream-files.txt
git diff --name-only "$BASE" HEAD                    | sort -u > /tmp/ours-files.txt
comm -12 /tmp/upstream-files.txt /tmp/ours-files.txt > /tmp/overlap.txt || true

log "合并基点 $BASE"
log "上游本次改动 $(wc -l < /tmp/upstream-files.txt) 个文件；其中与本地定制重叠 $(wc -l < /tmp/overlap.txt) 个"
log "上游新增提交："
git log --oneline --no-decorate "HEAD..upstream/$UPSTREAM_BRANCH" | sed 's/^/    /'
log "上游改动文件："
sed 's/^/    /' /tmp/upstream-files.txt
if [ -s /tmp/overlap.txt ]; then
  log "⚠️ 与本地定制重叠的文件："
  sed 's/^/    /' /tmp/overlap.txt
else
  log "✅ 与本地定制零重叠（可安全自动合并）"
fi

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN=1：只报告，不合并、不推送"
  exit 0
fi

# ------------------------------------------------------------------ 2. 合并（冲突即停）
PRE_MERGE="$(git rev-parse HEAD)"
log "执行 git merge upstream/$UPSTREAM_BRANCH（只合并，绝不覆盖）"
if git merge --no-edit "upstream/$UPSTREAM_BRANCH" >/tmp/merge.out 2>&1; then
  log "✅ 合并成功：$(git log --oneline -1)"

  # ---- 2b. 上游对 .github/workflows/ 的改动按设计【不采纳】 ----
  # 原因：GITHUB_TOKEN 是 GitHub App 身份，没有 workflows 权限，只要 push 的
  #       ref 最终差异里含 workflow 文件就会被服务端拒绝：
  #         "refusing to allow a GitHub App to create or update workflow ..."
  #       已实测：把该目录还原成合并前版本后即可放行（edgeever 同款做法）。
  #       这些上游 workflow（docker.yml / crawler.yml 等）本来只服务上游自己的
  #       发布与试用流程，与我们的部署无关。
  if git diff --name-only "$PRE_MERGE" HEAD -- .github/workflows | grep -q .; then
    log "上游本次改动了 .github/workflows/ —— 按设计不采纳，还原为合并前版本"
    git diff --name-only "$PRE_MERGE" HEAD -- .github/workflows | sed 's/^/    /'
    git rm -r -q --ignore-unmatch .github/workflows
    git checkout "$PRE_MERGE" -- .github/workflows
    git commit --amend --no-edit -q
    log "已修正合并提交：.github/workflows/ 保持本仓库版本"
  fi
else
  log "✗ 合并冲突 —— 已中止，本地定制未被改动、未推送、未出新镜像"
  sed 's/^/    /' /tmp/merge.out
  CONFLICTED="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
  git merge --abort || true
  BODY="上游 \`sansan0/TrendRadar\` 有新提交，但与本地定制**冲突**，同步已自动中止。

- ✅ **未推送、未构建镜像** → 线上继续运行上一个好版本，定制的功能与样式**一点没丢**
- ❌ 冲突文件：
\`\`\`
${CONFLICTED}
\`\`\`
- ⚠️ 上游本次改动 ∩ 本地定制过的文件：
\`\`\`
$(cat /tmp/overlap.txt)
\`\`\`

**处理方式（任选其一）**
1. 在 bits-FRA 上 \`cd /root/data/docker/TrendRadar && ./sync-upstream.sh\`，手工解冲突后 \`git push origin master\`；
2. 或在 GitHub 上对比 \`upstream/master\` 手工合并。

推送后镜像会自动重建（若没自动构建，去 Actions 页 Run workflow）。本 issue 处理完可直接关闭。"
  open_issue_or_comment "upstream-sync-conflict" "d73a4a" \
    "[上游同步] 与本地定制冲突，需人工合并" "$BODY"
  exit 1
fi

# ------------------------------------------------------------------ 3. 推送 + 派发构建
log "推送到 origin/$BRANCH"
if ! git push origin "HEAD:$BRANCH" 2>&1 | sed 's/^/    /'; then
  log "✗ 推送失败"
  log "  若输出里含 push protection 字样 → 打开提示的 unblock 链接放行该误报后重跑本 workflow"
  exit 1
fi
NEW_SHA="$(git rev-parse --short HEAD)"
log "✅ 已推送 $NEW_SHA"

SHA_FULL="$(git rev-parse HEAD)"
log "派发构建 $TARGET_WORKFLOW（GITHUB_TOKEN 推送不会触发 workflow，必须显式派发）"
dispatched=0
# 兜底判断：万一 push 真的触发了构建（例如改用部署密钥推送），就别重复派发
sleep 10
if gh run list --repo "$REPO" --workflow "$TARGET_WORKFLOW" --limit 10 \
     --json headSha --jq '.[].headSha' 2>/dev/null | grep -qx "$SHA_FULL"; then
  log "✅ 该提交已有构建运行，无需重复派发"
  dispatched=1
fi
for i in 1 2 3; do
  [ "$dispatched" -eq 1 ] && break
  if gh workflow run "$TARGET_WORKFLOW" --repo "$REPO" --ref "$BRANCH" >/dev/null 2>&1; then
    dispatched=1; log "✅ 已派发（第 $i 次尝试）"; break
  fi
  log "  第 $i 次派发失败，3 秒后重试"
  sleep 3
done
if [ "$dispatched" -ne 1 ]; then
  log "⚠️ 派发构建失败 —— 请到 Actions 页面手动 Run workflow"
  open_issue_or_comment "upstream-sync-drift" "fbca04" \
    "[上游同步] 已合并但构建派发失败，请手动触发" \
    "上游同步已推送（\`$NEW_SHA\`），但 \`gh workflow run $TARGET_WORKFLOW\` 连续 3 次失败。

请在 Actions 页面手动运行 **$TARGET_WORKFLOW**（分支 $BRANCH），完成后本 issue 可关闭。"
fi

# ------------------------------------------------------------------ 4. 重叠提示
if [ -s /tmp/overlap.txt ]; then
  BODY="上游同步**已完成**（提交 \`$NEW_SHA\`，镜像已派发重建），但本次上游也改动了以下**本地定制过的文件**：

\`\`\`
$(cat /tmp/overlap.txt)
\`\`\`

git merge 已自动完成，**你的定制没有被覆盖**；但请人工确认合并结果里这些定制仍然完整、
样式仍然生效（上游若重构了文件内部结构，合并后可能语义上有偏差）。

在 bits-FRA 上核对：
\`\`\`bash
cd /root/data/docker/TrendRadar
git show --stat HEAD
git diff '$BASE' HEAD -- $(tr '\n' ' ' < /tmp/overlap.txt)
\`\`\`

确认无误后可直接关闭本 issue。"
  open_issue_or_comment "upstream-sync-drift" "fbca04" \
    "[上游同步] 上游改动了定制文件，请人工确认" "$BODY"
fi

log "🎉 同步流程结束（上游 ${BEHIND} 个新提交已合并）"
exit 0

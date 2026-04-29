#!/usr/bin/env bash
# 在已登录 gh 的前提下：创建 github.com/<你>/vla-paper-digest 并推送 main。
# 前置：brew install gh && gh auth login
set -euo pipefail
cd "$(dirname "$0")/.."

if ! gh auth status &>/dev/null; then
  echo "请先执行: gh auth login" >&2
  exit 1
fi

OWNER="$(gh api user -q .login)"
REPO_NAME="vla-paper-digest"

if git remote get-url origin &>/dev/null; then
  echo "remote origin 已存在，直接 push..."
  git push -u origin main
  exit 0
fi

if gh repo view "${OWNER}/${REPO_NAME}" &>/dev/null; then
  echo "远程仓库已存在，添加 origin 并推送..."
  git remote add origin "https://github.com/${OWNER}/${REPO_NAME}.git"
  git push -u origin main
  exit 0
fi

gh repo create "${REPO_NAME}" --public --source=. --remote=origin --push \
  --description "Daily VLA/robotics paper digest (GitHub Actions)"

echo "完成。请到仓库 Settings → Secrets and variables → Actions 配置 README 中的 Secrets。"

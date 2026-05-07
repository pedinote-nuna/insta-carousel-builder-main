#!/usr/bin/env bash
# precommit-check.sh — 소아과언니 카드뉴스 시스템 보안 사전 점검
#
# 실행: bash scripts/precommit-check.sh
# git commit 전에 항상 수동 실행. 위반 1건이라도 있으면 exit 1.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VIOLATIONS=()

# 0. git 저장소 확인
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "❌ 이 폴더는 git 저장소가 아닙니다."
  echo "   먼저 'git init' 을 실행한 뒤 다시 시도하세요."
  exit 1
fi

# 1. .env 가 git 추적되지 않는지
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  VIOLATIONS+=("• .env 가 git 추적 대상입니다 → .gitignore 에 등록되어 있는지 확인")
fi

# 2. .env.example 에 실제 API 키 패턴이 들어있지 않은지
if [ -f .env.example ]; then
  if grep -E "AIzaSy[A-Za-z0-9_-]{20,}" .env.example >/dev/null 2>&1; then
    VIOLATIONS+=("• .env.example 에 Gemini 실제 키 패턴(AIzaSy...) 발견 → placeholder 로 교체 필요")
  fi
  if grep -E "sk-ant-api03-[A-Za-z0-9_-]{20,}" .env.example >/dev/null 2>&1; then
    VIOLATIONS+=("• .env.example 에 Anthropic 실제 키 패턴(sk-ant-api03-...) 발견 → placeholder 로 교체 필요")
  fi
fi

# 3. knowledge/banned-words.json 이 git 추적되지 않는지
if git ls-files --error-unmatch knowledge/banned-words.json >/dev/null 2>&1; then
  VIOLATIONS+=("• knowledge/banned-words.json 이 git 추적 대상입니다 (개인정보 포함 가능) → .gitignore 등록 필요")
fi

# 결과
if [ ${#VIOLATIONS[@]} -gt 0 ]; then
  echo "❌ 보안 점검 실패 — 위반 ${#VIOLATIONS[@]}건:"
  for v in "${VIOLATIONS[@]}"; do
    echo "  $v"
  done
  echo ""
  echo "위 항목을 모두 해결한 뒤 git commit 을 진행하세요."
  exit 1
fi

echo "✅ 보안 점검 통과"
exit 0

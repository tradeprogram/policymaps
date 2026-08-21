#!/usr/bin/env bash
# 공개 전 키 노출 검사 — .env 의 '비밀' 변수만 골라 커밋 대상 전체를 훑는다.
# 문서·스크립트 어디에도 실키를 적지 않기 위해 .env 를 유일한 출처로 삼는다.
# 주의: VWORLD_DOMAIN 같은 비밀 아닌 설정값은 제외해야 오탐이 없다.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVF="$ROOT/system/.env"
[ -f "$ENVF" ] || { echo "  .env 없음: $ENVF"; exit 2; }

SECRET_VARS='^(LAW_OC|ASSEMBLY_KEY|VWORLD_KEY|LOFIN_KEY|STANREGIN_KEY|STANREGIN_KEY_ENC)='
mapfile -t KEYS < <(grep -vE '^\s*#' "$ENVF" | grep -E "$SECRET_VARS" \
  | cut -d= -f2- | tr -d '"' | tr -d "'" | awk 'length($0)>=6' | sort -u)
[ "${#KEYS[@]}" -gt 0 ] || { echo "  검사할 키 없음"; exit 2; }

PAT="$(printf '%s|' "${KEYS[@]}")"; PAT="${PAT%|}"
HITS=$(grep -rIl --exclude-dir=external --exclude-dir=__pycache__ --exclude-dir=.git \
       --exclude-dir=node_modules -E "$PAT" "$ROOT" 2>/dev/null \
       | grep -v '/system/data/' | grep -v '/system/scratchpad/' | grep -vE '\.env$' || true)

if [ -z "$HITS" ]; then
  echo "  ✅ 커밋 대상 키 노출 0건 (비밀키 ${#KEYS[@]}개 검사)"
  exit 0
else
  echo "  ⚠ 키 노출 발견 — push 금지:"
  echo "$HITS" | sed 's/^/     /'
  exit 1
fi

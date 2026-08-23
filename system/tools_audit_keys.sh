#!/usr/bin/env bash
# 공개 전 키 노출 검사 — .env 의 '비밀' 변수만 골라 커밋 대상 전체를 훑는다.
# 문서·스크립트 어디에도 실키를 적지 않기 위해 .env 를 유일한 출처로 삼는다.
#
# [수정 이력]
#  - VWORLD_DOMAIN 같은 비밀 아닌 값까지 검사해 오탐 → SECRET_VARS 로 한정.
#  - /system/data/ 를 통째로 제외했더니 그 밑 api/ 는 커밋 대상인데도 검사에서 빠져
#    search.json 의 실키 노출을 놓쳤다. 이제 커밋 제외 경로(index/graph/reference/db/log)만 뺀다.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVF="$ROOT/system/.env"
[ -f "$ENVF" ] || { echo "  .env 없음: $ENVF"; exit 2; }

SECRET_VARS='^(LAW_OC|ASSEMBLY_KEY|VWORLD_KEY|LOFIN_KEY|STANREGIN_KEY|STANREGIN_KEY_ENC|GEMINI_API_KEY|GOOGLE_API_KEY)='
mapfile -t KEYS < <(grep -vE '^\s*#' "$ENVF" | grep -E "$SECRET_VARS" \
  | cut -d= -f2- | tr -d '"' | tr -d "'" | awk 'length($0)>=6' | sort -u)
[ "${#KEYS[@]}" -gt 0 ] || { echo "  검사할 키 없음"; exit 2; }

PAT="$(printf '%s|' "${KEYS[@]}")"; PAT="${PAT%|}"
HITS=$(grep -rIl --exclude-dir=external --exclude-dir=__pycache__ --exclude-dir=.git \
       --exclude-dir=node_modules -E "$PAT" "$ROOT" 2>/dev/null \
       | grep -vE '/system/data/(index|graph|reference)/' \
       | grep -vE '/system/data/[^/]*\.(db|log|err|json)$' \
       | grep -v '/system/scratchpad/' \
       | grep -vE '\.env$' || true)

# 사전압축본(.json.gz)은 grep -I 가 바이너리로 보고 건너뛴다 — api/ shard 가 통째로
# 검사에서 빠지므로 풀어서 따로 훑는다(tools_compress_api.py 도입과 함께 생긴 구멍).
GZOUT="$(python "$ROOT/system/tools_audit_gz.py" "$ROOT" "$PAT" 2>/dev/null)"
GZN="$(find "$ROOT/system/data/api" -name '*.gz' 2>/dev/null | wc -l | tr -d ' ')"

if [ -z "$HITS" ] && [ -z "$GZOUT" ]; then
  echo "  ✅ 커밋 대상 키 노출 0건 (비밀키 ${#KEYS[@]}개 검사 · 압축 shard ${GZN}개 포함)"
  exit 0
else
  echo "  ⚠ 키 노출 발견 — push 금지:"
  [ -n "$HITS" ] && echo "$HITS" | sed 's/^/     /'
  [ -n "$GZOUT" ] && echo "$GZOUT" | sed 's/^/     [gz] /'
  exit 1
fi

#!/usr/bin/env python
"""사전압축 shard(.json.gz) 안의 키 노출 검사 — tools_audit_keys.sh 가 호출한다.

`grep -rI` 는 .json.gz 를 바이너리로 보고 건너뛴다. api/ shard 4,900여 개가 통째로
검사에서 빠지므로(= 압축을 도입하면서 생긴 안전망 구멍) 여기서 풀어서 훑는다.

    python system/tools_audit_gz.py <root> <정규식>

노출이 있으면 파일 경로를 한 줄씩 stdout 으로 내보내고 exit 1, 없으면 exit 0.
"""

from __future__ import annotations

import gzip
import os
import re
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: tools_audit_gz.py <root> <pattern>", file=sys.stderr)
        return 2
    root, pattern = argv[1], argv[2]
    pat = re.compile(pattern)
    scanned = 0
    hits: list[str] = []

    for base, _dirs, files in os.walk(os.path.join(root, "system", "data", "api")):
        for name in files:
            if not name.endswith(".gz"):
                continue
            path = os.path.join(base, name)
            scanned += 1
            try:
                body = gzip.decompress(open(path, "rb").read()).decode("utf-8", "replace")
            except Exception as exc:  # 깨진 파일도 노출만큼이나 알아야 한다
                hits.append(f"{path}  (읽기 실패: {exc})")
                continue
            if pat.search(body):
                hits.append(path)

    for h in hits:
        print(h)
    print(f"__SCANNED__ {scanned}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

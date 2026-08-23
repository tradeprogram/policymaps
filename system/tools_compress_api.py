#!/usr/bin/env python
"""배포본 정적 shard 사전압축 — api/ 하위 디렉터리의 .json 을 .json.gz 로 바꾼다.

왜 필요한가
-----------
StepA 에서 조례·법령·위임·신경망·의안을 전량 shard 로 구우면서 system/data/api 가
405MB 로 불어났다. 임무서의 배포 예산은 250MB 다. 여기서 데이터를 버리는 대신
(그러면 "빠짐없이 전부"라는 1차 목표와 충돌한다) 파일을 미리 gzip 으로 굽는다.
JSON 은 4~10배로 줄어 405MB → 약 108MB 가 되고 **한 건도 잃지 않는다**.

브라우저는 viz/public/js/api.js 의 getJSONFromBase() 가 처리한다. 서버가
Content-Encoding: gzip 을 붙여 주든(브라우저가 알아서 푼다) 안 붙이든(gzip 매직바이트
0x1f8b 를 보고 DecompressionStream 으로 직접 푼다) 양쪽 다 동작한다. 따라서
python -m http.server / serve_full.py / Vercel 어디서든 같은 파일이 그대로 쓰인다.

압축 대상 규칙 (api.js 의 GZIP_SHARD_RE 와 반드시 같아야 한다)
    api/<디렉터리>/**/*.json   → 압축한다   (지역·버킷 shard. 파일 수가 많고 크다)
    api/*.json                 → 놔둔다     (부팅 때 읽는 카탈로그. 규칙을 단순하게 유지)

    python system/tools_compress_api.py            # 압축
    python system/tools_compress_api.py --check    # 상태만 점검(쓰기 없음)
    python system/tools_compress_api.py --decompress   # 되돌리기

재실행 안전: 이미 .gz 인 것은 건너뛴다. gzip mtime 을 0 으로 고정해 같은 입력이면
같은 바이트가 나오므로, 다시 돌려도 git 이 변경으로 보지 않는다.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "system" / "data" / "api"
MANIFEST = "_compression.json"


def shard_files(api_dir: Path, suffix: str):
    """api/ 하위 디렉터리에 있는 suffix 파일만 (최상위 카탈로그는 제외)."""
    for path in sorted(api_dir.rglob("*" + suffix)):
        if path.parent == api_dir:
            continue  # api/*.json 최상위 카탈로그는 압축 대상이 아니다
        if path.name == MANIFEST:
            continue
        yield path


def compress(api_dir: Path, *, check: bool) -> dict:
    done = skipped = 0
    raw_bytes = gz_bytes = 0
    for src in shard_files(api_dir, ".json"):
        dst = src.with_suffix(".json.gz")
        data = src.read_bytes()
        raw_bytes += len(data)
        if check:
            gz_bytes += len(gzip.compress(data, 6, mtime=0))
            done += 1
            continue
        tmp = dst.with_suffix(".gz.tmp")
        # mtime=0: 같은 입력 → 같은 바이트. 재실행이 git diff 를 만들지 않는다.
        tmp.write_bytes(gzip.compress(data, 6, mtime=0))
        os.replace(tmp, dst)
        gz_bytes += dst.stat().st_size
        src.unlink()
        done += 1
    for existing in shard_files(api_dir, ".json.gz"):
        if existing.with_suffix("").with_suffix(".json").exists():
            continue
        skipped += 1
        gz_bytes += existing.stat().st_size
    return {"compressed": done, "already": skipped,
            "raw_bytes": raw_bytes, "gz_bytes": gz_bytes}


def decompress(api_dir: Path) -> dict:
    done = 0
    for src in shard_files(api_dir, ".json.gz"):
        dst = src.with_suffix("")  # x.json.gz -> x.json
        tmp = dst.with_suffix(".json.tmp")
        tmp.write_bytes(gzip.decompress(src.read_bytes()))
        os.replace(tmp, dst)
        src.unlink()
        done += 1
    return {"decompressed": done}


def survey(api_dir: Path) -> dict:
    """압축 여부와 무관하게 현재 api/ 총량을 잰다."""
    total = files = 0
    plain = gz = 0
    for r, _, fs in os.walk(api_dir):
        for f in fs:
            p = Path(r) / f
            total += p.stat().st_size
            files += 1
            if f.endswith(".json.gz"):
                gz += 1
            elif f.endswith(".json"):
                plain += 1
    return {"total_bytes": total, "files": files,
            "json_plain": plain, "json_gz": gz}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="배포본 정적 shard 사전압축")
    ap.add_argument("--api-dir", default=str(API_DIR))
    ap.add_argument("--check", action="store_true", help="쓰지 않고 예상 용량만 보고")
    ap.add_argument("--decompress", action="store_true", help="되돌리기")
    args = ap.parse_args(argv)

    api_dir = Path(args.api_dir)
    if not api_dir.is_dir():
        print(f"api 디렉터리가 없다: {api_dir}", file=sys.stderr)
        return 2

    before = survey(api_dir)
    t0 = time.time()
    if args.decompress:
        res = decompress(api_dir)
        verb = "복원"
    else:
        res = compress(api_dir, check=args.check)
        verb = "점검" if args.check else "압축"
    after = survey(api_dir)
    secs = time.time() - t0

    print("─" * 72)
    print(f"[{verb}] {res}")
    print(f"  이전 {before['total_bytes']/1048576:8.2f} MB  ({before['files']} 파일, "
          f"plain {before['json_plain']} / gz {before['json_gz']})")
    print(f"  이후 {after['total_bytes']/1048576:8.2f} MB  ({after['files']} 파일, "
          f"plain {after['json_plain']} / gz {after['json_gz']})")
    if args.check and res.get("raw_bytes"):
        est = before["total_bytes"] - res["raw_bytes"] + res["gz_bytes"]
        print(f"  압축하면 예상 {est/1048576:8.2f} MB "
              f"({res['raw_bytes']/max(res['gz_bytes'],1):.1f}배 축소)")
    print(f"  {secs:.1f}s")

    if not args.check:
        (api_dir / MANIFEST).write_text(json.dumps({
            "scheme": "gzip",
            "rule": "api/<dir>/**/*.json -> .json.gz (api/*.json 최상위 카탈로그는 비압축)",
            "reader": "viz/public/js/api.js getJSONFromBase() — "
                      "Content-Encoding 이 없으면 매직바이트를 보고 DecompressionStream 으로 푼다",
            "generator": "system/tools_compress_api.py",
            "state": "decompressed" if args.decompress else "compressed",
            "files": after["files"],
            "total_bytes": after["total_bytes"],
            "json_plain": after["json_plain"],
            "json_gz": after["json_gz"],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

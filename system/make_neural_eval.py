"""신경망 유사도 모델 평가 — 화면에 공시할 수치를 만든다.

왜 필요한가
    '모델 간 일치도' 만 보면 Jaccard 0 이라는 사실밖에 알 수 없고, 그것이
    (a) 모델이 서로 다른 타당한 이웃을 고른 것인지 (b) 일부 모델이 사실상
    무작위인지 구분되지 않는다. 그래서 **무작위 기준선과 비교한 분야 일치율**을
    같이 낸다. 기준선 없는 정확도 수치는 해석할 수 없다.

지표
    category_agreement  이웃이 원 조례와 분야(ordinance_category) 코드를 하나라도
                        공유하는 비율. 이웃 top-k 에 대해 평균.
    random_baseline     같은 표본에서 이웃을 무작위로 뽑았을 때의 같은 지표.
                        조례 대부분이 C01(행정) 같은 흔한 분야를 갖고 있어
                        기준선이 30% 대로 높다 — 이걸 모르면 34% 를 '괜찮다'고 오독한다.
    lift                category_agreement / random_baseline
    region_spread       top-k 이웃이 몇 개 서로 다른 지자체에서 왔는가(평균).
                        '다른 지자체는 이걸 어떻게 만들었나' 가 이 기능의 용도라
                        한 지역에 몰리면 쓸모가 없다.
    same_region_share   같은 지자체 이웃 비율. make_neural_cross.py 적용 후에는 0 이어야 한다.

산출
    system/data/api/neural_eval.json   (make_gap_fixtures.envelope 봉투 규약)

사용
    python system/make_neural_eval.py --sample 400
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics as st
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from policymap import db as D
from policymap.config import load_config

PREFIX = "ordinance:"
strip = lambda x: x[len(PREFIX):] if x.startswith(PREFIX) else x


def chunked_map(conn, sql: str, ids: list[str], collect):
    out: dict = {}
    for i in range(0, len(ids), 900):
        ch = ids[i:i + 900]
        rows = D.fetchall(conn, sql.format(q=",".join("?" * len(ch))), ch)
        for r in rows:
            collect(out, r)
    return out


def evaluate(conn, model: str, sample: int, top_k: int, seed: int) -> dict:
    rows = D.fetchall(
        conn,
        "SELECT src_id, dst_id, rank FROM neural_similarity "
        "WHERE model_name=? AND node_kind='Ordinance' AND rank<=? AND src_id IN ("
        "  SELECT src_id FROM neural_similarity WHERE model_name=? AND node_kind='Ordinance'"
        "  GROUP BY src_id ORDER BY RANDOM() LIMIT ?)",
        (model, top_k, model, sample))
    if not rows:
        return {"model": model, "n": 0, "note": "이 모델의 kNN 결과가 없다"}

    ids = sorted({strip(r["src_id"]) for r in rows} | {strip(r["dst_id"]) for r in rows})
    cat = chunked_map(
        conn, "SELECT ordinance_id, category_code FROM ordinance_category WHERE ordinance_id IN ({q})",
        ids, lambda o, r: o.setdefault(r["ordinance_id"], set()).add(r["category_code"]))
    reg = chunked_map(
        conn, "SELECT ordinance_id, region_id FROM ordinances WHERE ordinance_id IN ({q})",
        ids, lambda o, r: o.__setitem__(r["ordinance_id"], r["region_id"]))

    per_cat: dict[str, list[int]] = {}
    per_reg: dict[str, list[str]] = {}
    per_same: dict[str, list[int]] = {}
    for r in rows:
        s, d = strip(r["src_id"]), strip(r["dst_id"])
        a, b = cat.get(s), cat.get(d)
        if a:
            per_cat.setdefault(s, []).append(1 if (b and a & b) else 0)
        per_reg.setdefault(s, []).append(reg.get(d) or "")
        per_same.setdefault(s, []).append(1 if (reg.get(s) and reg.get(s) == reg.get(d)) else 0)

    agree = [sum(v) / len(v) for v in per_cat.values() if v]
    spread = [len({x for x in v if x}) for v in per_reg.values() if v]
    same = [sum(v) / len(v) for v in per_same.values() if v]

    # 무작위 기준선 — 같은 후보 풀에서 이웃만 무작위로 바꾼다
    rng = random.Random(seed)
    pool = [i for i in ids if i in cat]
    base = []
    for s in list(per_cat)[:len(agree)]:
        a = cat.get(s)
        if not a or len(pool) < top_k:
            continue
        picks = rng.sample(pool, top_k)
        base.append(sum(1 for d in picks if cat.get(d) and a & cat[d]) / top_k)

    ca = st.mean(agree) if agree else 0.0
    rb = st.mean(base) if base else 0.0
    return {
        "model": model,
        "n_sources": len(per_cat),
        "top_k": top_k,
        "category_agreement": round(ca, 4),
        "random_baseline": round(rb, 4),
        "lift": round(ca / rb, 3) if rb else None,
        "zero_agreement_sources": sum(1 for x in agree if x == 0),
        "region_spread_mean": round(st.mean(spread), 2) if spread else None,
        "single_region_sources": sum(1 for v in per_reg.values() if len({x for x in v if x}) == 1),
        "same_region_share": round(st.mean(same), 4) if same else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config()
    conn = D.connect(cfg.db_path)
    conn.execute("PRAGMA busy_timeout = 180000")

    models = [r["model_name"] for r in D.fetchall(
        conn, "SELECT DISTINCT model_name FROM neural_similarity "
              "WHERE node_kind='Ordinance' ORDER BY model_name")]
    results = []
    for m in models:
        r = evaluate(conn, m, args.sample, args.top_k, args.seed)
        results.append(r)
        print(f"  {m:24s} 분야일치 {r.get('category_agreement', 0):.1%} "
              f"기준선 {r.get('random_baseline', 0):.1%} "
              f"lift {r.get('lift')} 지역폭 {r.get('region_spread_mean')}", flush=True)

    ranked = sorted([r for r in results if r.get("lift") is not None],
                    key=lambda x: -x["lift"])
    payload = {
        "method": {
            "metric": "이웃이 원 조례와 분야(ordinance_category) 코드를 하나라도 공유하는 비율",
            "sample": args.sample, "top_k": args.top_k, "seed": args.seed,
            "baseline": "같은 후보 풀에서 이웃만 무작위로 뽑은 같은 지표. "
                        "조례 다수가 C01(행정) 같은 흔한 분야를 가져 기준선이 30%대다 — "
                        "기준선 없이 절대값만 보면 오독한다.",
            "caveat": "분야 라벨 자체가 자동 분류 결과다(전체 정밀도 64.9%, 신뢰도 0.8 이상 구간 93.2%). "
                      "따라서 이 수치는 모델 간 상대 비교용이고 절대 정확도가 아니다.",
        },
        "models": results,
        "best_model": ranked[0]["model"] if ranked else None,
        "ranking": [{"model": r["model"], "lift": r["lift"]} for r in ranked],
    }

    out_dir = Path(args.out or (Path(cfg.out_dir) / "api"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "neural_eval.json"
    try:
        # envelope(data, **extra) 는 extra 를 봉투 최상위에 그대로 넣는다.
        # conn 같은 객체를 넘기면 직렬화에서 터진다.
        from make_gap_fixtures import envelope
        doc = envelope(payload)
    except Exception:
        doc = {"data": payload}
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    print(f"\n  → {path}  ({path.stat().st_size/1024:.1f}KB)  best={payload['best_model']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

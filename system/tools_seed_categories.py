"""C01~C14 정책분야 통제어휘 시드 + 전국 조례 전건 분류.

기존 C-BIRTH/C-PET 2종만 있어 ordinance_category 커버리지가 0.68%에 머물렀고,
그 결과 find_peer_governments 의 '정책구조' 특성이 변별력 0이 되는 병목이었다.
분류체계는 조례명 159,452건의 키워드 빈도 분석에 근거해 설계했다.
"""
import json, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from policymap import db as D
from policymap.parsers import category as CAT

TAXONOMY = [
    ("C01", "행정·자치·의회", ["의회", "의원", "공무원", "지방공무원", "위원회", "정원", "복무", "공인",
                              "여비", "행정기구", "감사", "청사", "민원", "포상", "표창", "자치"]),
    ("C02", "재정·세무·회계", ["세", "시세", "군세", "구세", "징수", "특별회계", "기금", "출자", "출연",
                              "예산", "보조금", "재정", "회계", "채권", "지방채", "사용료", "수수료"]),
    ("C03", "복지·돌봄", ["복지", "장애인", "노인", "아동", "기초생활", "돌봄", "요양", "한부모", "저소득",
                          "취약계층", "사회복지", "자활", "긴급지원", "경로당", "국가유공자", "보훈"]),
    ("C04", "인구·출산·양육", ["출산", "임신", "산후", "양육", "보육", "다자녀", "인구", "난임", "모자",
                              "어린이집", "육아", "저출산"]),
    ("C05", "청년·교육", ["청년", "교육", "장학", "학교", "평생학습", "청소년", "학생", "대학", "교육경비",
                          "방과후", "진로", "돌봄교실"]),
    ("C06", "보건·의료", ["보건", "의료", "감염병", "건강", "병원", "보건소", "정신건강", "치매", "금연",
                          "예방접종", "응급의료", "약국", "위생", "식품위생"]),
    ("C07", "환경·기후", ["환경", "기후", "탄소", "온실가스", "폐기물", "재활용", "대기", "수질", "녹지",
                          "하수", "정화조", "소음", "미세먼지", "에너지", "신재생", "자원순환"]),
    ("C08", "안전·재난", ["안전", "재난", "소방", "방재", "화재", "재해", "구조", "구급", "민방위",
                          "풍수해", "지진", "안전관리", "위험물", "산사태"]),
    ("C09", "도시·건축·주택", ["도시", "건축", "주택", "공동주택", "도시계획", "경관", "개발", "재개발",
                              "재건축", "빈집", "정비사업", "택지", "지구단위", "옥외광고"]),
    ("C10", "교통", ["교통", "도로", "주차", "주차장", "자전거", "버스", "대중교통", "택시", "화물",
                     "교통약자", "보행", "터미널", "철도"]),
    ("C11", "경제·산업·일자리", ["기업", "산업", "일자리", "고용", "소상공인", "창업", "투자", "중소기업",
                               "전통시장", "상인", "노동", "근로자", "경제", "유통", "물류"]),
    ("C12", "농림·수산", ["농업", "농촌", "축산", "임업", "산림", "수산", "어촌", "어업", "농산물",
                          "귀농", "귀어", "산지", "가축", "농기계", "food"]),
    ("C13", "문화·체육·관광", ["문화", "체육", "관광", "예술", "축제", "도서관", "박물관", "미술관",
                              "공연", "스포츠", "생활체육", "문화재", "관광지", "야영장"]),
    ("C14", "동물·반려", ["반려동물", "반려견", "유기동물", "동물보호", "동물", "길고양이", "야생동물",
                          "동물등록", "동물복지"]),
]


def main():
    conn = D.connect()
    t0 = time.time()
    from policymap.util import now_kst_iso
    now_iso = now_kst_iso()
    # 1) 통제어휘 시드
    with D.tx(conn):
        for code, name, kws in TAXONOMY:
            conn.execute(
                "INSERT INTO categories(code,name,level,parent_code,definition,keywords) "
                "VALUES(?,?,1,NULL,?,?) ON CONFLICT(code) DO UPDATE SET "
                "name=excluded.name, definition=excluded.definition, keywords=excluded.keywords",
                (code, name, f"{name} 관련 지방자치단체 자치법규", json.dumps(kws, ensure_ascii=False)),
            )
    cats = [dict(r) for r in conn.execute(
        "SELECT code,name,level,parent_code,definition,keywords FROM categories WHERE code LIKE 'C__'")]
    print(f"[seed] 통제어휘 {len(cats)}종 시드 완료", flush=True)

    # 2) 기존 임시 분류(C-BIRTH/C-PET) 정리
    with D.tx(conn):
        n = conn.execute("DELETE FROM ordinance_category WHERE category_code LIKE 'C-%'").rowcount
    print(f"[clean] 구 분류 {n:,}행 삭제", flush=True)

    # 3) 전건 분류 (조례명 + 목적문)
    BATCH = 5000
    wm = D.get_watermark(conn, "category_clf", "all") or {}
    cursor = (wm.get("cursor") or "") if isinstance(wm, dict) else ""
    total, saved = 0, 0
    print(f"[resume] 커서='{cursor}'", flush=True)
    while True:
        rows = conn.execute(
            "SELECT ordinance_id, name, COALESCE(article_count,0) FROM ordinances "
            "WHERE ordinance_id>? ORDER BY ordinance_id LIMIT ?",
            (cursor, BATCH)).fetchall()
        if not rows:
            break
        # [최적화] 조례별 개별 조문 쿼리(159k회)가 병목이었다. 본문 보유 조례만
        # 배치 IN 조회로 한 번에 가져온다. 미보유 조례는 조례명만으로 분류.
        # 본문 보유 조례(article_count>0)만 조문을 조회한다. 대부분은 미보유라
        # 전체를 IN 절에 넣으면 인덱스 이점 없이 느려진다. 500개씩 끊어 조회.
        with_body = [r[0] for r in rows if r[2] > 0]
        arts_by: dict = {}
        for i in range(0, len(with_body), 500):
            grp = with_body[i:i + 500]
            ph = ",".join("?" * len(grp))
            for oid, ti, bo in conn.execute(
                    f"SELECT ordinance_id,title,body FROM ordinance_articles "
                    f"WHERE ordinance_id IN ({ph})", grp):
                lst = arts_by.setdefault(oid, [])
                if len(lst) < 3:
                    lst.append({"title": ti, "body": bo})
        payload = []
        for oid, nm, _ac in rows:
            res = CAT.classify_ordinance({"ordinance_id": oid, "name": nm},
                                         arts_by.get(oid, []), cats, top_k=2)
            for r in res:
                if float(r.get("confidence") or 0) >= 0.24:
                    payload.append({
                        "ordinance_id": oid,
                        "category_code": r["category_code"],
                        "confidence": r.get("confidence"),
                        "method": r.get("method") or "rule",
                        "computed_at": now_iso,
                    })
        for attempt in range(8):
            try:
                # [최적화] save_categories 는 호출마다 자체 트랜잭션을 열어
                # 조례당 커밋 1회가 발생한다(실측 361행 37초). 배치 단위 1커밋으로 대체.
                with D.tx(conn):
                    if payload:
                        D.upsert_many(conn, "ordinance_category", payload,
                                      ("ordinance_id", "category_code"))
                break
            except sqlite3.OperationalError as e:
                if "lock" not in str(e).lower():
                    raise
                print(f"  [lock] 재시도 {attempt+1}", flush=True); time.sleep(20)
        cursor = rows[-1][0]; total += len(rows); saved += len(payload)
        try:
            with D.tx(conn):
                D.set_watermark(conn, "category_clf", "all", cursor=cursor, status="ok")
        except sqlite3.OperationalError:
            pass
        print(f"[batch] 조례 {total:,} 처리 / 분류 {saved:,}행 | {time.time()-t0:.0f}s", flush=True)
    print(f"[finish] {total:,}건 분류, {saved:,}행 저장, {(time.time()-t0)/60:.1f}분", flush=True)


if __name__ == "__main__":
    main()

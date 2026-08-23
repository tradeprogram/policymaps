# 보안 정책

## 비밀정보 취급

이 저장소는 공공 API 키 5종과 Gemini 키를 사용한다. **키는 저장소에 들어가지 않는다.**

| 위치 | 내용 | 상태 |
|---|---|---|
| `system/.env` | 실제 키 | `.gitignore` 로 차단 |
| `system/.env.example` | 형식만(빈 값) | 커밋됨 |
| GitHub Actions | `secrets.*` 참조 | 저장소 Secrets |
| Vercel / Render | 대시보드 환경변수 | `sync: false` |

## 푸시 전 검사

수집한 데이터의 `official_url` 에 API 키가 쿼리스트링으로 섞여 들어가는 사고가 실제로
있었다(230,691건, 커밋 `e30bcdd`). 재발을 막기 위해 자동 검사를 둔다.

```bash
bash system/tools_audit_keys.sh
```

`0건` 이 아니면 푸시하지 않는다. 이 검사는 사전압축된 `.json.gz` shard 4,953개도
풀어서 본다(`system/tools_audit_gz.py`) — `grep` 이 바이너리로 건너뛰는 구멍을 막기 위해서다.

살균이 필요하면:

```bash
python system/tools_sanitize_keys.py
```

## 취약점 신고

보안 문제를 발견하면 공개 이슈 대신 저장소 소유자에게 직접 알려 주기 바란다.
공개 이슈로 올리면 수정 전에 노출된다.

## 개인정보

- 수집 대상은 **공표된 법령·조례·의안·표결·예산** 이며 개인정보를 수집하지 않는다.
- 국회의원 정보(성명·정당·표결)는 「공공기관의 정보공개에 관한 법률」에 따라
  열린국회정보가 공표하는 공적 활동 기록이다.

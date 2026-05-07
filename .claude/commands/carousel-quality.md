---
description: 이미 생성된 소아과언니 카드뉴스 폴더의 9장 PNG + sources.json 출처 매핑 재검증
argument-hint: <topic 폴더명 또는 경로>
---

운영자가 **"$ARGUMENTS"** 카드뉴스 폴더의 품질·출처 재검증을 요청했습니다.

이 명령은 이미 생성된 캐러셀(`output/<topic>/`)을 다시 채점하고, sources.json 매핑까지 재검증합니다.

---

## 0. 사전 로드 (생략 금지)

1. `output/<topic>/sources.json`
2. `output/<topic>/brief.json`
3. `knowledge/source-policy.md`
4. `knowledge/banned-words.json`
5. `knowledge/patterns/sources-schema.md`
6. `knowledge/patterns/carousel-structure.md`

---

## 1. 자동 검사 (스크립트)

```bash
node scripts/quality-check.js --dir output/$ARGUMENTS
```

출력:
- 9장 PNG 존재 여부
- 해상도 1080×1350 일치 개수
- 파일 크기 분포 (너무 작으면 깨진 이미지)
- (있다면) `templates/slides.<topic>.json` 또는 `output/<topic>/slides/*.html` 스키마 검증

---

## 2. 정성 + 출처 매핑 리뷰

**`carousel-reviewer` 서브에이전트 dispatch**:

> output/$ARGUMENTS/ 의 9장 PNG 와 sources.json 을 검증해줘.
>
> 1. 자동 FAIL 8가지 점검:
>    - sources.json 미존재 / claims[] 비어있음
>    - source_tier 가 1·2 가 아님
>    - source_citation 비어있음
>    - 본문에 의학적 사실 주장 orphan 존재
>    - 슬라이드 9 출처 박스 미표기
>    - banned-words.json 위반
>    - 개인정보 노출 (실명·병원·지역·가족)
>    - 인스타 핸들 오타
>
> 2. 경고 4가지 점검:
>    - publication_date 5년 이상 (reaffirmed 표기 없음)
>    - applicable_age 본문 미명시
>    - UpToDate last_accessed_at 누락
>    - 본문 vs claim 연령 표기 불일치
>
> 3. 10항목 점수 채점 + 한글 오타 slide별 육안 확인
>
> 4. 본문 ↔ sources.json claims 1:1 매핑 검증 (의학적 사실 주장 정의 기준)
>
> 5. sources.json `verification` 섹션 + 각 claim 의 `reviewer_pass` 마킹
>
> 6. 판정 (PASS / HOLD / FAIL) — 자동 FAIL 1건이라도 있으면 FAIL
>
> 직접 고치지 말고 지적만.

---

## 3. 리포트 저장

- `output/$ARGUMENTS/quality-report.md` 자동 저장 (덮어쓰기, 이전 버전은 `quality-report.YYYY-MM-DD.md` 로 백업)
- `output/$ARGUMENTS/sources.json` 의 verification 섹션 갱신

---

## 4. 운영자 보고

```
🔍 "$ARGUMENTS" 재검증 결과
📁 output/<topic>/

## 자동 FAIL: 0건 / N건
(거부 사유 나열)

## 경고: 0건 / N건
(노후화·연령·접근일 등 나열)

## 총점: NN/100 (이전 NN → 현재 NN)

## 판정: PASS / HOLD / FAIL

## 상위 개선 포인트 3개
1. ...
2. ...
3. ...

## 운영자 판단 필요
- 재생성 필요 슬라이드: slide-XX
- 업로드 추천: YES / NO / 보류
```

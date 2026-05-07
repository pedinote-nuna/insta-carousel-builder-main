# sources-schema.md — 출처 매핑 규약 (sources.json)

> 모든 카드뉴스의 사실 주장은 `output/<topic>/sources.json` 으로 추적된다.
> researcher 가 초안 생성 → writer 가 본문 매핑 유지 → reviewer 가 검증.
> 누락 1건이라도 있으면 발행 거부.

---

## 파일 위치

`output/<YYYY-MM-DD>_<topic>/sources.json`

---

## JSON 스키마

```json
{
  "topic": "fever-clothing",
  "topic_kr": "열나는 아이, 옷 벗기지 마세요",
  "palette": "B",
  "category": "응급",
  "created_at": "2026-05-08",
  "researcher": "carousel-researcher",

  "claims": [
    {
      "claim_id": "S03-C1",
      "slide_number": 3,
      "claim_text": "오한이 발생하면 떨림으로 인한 열 생산이 늘어 체온이 더 상승할 수 있다.",
      "applicable_age": "전 연령",
      "source_tier": 1,
      "source_type": "guideline",
      "source_citation": "AAP Clinical Practice Guideline. Fever and Antipyretic Use in Children. Pediatrics. 2011;127(3):580-587 (reaffirmed)",
      "source_url": "https://publications.aap.org/pediatrics/article/127/3/e20103852/65016/Fever-and-Antipyretic-Use-in-Children",
      "secondary_url": "https://pubmed.ncbi.nlm.nih.gov/21357332/",
      "publication_date": "2011-03",
      "publication_date_estimated": false,
      "last_accessed_at": "2026-05-08",
      "page_or_section": "Pathophysiology",
      "verified_by": "researcher",
      "verified_at": "2026-05-08",
      "writer_used": null,
      "reviewer_pass": null,
      "reviewer_note": null
    }
  ],

  "summary": {
    "total_claims": 0,
    "tier_1_count": 0,
    "tier_2_count": 0,
    "claims_by_slide": {
      "1": 0, "2": 0, "3": 0, "4": 0, "5": 0,
      "6": 0, "7": 0, "8": 0, "9": 0
    }
  },

  "verification": {
    "all_claims_have_sources": null,
    "all_sources_tier_1_or_2": null,
    "no_orphan_claims_in_body": null,
    "reviewer_pass": null,
    "reviewer_notes": null,
    "reviewed_at": null
  }
}
```

---

## 필드 설명

### 최상위 메타
| 필드 | 의미 | 예시 |
|:---|:---|:---|
| `topic` | 영문 슬러그 (파일명에 사용) | `fever-clothing` |
| `topic_kr` | 한글 주제 | `열나는 아이, 옷 벗기지 마세요` |
| `palette` | `A` 또는 `B` (`brand-facts.md` 참조) | `B` |
| `category` | `topic-selection.md` 카테고리 | `응급` |
| `created_at` | researcher 가 작성한 날짜 (ISO) | `2026-05-08` |
| `researcher` | 에이전트 식별자 | `carousel-researcher` |

### "의학적 사실 주장" 정의 (claims[] 매핑 대상 판정)

본문에 등장하는 모든 문장이 매핑 대상은 아니다. 다음 기준으로 판정한다.

**판단 한 줄**: **"이게 틀렸을 때 의학적 위험이 발생하는가?"**
- YES → claim 매핑 **필수**
- NO → 매핑 **불필요**

| 매핑 필수 (의학적 사실 주장) | 매핑 불필요 (정서·강조·CTA·상식) |
|:---|:---|
| 숫자·기준값 (예: "체온 38도", "체중당 10mg/kg") | 공감·정서 표현 ("당황스럽죠", "괜찮아요") |
| 약물명·질환명 | 강조·지시문 ("꼭 기억해주세요", "잊지 마세요") |
| 메커니즘 (예: "오한 → 체온 상승") | CTA ("저장하기", "공유해주세요") |
| 가이드라인 권고사항 ("…을 권고한다") | 일반 상식 ("아이들은 잘 아파요") |
| 기간·시점·연령 기준 (예: "3개월 미만", "48시간 이내") | 브랜드 메시지 ("소아과수첩 앱에서 관리하세요") |
| 응급/위험 신호 (예: "경련 시 즉시 119") | 후킹 헤드라인 자체 (사실은 본문 슬라이드에서 출처와 함께 다뤄야) |

이 정의는 researcher 가 claims 추출할 때, writer 가 본문 작성할 때, reviewer 가 orphan 검출할 때 **공통 기준**.

---

### claims[] (사실 주장 배열)
| 필드 | 의미 | 작성 주체 | 필수 |
|:---|:---|:---|:---:|
| `claim_id` | `S<슬라이드번호 2자리>-C<순번>` (예: `S03-C1`) | researcher | ✅ |
| `slide_number` | 1~9 (Cover/Outro 포함 가능) | researcher | ✅ |
| `claim_text` | 본문에 등장하는 사실 진술 1줄 | researcher | ✅ |
| `applicable_age` | 적용 대상 연령 (예: `전 연령` / `0-6개월` / `영유아` / `학령기` / `청소년`) | researcher | ✅ |
| `source_tier` | `1` 또는 `2` (3은 보조용이라 단독 인용 불가) | researcher | ✅ |
| `source_type` | `guideline` / `textbook` / `journal` / `uptodate` / `gov_agency` | researcher | ✅ |
| `source_citation` | `source-policy.md` 인용 형식 | researcher | ✅ |
| `source_url` | 직접 링크 (없으면 `null`) | researcher | ✅ |
| `secondary_url` | 보조 미러 URL (PubMed·NCBI Books·AAFP summary 등). 본 URL 이 봇 차단된 경우 검증·재현 보존용 | researcher | 옵션 |
| `publication_date` | 가이드라인·논문 발행일 `YYYY-MM` 또는 `YYYY-MM-DD` (5년 이상 시 reviewer 경고) | researcher | ✅ |
| `publication_date_estimated` | 발행일이 페이지에 미표기되어 보수 추정한 경우 `true` (한국 학회 페이지 등). 기본 `false` | researcher | 옵션 |
| `last_accessed_at` | URL 접근 일자 `YYYY-MM-DD` (UpToDate·웹 자료 인용 시 필수) | researcher | UpToDate ✅ / 그 외 권장 |
| `page_or_section` | 페이지/장/섹션 (교과서·UpToDate 등) | researcher | 권장 |
| `verified_by` | `researcher` (초기) | researcher | ✅ |
| `verified_at` | ISO 날짜 | researcher | ✅ |
| `writer_used` | writer 가 본문에 반영했으면 `true` | writer | ✅ |
| `reviewer_pass` | reviewer 검증 통과 여부 | reviewer | ✅ |
| `reviewer_note` | reviewer 코멘트 (이슈 있을 때만) | reviewer | 옵션 |

### summary (researcher 가 채움, reviewer 가 재계산)
- `total_claims`: claims[] 길이
- `tier_1_count` / `tier_2_count`: tier 별 카운트
- `claims_by_slide`: 슬라이드별 claim 개수

### verification (reviewer 만 채움)
- `all_claims_have_sources`: 모든 claim 에 source_citation 있는가
- `all_sources_tier_1_or_2`: 모든 source_tier 가 1 또는 2 인가
- `no_orphan_claims_in_body`: 본문에 등장하는 사실 주장 중 sources.json 에 없는 것이 있는가 (`true` = 없음 = 통과)
- `reviewer_pass`: 위 셋 모두 통과 + 본문 ↔ claims 매핑 검증 통과
- `reviewer_notes`: 거부 사유 / 코멘트
- `reviewed_at`: ISO 날짜

---

## 책임 분배 (절대 침범 금지)

### 🔬 researcher (carousel-researcher)
- `output/<topic>/sources.json` **초안 생성**
- 슬라이드 outline 작성 시점에 동시에 사실 주장 ↔ Tier 1·2 출처 매핑
- `writer_used`, `reviewer_pass`, `reviewer_note` 는 `null` 로 둠
- summary 계산까지 책임
- **출처 부족하면 그 슬라이드를 outline 에서 빼고 운영자에게 보고** (만들어내지 말 것)

### ✍️ writer (carousel-prompt-writer / carousel-html-writer)
- sources.json 을 **읽고**, 본문 슬라이드에 해당 `claim_text` 가 명확히 표현되도록 프롬프트/HTML 작성
- 각 슬라이드(JSON 또는 HTML)에 `data-claim-ids` 또는 `claim_ids` 필드로 매핑 표시
- 사용한 claim 의 `writer_used` 를 `true` 로 업데이트
- **새로운 사실 주장을 추가하지 말 것** — 새 사실이 필요하면 researcher 재호출

### 🔍 reviewer (carousel-reviewer)
- sources.json 의 `verification` 섹션 채움
- 본문(이미지 내 텍스트 또는 HTML)에 등장하는 사실 주장이 모두 claims[] 에 존재하는지 검증
- claim 누락 / Tier 3 이하 / orphan claim 발견 시 → **발행 거부 (FAIL)**
- 각 claim 의 `reviewer_pass` 를 `true`/`false` 로 마킹
- 거부 사유는 `reviewer_notes` 에 기록

---

## 거부 사유 (reviewer 자동 FAIL)

다음 중 하나라도 해당하면 즉시 FAIL, 다른 항목 점수와 무관하게 발행 거부:

1. `sources.json` 파일 자체가 없음
2. `claims[]` 배열이 비어있음
3. 한 claim 이라도 `source_tier` 가 1·2 가 아님
4. 한 claim 이라도 `source_citation` 이 비어있거나 `null`
5. 본문에 **위 "의학적 사실 주장" 정의에 해당하는 진술**이 있는데 claims[] 에 매칭이 없음 (orphan)
   - 정서·강조·CTA·일반 상식·브랜드 메시지는 매핑 불필요 (FAIL 사유 아님)
6. 슬라이드 9(Outro)에 출처 박스가 시각적으로 표기되지 않음

---

## 경고 사유 (reviewer WARN, FAIL 은 아님)

자동 FAIL 은 아니지만 reviewer 가 `reviewer_note` 에 기록하고 운영자에게 표시:

1. **출처 노후화**: `publication_date` 가 5년 이상 지난 가이드라인·논문
   → 최신판으로 교체 가능한지 확인 권고. 단, "reaffirmed" 표기가 있으면 OK.
2. **연령 명시 누락**: claim 의 `applicable_age` 가 "전 연령" 이 아닌데, 본문 슬라이드에 해당 연령 기준이 표시되지 않음
   → 예: claim 이 "0-6개월"인데 본문에 "신생아·영유아" 표기가 없으면 경고.
3. **UpToDate 접근일 누락**: `source_type: "uptodate"` 인데 `last_accessed_at` 이 비어있음.
4. **연령 표기 불일치**: 본문에 "12개월 미만"이라 적었는데 claim 에는 "6개월 미만"으로 되어 있는 경우.

---

## 운영 룰 — AAP 봇 차단 시 보조 검증 경로

`publications.aap.org` 가 봇 차단(403)으로 직접 검증이 불가할 때, researcher 는 다음 순서로 시도하고 결과를 `_webfetch-log.md` 에 기록:

1. **PubMed 미러** — `pubmed.ncbi.nlm.nih.gov` (PMID 검색)
2. **NCBI Bookshelf** — `ncbi.nlm.nih.gov/books` (NICE 가이드라인 등)
3. **AAFP summary** — American Academy of Family Physicians 의 요약 페이지

세 곳 다 안 되면 그 출처는 sources.json 에서 **제외**하고 운영자에게 보고한다.

검증된 미러 URL 은 claim 의 `secondary_url` 필드에 저장 — 발행 후 누군가 출처를 재확인할 때 봇 차단 우회 경로로 활용.

---

## 운영 룰 — 한국 학회 페이지 발행일 미표기 시

대한소아청소년과학회·대한소아응급의학회 등 한국 학회의 일반인 안내 페이지는 종종 게재일·검토일이 명시되어 있지 않다. researcher 는 다음 순서로 처리:

1. **PDF·공식 보도자료** 에서 정확한 일자 확인 시도 (학회 publications 섹션, 보도자료 아카이브 등)
2. 그래도 없으면 페이지의 **마지막 수정일** 또는 **보수 추정값** 사용 (예: 검색 인덱스 기준 첫 노출 월)
3. `publication_date_estimated: true` 마킹 + `reviewer_note` 에 추정 근거 한 줄 기록

reviewer 는 `publication_date_estimated: true` 가 있으면 정성 검수에서 한 번 더 확인 (정확한 일자가 본 학회 사이트의 다른 페이지에 명시되어 있을 수도).

---

## 관련 파일
- `knowledge/source-policy.md` — Tier 정의 + 인용 형식
- `knowledge/patterns/carousel-structure.md` — 9슬라이드 구조 + reviewer 거부 사유 (구조 측면)
- `knowledge/banned-words.json` — 표현 검증

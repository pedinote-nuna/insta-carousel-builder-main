# 클린 인포그래픽 톤 (Clean Infographic)

## 정의
가이드라인 기반 의학 정보를 데이터 중심으로 명료하게 전달하는 톤.
white background + navy 텍스트 + teal/coral 액센트 + 카드형 레이아웃.
부모님들이 한 눈에 핵심 수치·단계·기준을 파악할 수 있게 함.

## 적합한 토픽
- 학회 가이드라인 기반 권장치 (수면 시간, 영양 권장량 등)
- 연령별·체중별 수치 가이드 (해열제 용량, 체온 임계치)
- 단계·등급 비교 (정상 vs 위험 / 즉시 vs 당일 vs 관찰)
- 체크리스트형 신호 모음 (응급 신호, 재진료 신호)
- 데이터·근거가 콘텐츠의 핵심 자산인 주제

## 디자인 토큰
- 배경: white #FFFFFF
- 메인 텍스트: deep navy #1A1F36
- 메인 액센트: medical teal #2C6E63 (헤드라인 강조 단어 underline + 정보 카드 강조)
- 경고 액센트: coral #C44536 (응급·금지·위험 표시 한정)
- 카드 베이스: light gray #F0F4F8
- 카드 변형: light amber #FFF3E0, light teal #E8F4F2 (단계별 색상 코딩)
- 폰트: Pretendard 계열 한글 sans-serif (시그니처는 SemiBold)

## 레이아웃 패턴
- 포맷: 1080×1350 portrait, 외곽 margin 상하좌우 충분히
- 시그니처: 우상단 '소아과언니' (매 슬라이드 공통, 장식·뱃지 없음)
- 라벨: 좌상단 작은 영문/한글 라벨 (예: 'EMERGENCY' · '체온 측정' · 'CHECK')
- 헤드라인: 마지막 단어 teal underline (응급 슬라이드만 coral underline)
- 본문: 카드 stack(2-3개) 또는 numbered list — 카드 내부 padding 넉넉히, 카드 간 vertical spacing 확보
- 시각 요소: 각 슬라이드에 일러스트/아이콘 1개 이상 크게 배치 (텍스트만 가득 찬 구성 금지)
- 단계별 색 코딩: 응급 = coral 배경 / 당일 = light amber / 관찰 = light teal
- Outro(slide-09): 출처 박스(light gray) → '소아과언니' brand block → divider → @soagwa_unnie + 매주 새 가이드 → 소아과수첩 앱 CTA

## 렌더링 가드 (모델 실수 패턴 방지)
- 숫자+px 형태(40px, 80px 등)는 절대 카드 위에 글자로 표시 금지
- 따옴표(' ' / " ")를 카드 위에 그리지 말 것
- 픽셀값·좌표·position 표기를 visible text 로 출력 금지
- '소아과언니' 4글자 외 장식·뱃지·픽셀값 추가 금지
- 텍스트 크기는 compact 기조 — 한 슬라이드에 정보 과밀 금지 (compact 가이드: 표준 대비 약 20% 축소 권장)
- 카드 내부 padding: 내용과 테두리 사이 여백 충분히 (cramped 금지)
- 시각 요소 1개 이상 필수 — 텍스트 도배 슬라이드 거부

## 소아과수첩 앱 CTA 매칭 기준
토픽별로 가장 관련 있는 기능 1개 선택:
- 열·해열제 관련 → 해열제 계산기 ("체중별 해열제 용량 바로 계산")
- 수면 문제 → 진료 메모 ("수면 문제 진료 전 메모해두기")
- 영양·이유식·성장 → 식사·성장 기록
- 약 복용·처방 → 약봉투 스캔
- 야간 증상·응급 → 야간·달빛병원 바로 찾기

## 검증 완료 카드뉴스
- sleep-duration (연령별 수면 권장 시간) — 2026-05-10
  output/sleep-duration/slide-01~09.png
  templates/slides.sleep-duration.json
- fever-stages (영아 발열 단계별 대처) — 2026-05-10
  output/fever-stages/slide-01~09.png
  templates/slides.fever-stages.json
  (v2: 텍스트 약 20% 축소 + 일러스트 강조 + padding/margin 확보)

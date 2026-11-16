# 카페발주 단가표

카페 사장님이 발주 품목의 **상품 가격**과 **개당 · 100ml당 · 100g당 단가**를 12개 카테고리로 비교하는 모바일 웹 페이지입니다. 목록에는 **로켓배송이면서 상품명에 용량·수량이 적혀 단가를 계산할 수 있는 상품만** 싣습니다.

- 공개 주소: https://wonyoung101.github.io/cafe-order-prices/
- 정적 사이트: `index.html` + `deals.json` (별도 서버 없음)
- 모든 상품 카드에 실제 수집 기록 기반의 상태 배지(평균 대비 ↓/↑, 평균가 유지, 추적 1일차)를 표시하고, 카드를 누르면 **당일 시간대별 · 최근 30일 일별 가격 추이 모달**이 열립니다(구매 링크는 모달 안의 `쿠팡에서 최저가 확인 / 발주하기` 버튼).

> 이 페이지의 링크는 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.

## 운영 방식 (현재)

`deals.json` 은 **별도 스케줄러**가 갱신합니다. 저장소 연결 권한에 `workflows` / `secrets` / `pages` 권한이 없어
워크플로 파일과 시크릿은 GitHub 웹에서 직접 1회 등록해야 합니다.

- 워크플로 원본: `automation/update-deals.yml` → GitHub 웹에서 `.github/workflows/update-deals.yml` 로 복사하면 활성화
- 필요한 Secrets: `COUPANG_ACCESS_KEY`, `COUPANG_SECRET_KEY` (Settings → Secrets and variables → Actions)

## 데이터 갱신

`deals.json` 은 쿠팡 파트너스 오픈 API(`/v2/products/search`)로 수집해 GitHub Actions 가 자동 커밋합니다.

| 항목 | 값 |
| --- | --- |
| 실행 시각 | 매일 07:00 KST (`cron: '0 22 * * *'`) + 수동 실행(`workflow_dispatch`) |
| 수집 카테고리 | 우유·대체유, 원두, 시럽·소스, 파우더, 과일·베이스, 당류·토핑, 크림·연유, 베이킹 재료, 티·탄산수, 컵·뚜껑, 빨대·홀더, 매장소모품 (12개) |
| 호출 한도 | 시간당 최대 9회(상한 10회) — `--batch`/`--only`/`--wave`로 4~8회씩 분할 수집 |
| 요청 간격 | 8초 (`SAFE_INTERVAL_SEC`) |
| 비밀값 | `COUPANG_ACCESS_KEY`, `COUPANG_SECRET_KEY` — GitHub Actions Secrets 에만 저장 |
| 가격 이력 | `data/price_history.json` — 상품별로 실제 관측된 가격만 누적(추정·보간 없음) |

## 노출 필터 (로켓배송 + 단가 확인)

`deals.json` 에는 **`isRocket == true` 이고 `unitPrice` 가 계산된 상품만** 들어갑니다. 그래야 카드마다 비교 가능한 단가가 있고, 사이트 상단의 `🚀 로켓배송 · 단가 확인 품목만` 배지가 실제 내용과 일치합니다.

- 수집·병합(`merge_deals`)과 `--recompute` / `--prune-only` 모든 경로에서 같은 필터(`passes_listing_filter`)를 거칩니다.
- 비로켓 상품과 상품명에 용량·수량이 없어 단가를 낼 수 없는 상품은 `deals.json` 에서 빠지지만, `data/price_history.json` 의 **기존 관측 이력은 지우지 않고 보존**합니다.
- `index.html` 도 데이터를 한 번 더 확인해 조건 밖 품목은 그리지 않고, 숨긴 건수가 있으면 배지에 `조건 밖 N건 숨김`으로 표시합니다.
- 카테고리는 `당류·토핑`, `크림·연유`, `베이킹 재료` 3개가 추가되어 총 12개이며, 수집 키워드는 `TARGETS` 에 있습니다. 새 카테고리는 수집 전까지 빈 탭으로 두고, 빈 상태 문구로 안내합니다.

## 가격 이력 · 평균 대비 할인율

`data/price_history.json` 에 상품(`productId`)별 관측 가격을 쌓고, 그 평균과 비교해 할인율을 계산합니다.

- 관측 1건 = 그 수집 시각에 확인된 해당 상품의 `productPrice`. **보간·추정값은 넣지 않습니다.**
- `deals.json` 각 상품에 `avgPrice`(누적 평균가), `minPrice`(최저 관측가), `maxPrice`(최고 관측가), `priceCount`(관측 횟수), `discountRate`(평균 대비 하락률), `priceHistory`(관측 시각·가격 목록, 최근 120건), `dailyHistory`(최근 30일 일별 집계), `uniqueDays`(기록 일수)를 함께 저장합니다.
- `dailyHistory` 형식: `[날짜(YYYY-MM-DD), 최저, 최고, 평균, 마지막, 관측횟수]` — 오래된 순, 최대 30일.
- `discountRate` = 관측 2회 이상이고 현재가가 누적 평균보다 낮을 때 `round((avgPrice - productPrice) / avgPrice * 100)`, 그 외에는 `null`.
- 사이트는 **모든 상품**에 상태 배지를 붙입니다: `discountRate >= 1` → 빨강 `평균 대비 N%↓`, 평균보다 높음 → 주황 `평균 대비 +N%↑`, 평균과 같은 가격 → 회색 `평균가 유지 · N회 동일`, 관측 1회 → 파랑 `추적 1일차`. (0%·가짜 정가 표시 없음)
- 카드 하단에는 모든 상품에 `평균 X원 · 최저 Y원`을 같은 형식으로 표시합니다.
- 카드를 누르면 요약 타일(현재가·30일 평균·30일 최저·30일 최고) 아래에 **`당일 추이` / `30일 추이` 전환 탭**이 있고, 기본은 오늘 관측이 2회 이상이면 `당일 추이`, 1회뿐이면 `30일 추이`입니다.
  - `당일 추이`: 가장 최근 관측일의 `priceHistory` 관측 시각(`HH:MM`)·가격을 시간순으로 이은 SVG 라인 차트 + 관측 기록 목록(최근 순, 직전 관측 대비 ▲/▼). x축에 실제 관측 시각을 적고, 가로 위치는 관측 순서(보간 없음)입니다. 첫 관측 대비 변동(예: `▲ 2,670원 (+15.4%)`)을 차트 위에 표시합니다. 관측 1회뿐인 날은 가로 기준선+점+안내 문구로 그립니다.
  - `30일 추이`: 일별 평균가 SVG 라인 차트(막대 = 당일 최저~최고) + 일별 최저·평균·마지막 가격 목록. 하루치만 있어도 수평 기준선과 점, “매일 누적되어 최대 30일 추이가 그려집니다” 안내가 표시됩니다.
- 보관량: 원시 관측은 최근 120건(최근 35일치가 더 많으면 그만큼)까지, 일별 집계는 이력 파일에 최근 120일까지 남깁니다. → `deals.json` 에는 최근 30일 일별 집계만 실어 보냅니다.
- 관측 목록은 `deals.json` 에도 남기므로, 자동 갱신 워크플로가 `data/price_history.json` 을 커밋하지 못해도 다음 실행이 이력을 복원해 계속 누적합니다. (워크플로 `git add` 에 `data/price_history.json` 을 더하면 더 깔끔합니다 — 연결 권한상 워크플로 파일은 GitHub 웹에서 수정)
- 초기 이력은 저장소 커밋의 실제 `deals.json` 스냅샷에서 시드했습니다: `python scripts/seed_price_history.py`

### 쿨다운 안전장치

2026-11-14 15:02 KST 쿠팡 오픈 API `products/search` 가 403 경고(24시간 쿨다운, ~2026-11-15 23:58 KST)를 반환했습니다.
그래서 **2026-11-16 00:05 KST (`2026-11-15T15:05:00Z`) 이전에는 API를 호출하지 않습니다.**

1. 워크플로우 `쿨다운 안전장치` 스텝: 시각 이전이면 수집 스텝을 아예 실행하지 않음
2. `scripts/fetch_cafe_deals.py`: 동일 시각 이전이면 네트워크 요청 없이 `exit 0`

## 로컬 확인

```bash
python scripts/fetch_cafe_deals.py --dry-run      # 네트워크 없이 계획·안전장치만 확인
python scripts/fetch_cafe_deals.py --batch 1      # 1~4번째 카테고리만 수집 (1회 4호출)
python scripts/fetch_cafe_deals.py --wave 2 --max-requests 8  # 2차 보강 키워드 8개 수집
python scripts/fetch_cafe_deals.py --only 원두,파우더  # 카테고리 지정 수집
python scripts/fetch_cafe_deals.py --prune-only    # API 없이 기존 데이터 정리(중복·무관 상품 제거)
python scripts/fetch_cafe_deals.py --recompute     # API 없이 가격 이력만으로 평균가·할인율 재계산
python scripts/seed_price_history.py               # 커밋 스냅샷에서 가격 이력 최초 시드(1회성)
python scripts/fetch_cafe_deals.py --assume-used 5   # 로그 밖에서 이미 쓴 호출 수 가산
python -m http.server 8000                        # 사이트 미리보기 (http://localhost:8000)
```

수집 결과는 `productId` 기준으로 기존 `deals.json` 과 병합됩니다(`--replace` 로 전체 교체).
호출 시각은 `scripts/.api_usage.json` 에 기록되어 최근 1시간 9회를 넘으면 자동 중단합니다.

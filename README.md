# 카페발주 단가표

카페 사장님이 발주 품목의 **상품 가격**과 **개당 · 100ml당 · 100g당 단가**를 9개 카테고리로 비교하는 모바일 웹 페이지입니다.

- 공개 주소: https://wonyoung101.github.io/cafe-order-prices/
- 정적 사이트: `index.html` + `deals.json` (별도 서버 없음)

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
| 수집 카테고리 | 우유·대체유, 원두, 시럽·소스, 파우더, 과일·베이스, 티·탄산수, 컵·뚜껑, 빨대·홀더, 매장소모품 (9개) |
| 호출 한도 | 시간당 최대 9회(상한 10회) — `--batch`/`--only`/`--wave`로 4~8회씩 분할 수집 |
| 요청 간격 | 8초 (`SAFE_INTERVAL_SEC`) |
| 비밀값 | `COUPANG_ACCESS_KEY`, `COUPANG_SECRET_KEY` — GitHub Actions Secrets 에만 저장 |

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
python scripts/fetch_cafe_deals.py --assume-used 5   # 로그 밖에서 이미 쓴 호출 수 가산
python -m http.server 8000                        # 사이트 미리보기 (http://localhost:8000)
```

수집 결과는 `productId` 기준으로 기존 `deals.json` 과 병합됩니다(`--replace` 로 전체 교체).
호출 시각은 `scripts/.api_usage.json` 에 기록되어 최근 1시간 9회를 넘으면 자동 중단합니다.

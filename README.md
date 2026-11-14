# 카페발주 단가표

카페 사장님이 발주 품목의 **박스 가격**과 **개당 · 100ml당 단가**를 한 화면에서 비교하는 모바일 웹 페이지입니다.

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
| 수집 품목 | 멸균우유 1000ml, 테이크아웃 아이스컵 16oz, 모닌 시럽 1000ml, 카페 일회용 빨대, 카페 냅킨 |
| 요청 간격 | 7초 (`SAFE_INTERVAL_SEC`) |
| 비밀값 | `COUPANG_ACCESS_KEY`, `COUPANG_SECRET_KEY` — GitHub Actions Secrets 에만 저장 |

### 쿨다운 안전장치

2026-11-14 15:02 KST 쿠팡 오픈 API `products/search` 가 403 경고(24시간 쿨다운, ~2026-11-15 23:58 KST)를 반환했습니다.
그래서 **2026-11-16 00:05 KST (`2026-11-15T15:05:00Z`) 이전에는 API를 호출하지 않습니다.**

1. 워크플로우 `쿨다운 안전장치` 스텝: 시각 이전이면 수집 스텝을 아예 실행하지 않음
2. `scripts/fetch_cafe_deals.py`: 동일 시각 이전이면 네트워크 요청 없이 `exit 0`

## 로컬 확인

```bash
python scripts/fetch_cafe_deals.py --dry-run   # 네트워크 없이 계획·안전장치만 확인
python -m http.server 8000                     # 사이트 미리보기 (http://localhost:8000)
```

#!/usr/bin/env python3
"""카페 발주 단가표 · 쿠팡 파트너스 상품 수집 스크립트

- 환경변수 COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 로 HMAC 서명 요청을 보냅니다.
- 카페 5대 품목을 조회해 `deals.json` 을 갱신합니다.
- 요청 간 최소 6초(SAFE_INTERVAL_SEC) 대기하고, 하루 실행은 1회만 가정합니다.
- 안전장치: UNLOCK_AT(2026-11-15T15:05:00Z = 2026-11-16 00:05 KST) 이전에는
  네트워크 요청을 전혀 보내지 않고 즉시 정상 종료(exit 0)합니다.
  (2026-11-14 15:02 KST 쿠팡 오픈 API 403 경고 → 2026-11-15 23:58 KST 까지 24시간 쿨다운)

사용법:
  python scripts/fetch_cafe_deals.py            # 실제 수집 (쿨다운 중에는 자동 스킵)
  python scripts/fetch_cafe_deals.py --dry-run  # 네트워크 없이 계획/안전장치만 확인
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- 설정
ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "deals.json"

API_HOST = "https://api-gateway.coupang.com"
API_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
TIMEOUT_SEC = 20
SAFE_INTERVAL_SEC = 7        # 요청/요청 사이 최소 대기 (요구: 6초 이상)
LIMIT = 10                   # 품목별 수집 후보 수

# 쿨다운 해제 시각: 2026-11-16 00:05 KST (쿠팡 403 경고는 2026-11-15 23:58 KST 해제)
UNLOCK_AT = datetime(2026, 11, 15, 15, 5, 0, tzinfo=timezone.utc)
KST = timezone(timedelta(hours=9))

# 카페 5대 품목
TARGETS = [
    {"label": "멸균우유",                    "category": "우유·음료",   "query": "멸균우유",                    "default_basis": "100ml"},
    {"label": "테이크아웃 아이스컵",          "category": "컵·뚜껑",     "query": "테이크아웃 아이스컵",          "default_basis": "개당"},
    {"label": "모닌 시럽",                    "category": "시럽·소스",   "query": "모닌 시럽",                    "default_basis": "100ml"},
    {"label": "일회용 빨대",                  "category": "빨대·홀더",   "query": "일회용 빨대",                  "default_basis": "개당"},
    {"label": "카페 냅킨",                    "category": "매장소모품",  "query": "카페 냅킨",                    "default_basis": "개당"},
]

BANNER_NOTICE = "쿠팡 API 연동 대기 중 (2026-11-16 00:00 이후 자동 업데이트 예정)"


# ---------------------------------------------------------------- 안전장치
def cooldown_active(now: datetime | None = None) -> bool:
    """쿨다운(~2026-11-15T15:05Z) 이전이면 True."""
    return (now or datetime.now(timezone.utc)) < UNLOCK_AT


def kst_str(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


# ---------------------------------------------------------------- 서명/요청
def signed_headers(method: str, path: str, query: str, access_key: str, secret_key: str, dt: datetime) -> dict:
    signed_date = dt.strftime("%y%m%dT%H%M%SZ")
    message = signed_date + method.upper() + path + query
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"CEA algorithm=HmacSHA256, access-key={access_key}, "
            f"signed-date={signed_date}, signature={signature}"
        ),
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": "cafe-order-prices/1.0",
    }


def api_get(path: str, params: dict, access_key: str, secret_key: str) -> dict:
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    dt = datetime.now(timezone.utc)
    headers = signed_headers("GET", path, query, access_key, secret_key, dt)
    url = f"{API_HOST}{path}?{query}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


# ---------------------------------------------------------------- 단가 계산
ML_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|mL|ML|미리리터|리터|l|L)\b")
COUNT_RE = re.compile(r"(\d+)\s*(개입|박스|세트|팩|병|봉|포|롤|장|매|개|입)")
MAX_PLAUSIBLE_UNITS = 20000


def _ml_value(token_value: str, token_unit: str) -> float:
    value = float(token_value)
    return value * 1000 if token_unit.lower() in ("l", "리터") else value


def _counts(name: str) -> list[int]:
    """상품명에 적힌 수량 표기(개·개입·박스·세트…)를 모두 뽑는다."""
    vals: list[int] = []
    for m in COUNT_RE.finditer(name or ""):
        v = int(m.group(1))
        if 0 < v <= 100000:
            vals.append(v)
    return vals


def _total_units(vals: list[int]) -> tuple[int | None, str | None]:
    """박스 수 × 내부 수량처럼 여러 수량이 겹치면 곱해서 총 개수를 구한다."""
    if not vals:
        return None, None
    total = 1
    for v in vals:
        total *= v
    if total > MAX_PLAUSIBLE_UNITS:      # 박스×내부수량 곱셈이 비현실적이면 가장 큰 표기를 채택
        vals = [max(vals)]
        total = vals[0]
    factors = [v for v in vals if v != 1] or [1]
    if len(factors) == 1:
        note = f"{factors[0]:,}개입"
    else:
        note = "×".join(f"{v:,}개" for v in factors)
    return total, note


def calc_unit_price(name: str, price: float, default_basis: str):
    """상품명에서 용량/수량을 읽어 (단가, 기준, 산출근거)를 돌려준다. 실패하면 (None, 기준, None)."""
    if not price or price <= 0:
        return None, default_basis, None

    units, pack = _total_units(_counts(name))

    if default_basis == "100ml":
        ml_match = ML_RE.search(name or "")
        if ml_match:
            total_ml = _ml_value(ml_match.group(1), ml_match.group(2)) * (units or 1)
            if total_ml > 0:
                basis = f"100ml×{units}" if units else "100ml"
                return price / (total_ml / 100.0), "100ml당", pack or basis
        return None, "100ml당", None

    if units and units > 0:
        return price / units, "개당", pack
    return None, "개당", None


# ---------------------------------------------------------------- 수집
def collect(access_key: str, secret_key: str, sub_id: str) -> list[dict]:
    deals: list[dict] = []
    seen: set[str] = set()
    requests_made = 0

    for target in TARGETS:
        if requests_made:
            time.sleep(SAFE_INTERVAL_SEC)
        params = {"keyword": target["query"], "limit": LIMIT}
        if sub_id:
            params["subId"] = sub_id
        print(f"[fetch] {target['category']} · {target['query']}")
        payload = api_get(API_PATH, params, access_key, secret_key)
        requests_made += 1

        data_field = (payload or {}).get("data")
        if isinstance(data_field, dict):
            items = data_field.get("productData") or []
        elif isinstance(data_field, list):
            items = data_field
        else:
            items = []
        if (payload or {}).get("rCode") not in ("0", 0):
            print(f"  - API 오류 (rCode={payload.get('rCode')}, rMessage={payload.get('rMessage')})")
        if not items:
            print(f"  - 결과 없음 (rCode={payload.get('rCode')}, rMessage={payload.get('rMessage')})")
        for item in items:
            pid = str(item.get("productId") or item.get("vendorItemId") or item.get("productUrl") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            price = item.get("productPrice") or 0
            unit_price, basis, pack = calc_unit_price(item.get("productName", ""), price, target["default_basis"])
            deals.append({
                "productId": pid,
                "productName": item.get("productName", ""),
                "productPrice": int(price) if price else None,
                "productImage": item.get("productImage", ""),
                "productUrl": item.get("productUrl", ""),
                "isRocket": bool(item.get("isRocket")),
                "isFreeShipping": bool(item.get("isFreeShipping")),
                "category": target["category"],
                "query": target["query"],
                "unitPrice": round(unit_price) if unit_price else None,
                "unitBasis": basis,
                "packNote": pack,
            })
    return deals


# ---------------------------------------------------------------- 출력
def write_json(payload: dict) -> None:
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT_PATH)


def pending_payload(now: datetime, reason: str) -> dict:
    try:
        previous = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        previous = {}
    return {
        "status": "pending_api_sync",
        "updatedAt": kst_str(now),
        "unlocksAt": "2026-11-16 00:00 KST",
        "notice": BANNER_NOTICE,
        "note": reason,
        "watchlist": previous.get("watchlist") or [
            {"label": t["label"], "category": t["category"], "query": t["query"],
             "unitBasis": "100ml" if t["default_basis"] == "100ml" else "개당"}
            for t in TARGETS
        ],
        "deals": previous.get("deals") or [],
    }


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    now = datetime.now(timezone.utc)

    print(f"[info] now={kst_str(now)}  unlock_at={UNLOCK_AT.astimezone(KST).strftime('%Y-%m-%d %H:%M KST')}")

    if cooldown_active(now):
        print("[skip] 쿠팡 API 쿨다운 기간입니다. 네트워크 요청 없이 종료합니다.")
        if dry_run:
            print("[dry-run] 쓰기 없음.")
        return 0

    access_key = os.environ.get("COUPANG_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("COUPANG_SECRET_KEY", "").strip()
    sub_id = os.environ.get("COUPANG_SUB_ID", "").strip()

    if dry_run:
        print("[dry-run] 수집 계획:")
        for i, t in enumerate(TARGETS, 1):
            print(f"  {i}. [{t['category']}] {t['query']} (기준: {t['default_basis']}, 최대 {LIMIT}개)")
        print(f"[dry-run] 요청 {len(TARGETS)}회 · 간격 {SAFE_INTERVAL_SEC}초 · 키 설정됨={bool(access_key and secret_key)}")
        print("[dry-run] 네트워크 요청/파일 쓰기 없음.")
        return 0

    if not (access_key and secret_key):
        print("[error] COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 가 없습니다.", file=sys.stderr)
        return 2

    try:
        deals = collect(access_key, secret_key, sub_id)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:400]
        except Exception:
            pass
        print(f"[error] HTTP {exc.code} {detail}", file=sys.stderr)
        if exc.code in (403, 429):
            print("[abort] 호출 제한 응답입니다. deals.json 을 건드리지 않고 중단합니다.", file=sys.stderr)
        return 1
    except Exception as exc:  # 네트워크/파싱 오류 — 기존 데이터 유지
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not deals:
        print("[warn] 수집된 상품이 없습니다. 기존 deals.json 을 유지합니다.")
        return 0

    try:
        previous = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        previous = {}

    write_json({
        "status": "ok",
        "updatedAt": kst_str(datetime.now(timezone.utc)),
        "notice": "",
        "watchlist": previous.get("watchlist", []),
        "count": len(deals),
        "deals": deals,
    })
    print(f"[done] {len(deals)}개 상품으로 deals.json 갱신")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

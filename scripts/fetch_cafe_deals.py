#!/usr/bin/env python3
"""카페 발주 단가표 · 쿠팡 파트너스 상품 수집 스크립트

- 환경변수 COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 로 HMAC 서명 요청을 보냅니다.
- 카페 9개 카테고리(우유·대체유 / 원두 / 시럽·소스 / 파우더 / 과일·베이스 / 티·탄산수 /
  컵·뚜껑 / 빨대·홀더 / 매장소모품)를 조회해 `deals.json` 을 갱신합니다.
- 요청 간 최소 7초(SAFE_INTERVAL_SEC) 대기하고, 시간당 호출 한도(HOURLY_CAP)를
  로컬 사용기록(scripts/.api_usage.json)으로 지켜 스스로를 제한합니다.
- 결과는 productId 기준으로 기존 `deals.json` 과 병합(중복 제거)합니다.
- 안전장치: UNLOCK_AT(2026-11-15T15:05:00Z = 2026-11-16 00:05 KST) 이전에는
  네트워크 요청을 전혀 보내지 않고 즉시 정상 종료(exit 0)합니다.

사용법:
  python scripts/fetch_cafe_deals.py                     # 9개 카테고리 전체 수집(병합)
  python scripts/fetch_cafe_deals.py --dry-run           # 네트워크 없이 계획만 확인
  python scripts/fetch_cafe_deals.py --batch 1           # 1번째 묶음(기본 4개 카테고리)만 수집
  python scripts/fetch_cafe_deals.py --batch 2 --size 4  # 2번째 묶음
  python scripts/fetch_cafe_deals.py --wave 2            # 2차 보강 키워드만 수집
  python scripts/fetch_cafe_deals.py --only 원두,파우더    # 카테고리 지정 수집
  python scripts/fetch_cafe_deals.py --max-requests 4     # 이번 실행 최대 호출 수
  python scripts/fetch_cafe_deals.py --assume-used 5      # 로그 밖에서 이미 쓴 호출 수 가산
  python scripts/fetch_cafe_deals.py --replace            # 병합 대신 전체 교체
  python scripts/fetch_cafe_deals.py --prune-only         # API 0회 · 무관 상품 정리 + 평균가/할인율 재계산
  python scripts/fetch_cafe_deals.py --recompute          # API 0회 · 가격 이력만으로 평균가/할인율 재계산

가격 이력:
  수집한 상품 가격을 `data/price_history.json` 에 상품별로 누적해 두고, 그 평균(avgPrice)과
  평균 대비 할인율(discountRate)을 `deals.json` 에 함께 저장합니다. 관측되지 않은 가격은
  절대 만들지 않습니다(보간·추정 없음). 관측이 2회 미만이면 할인율은 null 입니다.

  보관량: 원시 관측은 최근 120건(그리고 최근 35일치)까지, 날짜별 집계(dailyHistory)는 최근 120일까지
  남깁니다. `deals.json` 각 상품에는 최근 30일 일별 집계(`dailyHistory`)·최고가(maxPrice)·
  기록 일수(uniqueDays)를 함께 실어 보내 사이트가 30일 추이 모달을 그릴 수 있게 합니다.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- 설정
ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "deals.json"
HISTORY_PATH = ROOT / "data" / "price_history.json"
HISTORY_INLINE_MAX = 120     # deals.json 상품 안에 함께 남기는 관측 개수 상한
HISTORY_RAW_MAX = 120        # data/price_history.json 에 남기는 원시 관측 개수 상한(최근 120건)
HISTORY_DAILY_KEEP_DAYS = 120  # 이력 파일에 보관하는 일별 집계 일수 (원시 관측이 줄어도 일별 집계는 유지)
DAILY_WINDOW_DAYS = 30       # 사이트 모달에 노출하는 일별 추이 일수(최근 30일)
USAGE_PATH = Path(__file__).resolve().parent / ".api_usage.json"

API_HOST = "https://api-gateway.coupang.com"
API_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
TIMEOUT_SEC = 20
SAFE_INTERVAL_SEC = 8        # 요청/요청 사이 최소 대기 (요구: 8초 이상)
LIMIT = 10                   # 품목별 수집 후보 수
HOURLY_CAP = 9               # 최근 1시간 내 최대 호출 수 (상한 10회, 여유 1회)
DEFAULT_BATCH_SIZE = 4       # --batch 묶음 크기 (4회 ≤ 시간당 9회)
DEFAULT_MAX_REQUESTS = 4     # 1회 실행 기본 상한

# 쿨다운 해제 시각: 2026-11-16 00:05 KST (쿠팡 403 경고는 2026-11-15 23:58 KST 해제)
UNLOCK_AT = datetime(2026, 11, 15, 15, 5, 0, tzinfo=timezone.utc)
KST = timezone(timedelta(hours=9))

# 사이트 카테고리 순서 (index.html 탭과 동일하게 유지)
CATEGORIES = [
    "우유·대체유",
    "원두",
    "시럽·소스",
    "파우더",
    "과일·베이스",
    "티·탄산수",
    "컵·뚜껑",
    "빨대·홀더",
    "매장소모품",
]

# 예전 카테고리명 → 새 카테고리명 (deals.json 마이그레이션)
CATEGORY_ALIASES = {"우유·음료": "우유·대체유"}

# 카페 9개 카테고리 수집 키워드
#   bases: 상품명에서 확인할 단가 기준 후보(앞에서부터 우선). 100ml → 100g → 개당 순 자동 선택.
TARGETS = [
    {"label": "멸균우유",           "category": "우유·대체유",  "query": "멸균우유",           "bases": ["100ml"]},
    {"label": "에스프레소 원두",      "category": "원두",        "query": "에스프레소 원두 1kg",  "bases": ["100g"]},
    {"label": "모닌 시럽",           "category": "시럽·소스",    "query": "모닌 시럽",           "bases": ["100ml"]},
    {"label": "카페 파우더",         "category": "파우더",       "query": "카페 파우더 1kg",      "bases": ["100g"]},
    {"label": "에이드 과일베이스",    "category": "과일·베이스",  "query": "에이드 과일베이스",     "bases": ["100g", "100ml"]},
    {"label": "업소용 탄산수",        "category": "티·탄산수",    "query": "업소용 탄산수",        "bases": ["100ml", "개당"]},
    {"label": "테이크아웃 아이스컵",  "category": "컵·뚜껑",      "query": "테이크아웃 아이스컵",   "bases": ["개당"]},
    {"label": "일회용 빨대",         "category": "빨대·홀더",    "query": "일회용 빨대",         "bases": ["개당"]},
    {"label": "카페 냅킨",           "category": "매장소모품",   "query": "카페 냅킨",           "bases": ["개당"]},

    # ---- 2차 보강 키워드 (wave=2) — 기존 1차 결과와 다른 상품군을 추가로 확보 ----
    {"label": "오트밀크 아몬드브리즈", "category": "우유·대체유", "query": "오트밀크",           "bases": ["100ml"], "wave": 2},
    {"label": "디카페인 원두",        "category": "원두",        "query": "디카페인 원두 1kg",   "bases": ["100g"], "wave": 2},
    {"label": "초코 파우더",          "category": "파우더",       "query": "초코 파우더 1kg",     "bases": ["100g"], "wave": 2},
    {"label": "자몽 에이드 베이스",     "category": "과일·베이스",  "query": "자몽 에이드 베이스",   "bases": ["100ml", "100g"], "wave": 2},
    {"label": "얼그레이 티백 업소용",   "category": "티·탄산수",    "query": "얼그레이 티백 업소용",  "bases": ["개당"], "wave": 2},
    {"label": "16oz 아이스컵 1000개",  "category": "컵·뚜껑",      "query": "16oz 아이스컵 1000개", "bases": ["개당"], "wave": 2},
    {"label": "테이크아웃 컵홀더",      "category": "빨대·홀더",    "query": "테이크아웃 컵홀더",    "bases": ["개당"], "wave": 2},
    {"label": "업소용 물티슈",         "category": "매장소모품",   "query": "업소용 물티슈",       "bases": ["개당"], "wave": 2},
]

BANNER_NOTICE = "쿠팡 API 연동 대기 중 (2026-11-16 00:00 이후 자동 업데이트 예정)"

# 검색 결과에 섞여 들어오는 카테고리 무관 상품(생수·감미료·개인용 텀블러 홀더 등)은 제외한다.
EXCLUDE_KEYWORDS = [
    "탐사 샘물",
    "알룰로스",
    "결로방지 컵홀더",
]


def is_excluded(name: str) -> bool:
    text = (name or "").replace(" ", "").lower()
    return any(k.replace(" ", "").lower() in text for k in EXCLUDE_KEYWORDS)


# ---------------------------------------------------------------- 안전장치
def cooldown_active(now: datetime | None = None) -> bool:
    """쿨다운(~2026-11-15T15:05Z) 이전이면 True."""
    return (now or datetime.now(timezone.utc)) < UNLOCK_AT


def kst_str(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def load_usage() -> list[datetime]:
    try:
        raw = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        return [datetime.fromisoformat(t) for t in raw.get("requests", [])]
    except Exception:
        return []


def save_usage(stamps: list[datetime]) -> None:
    try:
        USAGE_PATH.write_text(
            json.dumps({"requests": [s.astimezone(timezone.utc).isoformat() for s in stamps]}, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[warn] 사용기록 저장 실패: {exc}", file=sys.stderr)


def hour_window(stamps: list[datetime], now: datetime) -> list[datetime]:
    return [s for s in stamps if now - s < timedelta(hours=1) and s <= now]


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
COUNT_RE = re.compile(r"(\d+)\s*(개입|박스|세트|팩|병|봉|포|롤|장|매|캔|개|입)")
# "1.25L x 12", "1kg (250g x 4)" 처럼 앞에 용량/수량 표기가 붙은 곱셈만 수량으로 인정한다.
# ("5mm x 24cm" 같은 규격 표기를 수량으로 오인하지 않도록 단위를 요구)
X_COUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|킬로그램|g|그램|ml|mL|ML|Ml|미리리터|l|L|리터|개|입)?\s*[x×]\s*(\d+)\s*(개입|개|입|병|캔|팩|봉|포|세트)?"
)
VOL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|킬로그램|g|그램|ml|mL|ML|Ml|미리리터|l|L|리터)")
# "4팩 2000개입" 처럼 팩 수 뒤에 세트 전체 수량이 바로 적힌 경우: 4×2000 이 아니라 2000개가 총량이다.
# (예: 개별포장 자바라빨대 "4팩 2000개입, 1세트, 2000개" → 실제 2,000개)
PACK_TOTAL_RE = re.compile(r"(\d+)\s*팩\s*(\d+)\s*(개입|개)")
MAX_PLAUSIBLE_UNITS = 20000


def _norm(name: str) -> str:
    return (name or "").replace(",", "")


def _volume_tokens(name: str) -> list[tuple[float, str]]:
    """용량 표기를 (기준단위 값, 'g'|'ml') 로 뽑는다. 1kg → 1000g, 1L → 1000ml."""
    out: list[tuple[float, str]] = []
    for m in VOL_RE.finditer(_norm(name)):
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit in ("kg", "킬로그램"):
            out.append((value * 1000, "g"))
        elif unit in ("g", "그램"):
            out.append((value, "g"))
        elif unit in ("l", "리터"):
            out.append((value * 1000, "ml"))
        else:
            out.append((value, "ml"))
    return out


def _counts(name: str) -> list[int]:
    """상품명에 적힌 수량 표기(개·개입·박스·세트…, 용량×개수 곱셈)를 모두 뽑는다."""
    text = _norm(name)
    vals: list[int] = []
    used_spans: list[tuple[int, int]] = []
    for m in X_COUNT_RE.finditer(text):
        if m.group(2) or m.group(4):        # 앞에 용량/수량 표기가 있는 곱셈만 인정
            v = int(m.group(3))
            if 0 < v <= 100000:
                vals.append(v)
                used_spans.append((m.start(), m.end()))
    for m in PACK_TOTAL_RE.finditer(text):
        used_spans.append((m.start(1), m.start(2)))   # 'N팩' 은 포장 단위 표기 — 총량은 뒤 숫자
    for m in COUNT_RE.finditer(text):
        if any(s <= m.start() < e or s < m.end() <= e for s, e in used_spans):
            continue                         # 곱셈·팩 표기로 이미 처리한 수량
        v = int(m.group(1))
        if 0 < v <= 100000:
            vals.append(v)
    return vals


def _dedupe_repeats(vals: list[int]) -> list[int]:
    """`6입 1박스 … 6개` 처럼 같은 수량이 두 번 적힌 경우 한 번만 센다(중복 표기 보정)."""
    seen: set[int] = set()
    out: list[int] = []
    for v in vals:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _total_units(vals: list[int]) -> tuple[int | None, str | None]:
    """박스 수 × 내부 수량처럼 여러 수량이 겹치면 곱해서 총 개수를 구한다."""
    if not vals:
        return None, None
    vals = _dedupe_repeats(vals)
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


def _volume_total(name: str, kind: str) -> tuple[float | None, str | None]:
    """상품명에서 (총 용량, 표기 요약)을 구한다. kind 는 'g' 또는 'ml'."""
    vals = [v for v, k in _volume_tokens(name) if k == kind]
    if not vals:
        return None, None
    vmax, vmin = max(vals), min(vals)
    units, _ = _total_units(_counts(name))
    unit_text = "g" if kind == "g" else "ml"

    # "1kg (250g x 4)" 처럼 총량이 이미 적힌 경우: 큰 값 = 작은 값 × 개수 → 곱하지 않는다.
    if units and vmin > 0:
        ratio = vmax / vmin
        if ratio >= 2 and abs(ratio - round(ratio)) < 1e-9 and int(round(ratio)) in _counts(name):
            return vmax, f"{vmax:,.0f}{unit_text}"

    total = vmax * (units or 1)
    if units and units > 1:
        note = f"{vmax:,.0f}{unit_text}×{units:,}"
    else:
        note = f"{vmax:,.0f}{unit_text}"
    return total, note


BASIS_LABEL = {"100ml": "100ml당", "100g": "100g당", "개당": "개당"}


def calc_unit_price(name: str, price: float, bases: list[str]):
    """상품명에서 용량/수량을 읽어 (단가, 기준, 산출근거)를 돌려준다. 실패하면 (None, 첫 기준, None)."""
    if not price or price <= 0:
        return None, BASIS_LABEL.get(bases[0], bases[0]), None

    units, pack = _total_units(_counts(name))
    for basis in bases:
        if basis == "개당":
            if units and units > 0:
                return price / units, "개당", pack
            continue
        kind = "g" if basis == "100g" else "ml"
        total, note = _volume_total(name, kind)
        if total and total > 0:
            return price / (total / 100.0), BASIS_LABEL[basis], note
    return None, BASIS_LABEL.get(bases[0], bases[0]), None


# ---------------------------------------------------------------- 수집
def select_targets(argv: list[str]) -> list[dict]:
    targets = list(TARGETS)

    wave = _opt(argv, "--wave")
    if wave:
        targets = [t for t in targets if str(t.get("wave", 1)) == str(wave)]
        if not targets:
            print(f"[error] --wave={wave} 에 해당하는 키워드가 없습니다.", file=sys.stderr)

    only = _opt(argv, "--only")
    if only:
        wanted = {c.strip() for c in only.split(",") if c.strip()}
        aliased = {CATEGORY_ALIASES.get(c, c) for c in wanted}
        targets = [t for t in targets if t["category"] in aliased or t["label"] in wanted]
        if not targets:
            print(f"[error] --only={only} 에 해당하는 카테고리가 없습니다.", file=sys.stderr)

    batch = _opt(argv, "--batch")
    if batch:
        size = int(_opt(argv, "--size") or DEFAULT_BATCH_SIZE)
        idx = int(batch)
        start = (idx - 1) * size
        targets = targets[start:start + size]
        if not targets:
            print(f"[error] --batch {idx} (묶음 크기 {size}) 에 해당하는 품목이 없습니다.", file=sys.stderr)
    return targets


# ---------------------------------------------------------------- 가격 이력 / 평균가
def load_history() -> dict:
    try:
        hist = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        hist.setdefault("products", {})
        return hist
    except Exception:
        return {"updatedAt": None, "source": "collection", "products": {}}


def save_history(hist: dict) -> None:
    hist["updatedAt"] = kst_str(datetime.now(timezone.utc))
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(HISTORY_PATH)


def add_observation(hist: dict, deal: dict, at: str, source: str) -> bool:
    """상품 1건의 관측 가격을 이력에 추가한다. 같은 수집 시각이면 중복 기록하지 않는다."""
    pid = str(deal.get("productId") or "")
    price = deal.get("productPrice")
    if not pid or not price or not at:
        return False
    entry = hist["products"].setdefault(pid, {"productName": "", "category": "", "observations": []})
    entry["productName"] = deal.get("productName") or entry.get("productName") or ""
    entry["category"] = deal.get("category") or entry.get("category") or ""
    obs = entry.setdefault("observations", [])
    for o in obs:
        if o.get("at") == at:
            if o.get("price") == int(price):
                return False            # 같은 시각·같은 가격 → 이미 기록됨
            o["price"] = int(price)     # 같은 시각 재실행에서 가격이 바뀐 경우만 갱신
            o["source"] = source
            return True
    obs.append({"at": at, "price": int(price), "source": source})
    return True


def reconcile_from_previous(hist: dict, prev_payload: dict) -> int:
    """이전 `deals.json` 에서 관측 가격을 이력으로 복원한다.

    각 상품이 가진 `priceHistory`(관측 목록)를 활용하므로, 워크플로가
    `data/price_history.json` 을 커밋하지 못해도 이력이 계속 누적됩니다
    (추정값 없음, 실제 기록만).
    """
    at_default = (prev_payload or {}).get("updatedAt") or ""
    deals = (prev_payload or {}).get("deals") or []
    if not deals:
        return 0
    restored = 0
    for d in deals:
        pairs = d.get("priceHistory")
        if isinstance(pairs, list) and pairs:
            for pair in pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2 or not pair[1]:
                    continue
                probe = dict(d, productPrice=pair[1])
                if add_observation(hist, probe, str(pair[0] or ""), "이전 deals.json 이력"):
                    restored += 1
        elif at_default and d.get("productPrice"):
            if add_observation(hist, d, at_default, "이전 deals.json 복원"):
                restored += 1
    return restored


def obs_date(at: str) -> str:
    """관측 시각 문자열('YYYY-MM-DD HH:MM KST')에서 날짜(YYYY-MM-DD)만 뽑는다."""
    s = str(at or "")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def build_daily_history(obs: list[dict], days: int = DAILY_WINDOW_DAYS) -> list[list]:
    """관측 목록을 날짜별로 묶어 [날짜, 최저, 최고, 평균, 마지막, 관측횟수] 를 오래된 순으로 돌려준다.

    관측된 값만 사용한다(빠진 날짜를 보간하지 않음). 최근 `days` 일만 남긴다.
    """
    buckets: dict[str, list[int]] = {}
    for o in obs:
        d = obs_date(o.get("at"))
        price = o.get("price")
        if not d or not price:
            continue
        buckets.setdefault(d, []).append(int(price))
    items: list[list] = []
    for d in sorted(buckets)[-days:]:
        prices = buckets[d]
        items.append([d, min(prices), max(prices), round(sum(prices) / len(prices)), prices[-1], len(prices)])
    return items


def trim_observations(obs: list[dict]) -> list[dict]:
    """원시 관측을 최근 120건까지 줄이되, 최근 35일치 관측은 남긴다.

    하루에 여러 번 수집해 120건이 며칠치밖에 안 되는 경우에도 최소 한 달치
    일별 추이가 유지되도록 하는 보호장치다. (합성·보간 없이 실제 관측만 유지)
    """
    if len(obs) <= HISTORY_RAW_MAX:
        return obs
    dates = [obs_date(o.get("at")) for o in obs]
    valid = [d for d in dates if d]
    if not valid:
        return obs[-HISTORY_RAW_MAX:]
    latest = date.fromisoformat(max(valid))
    cutoff = (latest - timedelta(days=34)).isoformat()
    recent = [o for o in obs if obs_date(o.get("at")) >= cutoff]
    keep = obs[-HISTORY_RAW_MAX:]
    return recent if len(recent) > len(keep) else keep


def finalize_history(hist: dict) -> None:
    """관측을 시각순으로 정렬하고 상품별 통계(평균·최저·최고·관측 횟수·일별 집계)를 계산한다."""
    for entry in hist.get("products", {}).values():
        obs = sorted(entry.get("observations") or [], key=lambda o: o.get("at") or "")
        obs = trim_observations(obs)
        entry["observations"] = obs
        prices = [o["price"] for o in obs if o.get("price")]
        entry["priceCount"] = len(prices)
        entry["avgPrice"] = round(sum(prices) / len(prices)) if prices else None
        entry["minPrice"] = min(prices) if prices else None
        entry["maxPrice"] = max(prices) if prices else None
        entry["lastPrice"] = prices[-1] if prices else None
        entry["lastAt"] = obs[-1]["at"] if obs else None
        daily = build_daily_history(obs, HISTORY_DAILY_KEEP_DAYS)
        entry["dailyHistory"] = daily
        entry["uniqueDays"] = len(daily)


def apply_price_stats(deals: list[dict], hist: dict) -> int:
    """각 상품에 avgPrice·minPrice·priceCount·discountRate 를 넣고, 할인 배지 대상 수를 돌려준다.

    discountRate 는 관측 2회 이상이고 현재가가 누적 평균가보다 낮을 때만
    round((avgPrice - productPrice) / avgPrice * 100), 그 외에는 null.
    """
    products = hist.get("products") or {}
    discounted = 0
    for d in deals:
        entry = products.get(str(d.get("productId") or "")) or {}
        avg = entry.get("avgPrice")
        count = entry.get("priceCount") or 0
        price = d.get("productPrice")
        d["avgPrice"] = avg
        d["minPrice"] = entry.get("minPrice")
        d["maxPrice"] = entry.get("maxPrice")
        d["priceCount"] = count
        if avg and count >= 2 and price and price < avg:
            d["discountRate"] = round((avg - price) / avg * 100)
            if d["discountRate"] >= 1:
                discounted += 1
        else:
            d["discountRate"] = None
        # 날짜별 집계(최근 30일) — 사이트의 30일 가격 추이 모달이 그대로 사용한다.
        daily = entry.get("dailyHistory") or []
        d["uniqueDays"] = len(daily)
        daily30 = daily[-DAILY_WINDOW_DAYS:]
        if daily30:
            d["dailyHistory"] = daily30
        else:
            d.pop("dailyHistory", None)
        # 관측 목록을 상품 안에도 함께 남긴다 — 워크플로가 price_history.json 을
        # 커밋하지 못해도 이력이 deals.json 과 함께 누적되게 하는 안전장치.
        obs = entry.get("observations") or []
        if obs:
            d["priceHistory"] = [[o.get("at"), o["price"]]
                                 for o in obs[-HISTORY_INLINE_MAX:] if o.get("price")]
        else:
            d.pop("priceHistory", None)
    return discounted


def refresh_from_history(prev_payload: dict, prune: bool) -> tuple[dict, int, int]:
    """API 호출 없이 이력만으로 deals.json 의 평균가·할인율을 다시 계산한다."""
    hist = load_history()
    restored = reconcile_from_previous(hist, prev_payload)
    finalize_history(hist)
    deals = (prev_payload or {}).get("deals") or []
    if prune:
        deals = merge_deals(deals, [])
    deals = sort_deals(deals) if prune else deals
    discounted = apply_price_stats(deals, hist)
    save_history(hist)
    return deals, discounted, restored


def _opt(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def rotate_targets(targets: list[dict], max_requests: int, now: datetime | None = None) -> list[dict]:
    """하루 호출 상한(--max-requests)이 키워드 수보다 적을 때, 매번 같은 앞쪽 키워드만
    갱신되지 않도록 날짜에 따라 시작 위치를 돌린다. 며칠 안에 모든 키워드가 한 번씩 갱신된다."""
    if len(targets) <= max_requests:
        return targets
    start = ((now or datetime.now(timezone.utc)).toordinal() * max_requests) % len(targets)
    rotated = targets[start:] + targets[:start]
    return rotated[:max_requests]


def collect(access_key: str, secret_key: str, sub_id: str, targets: list[dict],
            max_requests: int, assume_used: int = 0) -> tuple[list[dict], list[str]]:
    deals: list[dict] = []
    seen: set[str] = set()
    requests_made = 0
    failed_categories: list[str] = []
    usage = load_usage()

    for target in targets:
        if requests_made >= max_requests:
            print(f"[stop] 이번 실행 상한({max_requests}회)에 도달해 남은 품목을 건너뜁니다.")
            failed_categories.extend(t["category"] for t in targets if t["category"] not in
                                     {d["category"] for d in deals} and t is not target)
            break

        now = datetime.now(timezone.utc)
        recent = hour_window(usage, now)
        if len(recent) + assume_used >= HOURLY_CAP:
            print(f"[stop] 최근 1시간 호출 {len(recent) + assume_used}회 — 시간당 상한({HOURLY_CAP}회) 도달. 중단합니다.")
            break
        if requests_made:
            time.sleep(SAFE_INTERVAL_SEC)

        params = {"keyword": target["query"], "limit": LIMIT}
        if sub_id:
            params["subId"] = sub_id
        print(f"[fetch] {target['category']} · {target['query']}")
        payload = api_get(API_PATH, params, access_key, secret_key)
        requests_made += 1
        usage.append(datetime.now(timezone.utc))
        save_usage(usage)

        data_field = (payload or {}).get("data")
        if isinstance(data_field, dict):
            items = data_field.get("productData") or []
        elif isinstance(data_field, list):
            items = data_field
        else:
            items = []
        if (payload or {}).get("rCode") not in ("0", 0):
            print(f"  - API 오류 (rCode={payload.get('rCode')}, rMessage={payload.get('rMessage')})")
            failed_categories.append(target["category"])
        if not items:
            print(f"  - 결과 없음 (rCode={payload.get('rCode')}, rMessage={payload.get('rMessage')})")
        for item in items:
            pid = str(item.get("productId") or item.get("vendorItemId") or item.get("productUrl") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            price = item.get("productPrice") or 0
            unit_price, basis, pack = calc_unit_price(item.get("productName", ""), price, target["bases"])
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
    print(f"[info] 요청 {requests_made}회 · 신규/갱신 후보 {len(deals)}건")
    return deals, failed_categories


# ---------------------------------------------------------------- 병합/출력
def load_previous() -> dict:
    try:
        return json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def merge_deals(previous: list[dict], fresh: list[dict]) -> list[dict]:
    """productId 기준 중복 제거 병합. 새 수집 결과가 같은 상품이면 최신 값으로 갱신한다."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for d in previous:
        if is_excluded(d.get("productName", "")):
            continue
        while d.get("category") in CATEGORY_ALIASES:
            d["category"] = CATEGORY_ALIASES[d["category"]]
        pid = str(d.get("productId") or "")
        if not pid:
            continue
        if pid not in merged:
            order.append(pid)
        merged[pid] = d
    for d in fresh:
        pid = str(d.get("productId") or "")
        if not pid or is_excluded(d.get("productName", "")):
            continue
        if pid not in merged:
            order.append(pid)
        merged[pid] = d
    return [merged[pid] for pid in order]


def sort_deals(deals: list[dict]) -> list[dict]:
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(
        deals,
        key=lambda d: (rank.get(d.get("category", ""), 99),
                       d.get("unitPrice") if d.get("unitPrice") else 10 ** 12),
    )


def write_json(payload: dict) -> None:
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT_PATH)


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    replace = "--replace" in argv
    now = datetime.now(timezone.utc)
    targets = select_targets(argv)
    try:
        max_requests = int(_opt(argv, "--max-requests") or DEFAULT_MAX_REQUESTS)
    except ValueError:
        max_requests = DEFAULT_MAX_REQUESTS
    try:
        assume_used = int(_opt(argv, "--assume-used") or 0)
    except ValueError:
        assume_used = 0

    # --batch/--only/--wave 로 범위를 직접 지정하지 않았고 상한이 키워드 수보다 적으면 순환 갱신
    explicit_scope = any(a in ("--batch", "--only", "--wave")
                         or a.startswith(("--batch=", "--only=", "--wave=")) for a in argv)
    if not explicit_scope:
        rotated = rotate_targets(targets, max_requests, now)
        if rotated is not targets:
            print("[info] 순환 갱신: " + " · ".join(f"{t['category']}/{t['query']}" for t in rotated))
        targets = rotated

    print(f"[info] now={kst_str(now)}  unlock_at={UNLOCK_AT.astimezone(KST).strftime('%Y-%m-%d %H:%M KST')}")

    access_key = os.environ.get("COUPANG_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("COUPANG_SECRET_KEY", "").strip()
    sub_id = os.environ.get("COUPANG_SUB_ID", "").strip()

    recent = hour_window(load_usage(), now)
    print(f"[info] 최근 1시간 기록된 호출 {len(recent)}회 (상한 {HOURLY_CAP}회) · 이번 실행 상한 {max_requests}회 · 가산 {assume_used}회")

    if dry_run:
        print("[dry-run] 수집 계획:")
        for i, t in enumerate(targets, 1):
            print(f"  {i}. [{t['category']}] {t['query']} (기준: {' → '.join(t['bases'])}, 최대 {LIMIT}개)")
        print(f"[dry-run] 요청 {len(targets)}회(이번 실행 상한 {max_requests}) · 간격 {SAFE_INTERVAL_SEC}초 · "
              f"병합={'아니오(교체)' if replace else '예'} · 키 설정됨={bool(access_key and secret_key)}")
        print("[dry-run] 네트워크 요청/파일 쓰기 없음.")
        return 0

    if "--prune-only" in argv or "--recompute" in argv:
        previous = load_previous()
        prev_deals = previous.get("deals") or []
        prune = "--prune-only" in argv
        deals, discounted, restored = refresh_from_history(previous, prune)
        write_json({
            "status": "ok",
            "updatedAt": previous.get("updatedAt") or kst_str(datetime.now(timezone.utc)),
            "statsAt": kst_str(datetime.now(timezone.utc)),
            "notice": "",
            "watchlist": [],
            "categories": CATEGORIES,
            "count": len(deals),
            "priceStats": {"products": len(load_history().get("products") or {}), "discounts": discounted},
            "deals": deals,
        })
        label = "prune" if prune else "recompute"
        print(f"[{label}] 네트워크 요청 없이 갱신: {len(prev_deals)} → {len(deals)}건 · "
              f"할인 배지 {discounted}건 · 이전 deals.json 복원 {restored}건")
        if prune:
            print(f"       (제외 키워드 {len(EXCLUDE_KEYWORDS)}종 적용)")
        return 0

    if cooldown_active(now):
        print("[skip] 쿠팡 API 쿨다운 기간입니다. 네트워크 요청 없이 종료합니다.")
        return 0

    if not (access_key and secret_key):
        print("[error] COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 가 없습니다.", file=sys.stderr)
        return 2

    try:
        deals, failed = collect(access_key, secret_key, sub_id, targets, max_requests, assume_used)
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

    previous = load_previous()
    prev_deals = previous.get("deals") or []
    merged = sort_deals(deals if replace else merge_deals(prev_deals, deals))

    # 수집 결과를 가격 이력에 기록하고, 누적 평균가·할인율을 계산해 함께 저장한다.
    updated_at = kst_str(datetime.now(timezone.utc))
    hist = load_history()
    reconcile_from_previous(hist, previous)
    recorded = sum(1 for d in deals if add_observation(hist, d, updated_at, "수집 결과"))
    finalize_history(hist)
    discounted = apply_price_stats(merged, hist)
    save_history(hist)

    write_json({
        "status": "ok",
        "updatedAt": updated_at,
        "statsAt": updated_at,
        "notice": "",
        "watchlist": [],
        "categories": CATEGORIES,
        "count": len(merged),
        "priceStats": {"products": len(hist.get("products") or {}), "discounts": discounted},
        "deals": merged,
    })
    print(f"[done] 총 {len(merged)}개 상품으로 deals.json 갱신 "
          f"(이전 {len(prev_deals)} + 신규/갱신 {len(deals)} · 병합 {'교체' if replace else '적용'})")
    print(f"[history] 관측 {recorded}건 기록 · 이력 상품 {len(hist.get('products') or {})}개 · "
          f"평균 대비 할인 {discounted}건")
    if failed:
        print(f"[note] 응답이 비어 있던 카테고리: {', '.join(sorted(set(failed)))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

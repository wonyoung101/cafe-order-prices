#!/usr/bin/env python3
"""카페 발주 단가표 · 가격 이력(price_history.json) 초기 시드 스크립트

저장소 커밋에 남아 있는 실제 `deals.json` 스냅샷에서 상품별 관측 가격을 뽑아
`data/price_history.json` 을 만든다. **네트워크 요청도, 임의의 가짜 가격도 없다.**

- 관측 시각은 각 스냅샷의 `updatedAt`(수집 시각)을 그대로 쓴다.
- 같은 `updatedAt` 을 가진 스냅샷(내용만 바뀐 커밋)은 한 번만 기록한다.
- 기존 `data/price_history.json` 이 있으면 (시각, 가격) 기준으로 중복 없이 병합한다.

사용법:
  python scripts/seed_price_history.py                 # 기본 스냅샷 목록으로 시드
  python scripts/seed_price_history.py --dry-run       # 파일 쓰기 없이 결과만 확인
  python scripts/seed_price_history.py --commits 56e0530,e7b70fd,f26fccf,9b03795
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = ROOT / "data" / "price_history.json"
KST = timezone(timedelta(hours=9))

# deals.json 스냅샷이 남아 있는 실제 커밋 (오래된 순)
DEFAULT_COMMITS = [
    "56e0530",   # 2026-11-16 00:12 KST · 최초 실데이터 수집 40건
    "e7b70fd",   # 2026-11-16 00:51 KST · 카테고리 9개 확장 75건
    "f26fccf",   # 2026-11-16 02:07 KST · 2차 확장 141건
    "9b03795",   # 2026-11-16 07:02 KST · 정기 갱신 156건
]


def git_show(commit: str, path: str = "deals.json") -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout
    except Exception:
        return None


def load_history() -> dict:
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"updatedAt": None, "source": "git snapshots", "products": {}}


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    commits = DEFAULT_COMMITS
    for i, a in enumerate(argv):
        if a == "--commits" and i + 1 < len(argv):
            commits = [c.strip() for c in argv[i + 1].split(",") if c.strip()]
        elif a.startswith("--commits="):
            commits = [c.strip() for c in a.split("=", 1)[1].split(",") if c.strip()]

    hist = load_history()
    products: dict[str, dict] = hist.get("products") or {}

    seen_at: set[str] = set()
    added = 0
    for commit in commits:
        raw = git_show(commit)
        if not raw:
            print(f"[warn] {commit}: deals.json 을 읽을 수 없습니다.", file=sys.stderr)
            continue
        snap = json.loads(raw)
        at = (snap.get("updatedAt") or "").strip()
        deals = snap.get("deals") or []
        if not at or not deals:
            print(f"[warn] {commit}: updatedAt/deals 가 비어 있습니다.", file=sys.stderr)
            continue
        key = f"{commit}:{at}"
        if at in seen_at:
            print(f"[skip] {commit}: 이미 기록한 수집 시각({at}) — 건너뜀")
            continue
        seen_at.add(at)
        n = 0
        for d in deals:
            pid = str(d.get("productId") or "")
            price = d.get("productPrice")
            if not pid or not price:
                continue
            entry = products.setdefault(pid, {"productName": d.get("productName", ""),
                                              "category": d.get("category", ""),
                                              "observations": []})
            obs = entry["observations"]
            entry["productName"] = d.get("productName") or entry.get("productName", "")
            if any(o.get("at") == at and o.get("price") == int(price) for o in obs):
                continue
            obs.append({"at": at, "price": int(price), "source": f"deals.json@{commit}"})
            added += 1
            n += 1
        print(f"[seed] {commit} · {at} · {len(deals)}건 중 {n}건 관측 기록")

    # 관측 시각 기준 정렬 + 통계
    now = datetime.now(KST)
    for pid, entry in products.items():
        entry["observations"].sort(key=lambda o: o.get("at") or "")
        prices = [o["price"] for o in entry["observations"]]
        entry["priceCount"] = len(prices)
        entry["avgPrice"] = round(sum(prices) / len(prices)) if prices else None
        entry["minPrice"] = min(prices) if prices else None
        entry["maxPrice"] = max(prices) if prices else None
        entry["lastPrice"] = prices[-1] if prices else None
        entry["lastAt"] = entry["observations"][-1]["at"] if entry["observations"] else None

    hist["products"] = products
    hist.setdefault("source", "git snapshots")
    hist["updatedAt"] = now.strftime("%Y-%m-%d %H:%M KST")

    print(f"[done] 상품 {len(products)}개 · 신규 관측 {added}건 · "
          f"2회 이상 관측 {sum(1 for p in products.values() if p['priceCount'] >= 2)}개")

    if dry_run:
        print("[dry-run] 파일을 쓰지 않았습니다.")
        return 0

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(HISTORY_PATH)
    print(f"[write] {HISTORY_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

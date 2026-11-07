#!/usr/bin/env python3
"""경주일 실시간 결과 갱신: 공공데이터포털 경주성적·구간 기록을 받아 latest.json에 합친다."""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = "https://apis.data.go.kr/B551015"
MEET_JEJU = 2
PAUSE = 0.15  # 호출 사이 대기(초). 초당 호출 제한(오류 23)을 피하기 위함

ENDPOINTS = {
    "results":  ("jejuhorseresult/getjejuhorseresult", "json"),
    "pace":     ("API4_3/raceResult_3", "json"),
}

ERRORS = {
    "12": "API 주소가 없습니다. 엔드포인트 이름을 확인하세요.",
    "20": "이 API에 대한 활용신청이 안 되어 있거나 승인 대기 중입니다.",
    "22": "오늘 호출 한도를 넘었습니다. 내일 다시 실행하세요.",
    "23": "초당 호출이 너무 많습니다. PAUSE 값을 늘리세요.",
    "30": "등록되지 않은 인증키입니다. 키를 확인하거나 승인 후 1~2시간 기다리세요.",
    "31": "인증키 사용 기한이 만료됐습니다.",
}


class ApiError(Exception):
    pass


def call(key, name, params):
    path, fmt = ENDPOINTS[name]
    q = {"ServiceKey": key, **params}
    if fmt == "json":
        q["_type"] = "json"
    url = f"{BASE}/{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": "jeju-racecard/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", "replace")
            break
        except Exception as e:  # 네트워크 일시 오류는 두 번까지 재시도
            if attempt == 2:
                raise ApiError(f"{name}: 연결 실패 ({e})")
            time.sleep(1.5)
    time.sleep(PAUSE)
    return parse(name, raw, fmt)


def parse(name, raw, fmt):
    text = raw.strip()
    if text.startswith("<"):
        root = ET.fromstring(text.replace("<script/>", ""))
        code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode")
        if code and code not in ("00", "0"):
            raise ApiError(f"{name}: 오류 {code} - {ERRORS.get(code, root.findtext('.//errMsg') or '')}")
        items = [{c.tag: (c.text or "") for c in it} for it in root.iter("item")]
        total = int(root.findtext(".//totalCount") or len(items))
        return items, total
    data = json.loads(text)
    if "OpenAPI_ServiceResponse" in data:
        h = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
        code = str(h.get("returnReasonCode", ""))
        raise ApiError(f"{name}: 오류 {code} - {ERRORS.get(code, h.get('errMsg', ''))}")
    body = data.get("response", {}).get("body", {})
    items = body.get("items") or {}
    items = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(items, dict):
        items = [items]
    return items, int(body.get("totalCount") or 0)


def fetch_all(key, name, params, max_pages=1, rows=100):
    out = []
    for page in range(1, max_pages + 1):
        items, total = call(key, name, {**params, "pageNo": page, "numOfRows": rows})
        out.extend(items)
        if len(out) >= total or not items:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("race_dt")
    ap.add_argument("--key", default=os.environ.get("JEJU_API_KEY"))
    ap.add_argument("--merge-into", default="latest.json")
    ap.add_argument("--out")
    ap.add_argument("--only-race-days", action="store_true")
    ap.add_argument("--result-pages", type=int, default=5)
    a = ap.parse_args()
    if not a.key:
        sys.exit("JEJU_API_KEY 없음")
    return quick_results(a)


def quick_results(a):
    """경주일 중 빠른 갱신: 경주성적 최근 몇 페이지 + 당일 구간 기록만 받아 기존 묶음에 병합."""
    base = {}
    if a.merge_into and os.path.exists(a.merge_into):
        with open(a.merge_into, encoding="utf-8") as f:
            base = json.load(f)
    if a.only_race_days:
        sheet = base.get("sheet") or []
        if not any(str(r.get("rcDate", "")).replace("/", "") == a.race_dt for r in sheet):
            print(f"{a.race_dt}: 출전표에 경주가 없어 건너뜁니다.")
            sys.exit(3)

    errors = []
    print("· 경주성적 (최근) ...", end=" ", flush=True)
    try:
        new_res = fetch_all(a.key, "results", {}, max_pages=min(a.result_pages, 5))
        print(f"{len(new_res)}건")
    except ApiError as e:
        print("실패"); errors.append(str(e)); new_res = []
    key = lambda r: f"{str(r.get('rcDate','')).replace('/','')}|{r.get('gbn','')}|{r.get('rcNo','')}|{r.get('hrNo') or r.get('hrName')}"
    merged = {key(r): r for r in (base.get("results") or [])}
    before = len(merged)
    for r in new_res:
        merged[key(r)] = r
    results = sorted(merged.values(), key=lambda r: (str(r.get("rcDate", "")), int(r.get("rcNo") or 0)), reverse=True)
    added = len(merged) - before

    # 구간 기록: 오늘 + 새 결과에 나온 날짜 중 아직 구간 기록이 없는 날
    have = {str(p.get("rcDate", "")) for p in (base.get("pace") or [])}
    want = {a.race_dt} | {str(r.get("rcDate", "")).replace("/", "") for r in new_res if r.get("gbn") == "R"}
    old_pace = base.get("pace") or []
    pace = [p for p in old_pace if str(p.get("rcDate", "")) != a.race_dt]  # 오늘 것은 새로 받음
    got = 0
    for d8 in sorted(want):
        if d8 != a.race_dt and d8 in have:
            continue
        try:
            rows = fetch_all(a.key, "pace", {"meet": MEET_JEJU, "rc_date": d8}, max_pages=2)
            pace.extend(rows); got += len(rows)
        except ApiError as e:
            errors.append(str(e)); break
    if not any(str(p.get("rcDate", "")) == a.race_dt for p in pace):
        # 오늘 구간 기록을 못 받았으면(오류·지연) 이미 있던 오늘 기록을 지우지 않는다
        pace.extend(p for p in old_pace if str(p.get("rcDate", "")) == a.race_dt)
    print(f"· 구간 기록 ... {got}건 (오늘 {a.race_dt} 포함)")

    bundle = dict(base) if base else {"bundle": 1, "sheet": [], "races": [], "reg": [], "runs": [], "jockeys": [], "trainers": [], "horses": []}
    bundle.update({"bundle": 1, "race_dt": base.get("race_dt", a.race_dt), "collected": time.strftime("%Y-%m-%d %H:%M"),
                   "results": results, "pace": pace, "live": {"date": a.race_dt, "at": time.strftime("%Y-%m-%d %H:%M")}})
    out = a.out or a.merge_into or f"jeju_bundle_{a.race_dt}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False)
    print(f"\n저장: {out} (결과 {len(results)}건, 새로 {added}건 · 구간 {len(pace)}건)")
    if errors:
        print("\n확인이 필요한 오류:")
        for e in dict.fromkeys(errors):
            print("  -", e)
    if errors and not new_res and not got:
        # 전부 실패(키 오류 등): 파일은 원래 내용 그대로라 커밋되지 않는다
        sys.exit(4)
    if not new_res and not got:
        sys.exit(2)


if __name__ == "__main__":
    main()

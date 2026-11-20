#!/usr/bin/env python3
"""国土交通省「不動産情報ライブラリ」API（XIT001）から沖縄県の取引価格を四半期ごとに取得し、
市町村別・用途地域別の集計値を保存する。あわせて、土地の個別分析（字ごとの取引事例・接面道路無の事例など）
のため、2020年以降の公表データ（地番・氏名を含まない国交省の公表項目）を四半期ごとに data/reinfolib/raw/ に保存する。
APIキーは環境変数 REINFOLIB_API_KEY（GitHub Actions Secrets）から読み、どのファイルにも保存しない。"""
import os, re, json, gzip, statistics as st, urllib.request, datetime
from collections import defaultdict

KEY = os.environ.get("REINFOLIB_API_KEY", "").strip()
if not KEY:
    raise SystemExit("REINFOLIB_API_KEY is not set")
API = "https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001?year={y}&quarter={q}&area=47&language=ja"
API_CITY = "https://www.reinfolib.mlit.go.jp/ex-api/external/XIT002?area=47&language={lang}"
OUT_DIR = "data/reinfolib"
RAW_DIR = OUT_DIR + "/raw"
RAW_START = 2020  # 個別取引（公表項目）を保存する最初の年
CREDIT = "このサービスは、国土交通省の不動産情報ライブラリのAPI機能を使用していますが、提供情報の最新性、正確性、完全性等が保証されたものではありません。"
RAW_FIELDS = ["Period", "Type", "Region", "MunicipalityCode", "Municipality", "DistrictName", "DistrictCode", "TradePrice",
              "PricePerUnit", "UnitPrice", "Area", "LandShape", "Frontage", "TotalFloorArea", "BuildingYear", "Structure",
              "Use", "Purpose", "Direction", "Classification", "Breadth", "CityPlanning", "CoverageRatio", "FloorAreaRatio",
              "Renovation", "Remarks", "FloorPlan", "PriceCategory"]
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(RAW_DIR, exist_ok=True)

def fetch(y, q, url=None):
    req = urllib.request.Request(url or API.format(y=y, q=q), headers={"Ocp-Apim-Subscription-Key": KEY, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            b = r.read()
            if r.headers.get("Content-Encoding") == "gzip" or b[:2] == b"\x1f\x8b":
                b = gzip.decompress(b)
        d = json.loads(b.decode("utf-8"))
        return d.get("data", []) if d.get("status") == "OK" else []
    except Exception as e:
        if not url: print(f"{y}Q{q}: not published or fetch failed ({type(e).__name__})")
        return []

def num(s):
    s = re.sub(r"[^\d.]", "", s or "")
    return float(s) if s else None

# XIT002: 沖縄県の市町村一覧（コード→正式名）。取引データの市町村名はコードで正規化する
CITY_JA = {c["id"]: c["name"] for c in fetch(0, 0, API_CITY.format(lang="ja"))}
CITY_EN = {c["id"]: c["name"] for c in fetch(0, 0, API_CITY.format(lang="en"))}
if len(CITY_JA) < 41:
    print(f"warning: XIT002 returned {len(CITY_JA)} municipalities")
def muni(r):
    return CITY_JA.get(r.get("MunicipalityCode", ""), re.sub(r"^.+?郡", "", r.get("Municipality", "")))

def zone(cp):
    if cp in ("商業地域", "近隣商業地域"): return "com"
    if "住居" in cp: return "res"
    if "工業" in cp: return "ind"
    return "out"

def land_unit(r):
    if r.get("Type") != "宅地(土地)": return None
    p, a = num(r.get("TradePrice")), num(r.get("Area"))
    return num(r.get("UnitPrice")) or (p / a if p and a else None)

def med(l): return round(st.median(l)) if len(l) >= 3 else None

def aggregate(rows):
    def blank(): return {"n": 0, "land": [], "mansion": [], "house": [], "com": [], "res": [], "ind": [], "out": [],
                         "shop": 0, "office": 0, "whs": 0, "r6": 0, "rn": 0}
    D = defaultdict(blank); P = blank()
    for r in rows:
        for d in (D[muni(r)], P):
            d["n"] += 1
            p = num(r.get("TradePrice")); u = land_unit(r)
            if u: d["land"].append(u); d[zone(r.get("CityPlanning", ""))].append(u)
            if r.get("Type") == "中古マンション等" and p: d["mansion"].append(p)
            if r.get("Type") == "宅地(土地と建物)" and p: d["house"].append(p)
            pu = r.get("Purpose", "")
            if pu == "店舗": d["shop"] += 1
            if pu == "事務所": d["office"] += 1
            if pu in ("倉庫", "工場"): d["whs"] += 1
            b = num(r.get("Breadth"))
            if b is not None:
                d["rn"] += 1
                if b >= 6: d["r6"] += 1
    def pack(d):
        return {"n": d["n"], "land": [len(d["land"]), med(d["land"])], "mansion": [len(d["mansion"]), med(d["mansion"])],
                "house": [len(d["house"]), med(d["house"])],
                "zone": {k: [len(d[k]), med(d[k])] for k in ("com", "res", "ind", "out")},
                "biz": [d["shop"], d["office"], d["whs"]], "road": [d["r6"], d["rn"]]}
    for name in CITY_JA.values(): D[name]  # 取引0件の市町村も必ず含める（41市町村）
    out = {k: pack(v) for k, v in sorted(D.items(), key=lambda x: -x[1]["n"])}
    out["_県計"] = pack(P)
    return out

today = datetime.date.today()

# --- 個別取引（公表項目）の保存：2020年以降。件数が前回と同じ四半期は書き換えない ---
raw_index_path = f"{RAW_DIR}/index.json"
try: raw_index = json.load(open(raw_index_path, encoding="utf-8"))
except Exception: raw_index = {"quarters": {}}
raw_cache = {}
for y in range(RAW_START, 2024):
    for q in range(1, 5):
        tag = f"{y}Q{q}"
        if tag in raw_index["quarters"] and os.path.exists(f"{RAW_DIR}/{tag}.json"):
            continue  # 2023年以前は確定済み。一度保存したら取り直さない
        rows = fetch(y, q)
        if rows:
            raw_cache[(y, q)] = rows

def save_raw(y, q, rows):
    tag = f"{y}Q{q}"
    if raw_index["quarters"].get(tag) == len(rows) and os.path.exists(f"{RAW_DIR}/{tag}.json"):
        return False
    slim = [{k: r.get(k, "") for k in RAW_FIELDS if r.get(k, "") != ""} for r in rows]
    json.dump({"quarter": tag, "fetched": today.isoformat(), "source": "国土交通省 不動産情報ライブラリ XIT001（沖縄県）",
               "credit": CREDIT, "n": len(slim), "rows": slim},
              open(f"{RAW_DIR}/{tag}.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    raw_index["quarters"][tag] = len(rows)
    return True

for (y, q), rows in raw_cache.items():
    save_raw(y, q, rows)

quarters = []
for y in range(2024, today.year + 1):
    for q in range(1, 5):
        if (y, q) <= (today.year, (today.month - 1) // 3 + 1):
            quarters.append((y, q))

prev_summary = {}
if os.path.exists("reinfolib-okinawa.json"):
    try: prev_summary = json.load(open("reinfolib-okinawa.json", encoding="utf-8"))
    except Exception: prev_summary = {}
prev_counts = {s["quarter"]: s["n"] for s in prev_summary.get("series", [])}

raw = {}; series = []
for y, q in quarters:
    rows = fetch(y, q)
    if not rows: continue
    raw[(y, q)] = rows
    save_raw(y, q, rows)
    agg = aggregate(rows)
    tag = f"{y}Q{q}"
    if prev_counts.get(tag) == len(rows) and os.path.exists(f"{OUT_DIR}/{tag}.json"):
        pass  # 件数が前回と同じ四半期は書き換えない（無駄なコミットを防ぐ）
    else:
      json.dump({"quarter": tag, "fetched": today.isoformat(), "source": "国土交通省 不動産情報ライブラリ XIT001（沖縄県）", "munis": agg},
                open(f"{OUT_DIR}/{tag}.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    P = agg["_県計"]
    series.append({"quarter": tag, "n": P["n"], "land": P["land"], "mansion": P["mansion"], "house": P["house"]})

if not series:
    raise SystemExit("no data fetched")
raw_index.update({"updated": today.isoformat(), "credit": CREDIT,
                  "note": "沖縄県の不動産取引価格情報（公表項目のみ）。ファイル名は四半期。DistrictName（字名）で絞り込んで使う。"})
raw_index["quarters"] = dict(sorted(raw_index["quarters"].items()))
json.dump(raw_index, open(raw_index_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
pub = sorted(raw.keys())
latest4 = pub[-4:]
prev4 = [(y - 1, q) for (y, q) in latest4 if (y - 1, q) in raw]
json.dump([{"code": k, "name": v, "name_en": CITY_EN.get(k, "")} for k, v in CITY_JA.items()],
          open(f"{OUT_DIR}/municipalities.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
summary = {
    "updated": today.isoformat(),
    "municipalities": {k: {"name": v, "name_en": CITY_EN.get(k, "")} for k, v in CITY_JA.items()},
    "source": "国土交通省「不動産情報ライブラリ」API XIT001（不動産取引価格情報・成約価格情報、沖縄県）をGitHub Actionsで取得しkoya集計",
    "note": "3件未満の中央値はnull。中央値は取引構成で大きく動くため前年比は参考。公表項目の個別取引は data/reinfolib/raw/ に保存。APIキーは保存しない。",
    "latest_window": f"{latest4[0][0]}Q{latest4[0][1]}〜{latest4[-1][0]}Q{latest4[-1][1]}",
    "latest_quarter": f"{pub[-1][0]}Q{pub[-1][1]}",
    "new_or_changed_quarters": [s["quarter"] for s in series if prev_counts.get(s["quarter"]) != s["n"]],
    "series": series,
    "latest4": aggregate([r for k in latest4 for r in raw[k]]),
    "prev_year_window": aggregate([r for k in prev4 for r in raw[k]]) if prev4 else None,
}
if not summary["new_or_changed_quarters"] and prev_summary:
    print("no new or changed quarters; summary kept as is")
else:
    json.dump(summary, open("reinfolib-okinawa.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print("quarters:", [s["quarter"] + ":" + str(s["n"]) for s in series])
print("changed:", summary["new_or_changed_quarters"])

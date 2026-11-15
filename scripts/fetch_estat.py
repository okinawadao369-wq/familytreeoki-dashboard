#!/usr/bin/env python3
"""e-Stat API（社会・人口統計体系 市区町村データ）から沖縄県41市町村の指標を取得し、最新値と推移を保存する。
appId は環境変数 ESTAT_APP_ID（GitHub Actions Secrets）から読む。appIdは保存しない。"""
import os, json, urllib.request, urllib.parse, datetime
APPID=os.environ.get("ESTAT_APP_ID","").strip()
if not APPID: raise SystemExit("ESTAT_APP_ID is not set")
BASE="https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"
# statsDataId -> {code: (表示名, 単位)}
SPEC={
 "0000020201":{"A1101":("総人口","人"),"A1301":("15歳未満人口","人"),"A1303":("65歳以上人口","人"),"A1405":("0〜5歳人口","人"),"A4101":("出生数","人"),"A5103":("転入者数","人"),"A5104":("転出者数","人"),"A6108":("昼夜間人口比率","%"),"A7101":("世帯数","世帯"),"A8401":("母子世帯数","世帯")},
 "0000020203":{"C120110":("課税対象所得","千円"),"C120120":("納税義務者数（所得割）","人"),"C2108":("事業所数（民営）","所"),"C210843":("小売業事業所数（民営）","所"),"C210847":("宿泊業・飲食サービス業事業所数（民営）","所"),"C210848":("生活関連サービス業・娯楽業事業所数（民営）","所"),"C210850":("医療・福祉事業所数（民営）","所"),"C2208":("従業者数（民営）","人")},
 "0000020206":{"F1101":("労働力人口","人"),"F1107":("完全失業者数","人"),"F2221":("第3次産業就業者数","人")},
 "0000020209":{"I5102":("一般診療所数","所"),"I6100":("医師数","人")},
}
def call(api,q):
    url=f"https://api.e-stat.go.jp/rest/3.0/app/json/{api}?"+urllib.parse.urlencode(dict(q,appId=APPID))
    with urllib.request.urlopen(url,timeout=120) as r: return json.loads(r.read().decode("utf-8"))
def get(sid,codes):
    m=call("getMetaInfo",{"statsDataId":sid})["GET_META_INFO"]["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"]
    area={c["@code"]:c["@name"].replace("沖縄県 ","") for o in m if o["@id"]=="area" for c in (o["CLASS"] if isinstance(o["CLASS"],list) else [o["CLASS"]]) if c["@code"].startswith("47")}
    vals=[]; start=None
    while True:
        q={"statsDataId":sid,"cdCat01":",".join(codes),"cdArea":",".join(area),"metaGetFlg":"N","limit":"100000"}
        if start: q["startPosition"]=start
        st=call("getStatsData",q)["GET_STATS_DATA"]
        if st["RESULT"]["STATUS"]!=0: raise SystemExit(f"{sid}: {st['RESULT']['ERROR_MSG']}")
        v=st["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]; vals+= v if isinstance(v,list) else [v]
        start=st["STATISTICAL_DATA"]["RESULT_INF"].get("NEXT_KEY")
        if not start: break
    return area,vals
out={"updated":datetime.date.today().isoformat(),"source":"総務省統計局 e-Stat API「社会・人口統計体系（統計でみる市区町村のすがた）」市区町村データ・基礎データ","indicators":{},"munis":{}}
for sid,spec in SPEC.items():
    area,vals=get(sid,list(spec))
    for code,(name,unit) in spec.items(): out["indicators"][code]={"name":name,"unit":unit,"statsDataId":sid}
    for v in vals:
        a=v.get("@area","")
        if not a.startswith("47"): continue
        try: x=float(v["$"])
        except: continue
        nm=area.get(a,a)
        rec=out["munis"].setdefault(nm,{"code":a})
        s=rec.setdefault(v["@cat01"],{})
        s[v["@time"][:4]]=x
# 最新値と派生指標
for nm,rec in out["munis"].items():
    latest={}
    for c in out["indicators"]:
        s=rec.get(c)
        if s: y=max(s); latest[c]=[y,s[y]]
    rec["latest"]=latest
    L=lambda c: latest.get(c,[None,None])[1]
    if L("C120110") and L("C120120"): rec["derived_income_per_taxpayer_k"]=round(L("C120110")/L("C120120"))  # 千円
    if L("F1107") is not None and L("F1101"): rec["derived_unemployment_rate"]=round(L("F1107")/L("F1101")*100,1)
    if L("A1303") and L("A1101"): rec["derived_aging_rate"]=round(L("A1303")/L("A1101")*100,1)
    if L("A1405") and L("A1101"): rec["derived_under6_rate"]=round(L("A1405")/L("A1101")*100,2)
prev=None
if os.path.exists("estat-okinawa.json"):
    try: prev=json.load(open("estat-okinawa.json",encoding="utf-8"))
    except Exception: prev=None
if prev and prev.get("munis")==out["munis"]:
    print("no data change; file kept")
else:
    json.dump(out,open("estat-okinawa.json","w",encoding="utf-8"),ensure_ascii=False,separators=(",",":"))
print("munis",len(out["munis"]),"indicators",len(out["indicators"]))

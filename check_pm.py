import sys,json,requests
slug=sys.argv[1] if len(sys.argv)>1 else "itf-santos6-damazi1-2026-09-19"
r=requests.get("https://gamma-api.polymarket.com/events",params={"slug":slug},timeout=15)
evs=r.json()
if not evs: print("not found"); sys.exit(1)
m=evs[0]["markets"][0]
print("event:",evs[0].get("title"))
print("outcomePrices:",m.get("outcomePrices"))
print("updatedAt:",m.get("updatedAt"))
toks=json.loads(m["clobTokenIds"])
outs=json.loads(m["outcomes"])
for i,t in enumerate(toks):
    name=outs[i] if i<len(outs) else str(i)
    b=requests.get("https://clob.polymarket.com/book",params={"token_id":t},timeout=15).json()
    bids=b.get("bids") or []
    asks=b.get("asks") or []
    bb=max(((float(x["price"]),float(x.get("size",0))) for x in bids),default=(None,0))
    ba=min(((float(x["price"]),float(x.get("size",0))) for x in asks),default=(None,0))
    print(name+": best_bid="+str(bb)+" best_ask="+str(ba))

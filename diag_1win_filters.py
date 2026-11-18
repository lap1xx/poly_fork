# -*- coding: utf-8 -*-
"""Diagnostic: what filter combos does 1win accept?"""
import json, requests

PARTNER_ID = "44ba10e5-7df2-47ab-a44d-dc93803c7a6e"
URL = "https://api-gateway.top-parser.com/matches/get-many"
H = {
    "accept": "application/json",
    "content-type": "application/json",
    "x-external-partner-id": PARTNER_ID,
    "x-lang": "ru-RU",
    "x-user-location": "RU",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
}

VARIANTS = {
    "A_only_esport":         {"includeSportType":"esport","excludeSportType":"polybet","limit":200,"hotsLimit":100},
    "B_all_no_esport":       {"excludeSportType":"polybet","limit":200,"hotsLimit":100},
    "C_only_tennis":         {"sportId":33,"limit":200},
    "D_esport_live_only":    {"includeSportType":"esport","excludeSportType":"polybet","service":"LIVE","limit":200,"hotsLimit":100},
    "E_all_live_only":       {"excludeSportType":"polybet","service":"LIVE","limit":200,"hotsLimit":100},
    "F_all_prematch_only":   {"excludeSportType":"polybet","service":"PREMATCH","limit":200,"hotsLimit":100},
    "G_only_sport":          {"includeSportType":"default","excludeSportType":"polybet","limit":200,"hotsLimit":100},
    "H_no_filter":           {"limit":200,"hotsLimit":100},
}

def summarize(items, key):
    if not items:
        return "0 items"
    # sport counts
    cnt = {}
    svc = {}
    for m in items:
        s = m.get("sport", {}).get("slug") or m.get("sportTag") or f"sp{m.get('sportId')}"
        cnt[s] = cnt.get(s, 0) + 1
        sv = m.get("service", "?")
        svc[sv] = svc.get(sv, 0) + 1
    top = sorted(cnt.items(), key=lambda x:-x[1])[:10]
    return f"{len(items)} items | sports={dict(top)} | service={svc}"

for name, body in VARIANTS.items():
    print(f"\n===== {name} =====")
    print(f"  body: {json.dumps(body)}")
    try:
        r = requests.post(URL, json=body, headers=H, timeout=15)
        print(f"  status={r.status_code} len={len(r.text)}")
        if r.status_code != 200:
            print(f"  body: {r.text[:200]}")
            continue
        data = r.json()
        items = ((data.get("result") or {}).get("items")) or []
        print(f"  {summarize(items, name)}")
        if items:
            # показать первые 2 матча
            for m in items[:2]:
                print(f"    - [{m.get('service')}] {m.get('sport',{}).get('slug')}: {m.get('name')} (id={m.get('id')})")
    except Exception as e:
        print(f"  ERR: {e}")

print("\nDONE")
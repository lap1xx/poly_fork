# diag_bind.py — показывает что видит bb_bridge и что тянет PM
import time, requests
import bb_bridge

print("=" * 60)
print("[1] Жду матчи от BB (через chrome_bridge)...")
bb_bridge.wait_ready(timeout=30, min_matches=3)

s = bb_bridge.get_global_stream()
matches = s.snapshot_matches()
print(f"[1] BB matches: {len(matches)}")

labels, f8s = {}, {}
for m in matches:
    labels[m.get("_label")] = labels.get(m.get("_label"), 0) + 1
    f8s[m.get("f8_sport")] = f8s.get(m.get("f8_sport"), 0) + 1
print(f"    by _label : {labels}")
print(f"    by f8_sport: {f8s}")

print("\n[2] Первые 5 матчей BB:")
for m in matches[:5]:
    teams = m.get("teams") or []
    names = [t.get("name") or t.get("short_name") or "?" for t in teams]
    print(f"    mid={m.get('match_id')} label={m.get('_label')} f8={m.get('f8_sport')} "
          f"start={m.get('start_dttm')}")
    print(f"        teams={names}")

print("\n[3] PM markets по tag_slug (что мы тянем):")
for tag in ["tennis", "atp", "wta", "cs2", "dota-2", "esports"]:
    try:
        r = requests.get("https://gamma-api.polymarket.com/events",
                         params={"tag_slug": tag, "active": "true",
                                 "closed": "false", "limit": 3},
                         timeout=10)
        if r.status_code != 200:
            print(f"    [{tag}] HTTP {r.status_code}")
            continue
        ev = r.json()
        print(f"    [{tag}] {len(ev)} events")
        for e in ev[:2]:
            print(f"        slug={e.get('slug')}")
    except Exception as e:
        print(f"    [{tag}] ERR {e}")

print("\n[4] Проверю: что возвращает fetch_live_pm_slugs() в polyarb_v4:")
try:
    import polyarb_v4 as p
    fn = getattr(p, "fetch_live_pm_slugs", None)
    if fn:
        res = fn()
        print(f"    fetch_live_pm_slugs → {len(res)} слагов")
        for sl in list(res)[:5]:
            print(f"        {sl}")
    else:
        print("    функция fetch_live_pm_slugs не найдена в polyarb_v4")
except Exception as e:
    print(f"    ERR: {e}")

print("\n[5] Готово.")

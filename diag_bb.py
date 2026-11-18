#!/usr/bin/env python3
# diag_bb.py - BB tree_ws diagnostic. Runs 30s, prints everything.
import time, sys
import bb_bridge

print("[*] connecting to BetBoom tree_ws (verbose=True)...")
st = bb_bridge.get_global_stream(verbose=True)
print("[*] stream started, waiting 30s...")

t0 = time.time()
last_report = 0
while time.time() - t0 < 30:
    time.sleep(1)
    el = time.time() - t0
    if el - last_report >= 5:
        n_m = len(st.matches)
        n_s = sum(len(v) for v in st.stakes.values())
        print(f"    [{el:>4.0f}s] matches={n_m}  stakes={n_s}")
        last_report = el

print()
print("=" * 60)
print(f"RESULT: {len(st.matches)} matches, "
      f"{sum(len(v) for v in st.stakes.values())} stakes total")
print("=" * 60)

if len(st.matches) == 0:
    print()
    print("DIAGNOSIS:")
    print("  - 0 matches => likely rate-limit or BB tree_ws down")
    print("  - check log lines above: do you see 'subscribed'?")
    print("  - if no 'subscribed' => connection refused")
    print("  - if 'subscribed' but no f29 => BB silent (ban)")
    print("  - wait 15 min, retry")

bb_bridge.stop_global_stream()
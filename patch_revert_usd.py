# patch_revert_usd.py
# Откат usd_target обратно к 50.0 / min 20.0 (как было до patch_pm_depth).
# Оставляет только strict walk_book check (фикс фейков).
import pathlib, sys

P = pathlib.Path("polyarb_v4.py")
if not P.exists():
    sys.exit("polyarb_v4.py not found")

src = P.read_text(encoding="utf-8").replace("\r\n", "\n")

old = """    # Размер PM-ноги в USD. Оценка — половина банка (в $), но не меньше $5.
    usd_target = 20.0
    try:
        if bank and rate and rate > 0:
            usd_target = max(5.0, min(200.0, bank / rate * 0.5))
    except Exception:
        pass"""

new = """    # размер PM-ноги в USD (половина банка, но 20..200)
    usd_target = 50.0
    try:
        if bank and rate and rate > 0:
            usd_target = max(20.0, min(200.0, bank / rate * 0.5))
    except Exception:
        pass"""

if "usd_target = 50.0" in src and "max(20.0, min(200.0" in src:
    print("[=] already reverted")
elif old in src:
    src = src.replace(old, new, 1)
    P.write_text(src, encoding="utf-8")
    print("[+] usd_target reverted: 50.0 default, min 20.0")
    print("    ROI calculation unchanged. Only PM depth check remains.")
else:
    print("[!] pattern not found - send me the current build_arb_candidates")
    sys.exit(1)

print()
print("Restart:")
print("    python -B bot_live.py")

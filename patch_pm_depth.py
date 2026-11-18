# patch_pm_depth.py
# Заменяет блок build_arb_candidates в polyarb_v4.py на строгую проверку
# глубины PM-стакана. Убирает fallback `else ba`, который рождал фейки.
import pathlib, sys

P = pathlib.Path("polyarb_v4.py")
if not P.exists():
    sys.exit("polyarb_v4.py not found")

src = P.read_text(encoding="utf-8").replace("\r\n", "\n")

# ---------- 1) старый блок построения PM-ног ----------
old = """    # размер PM-ноги в USD (половина банка, но 20..200)
    usd_target = 50.0
    try:
        if bank and rate and rate > 0:
            usd_target = max(20.0, min(200.0, bank / rate * 0.5))
    except Exception:
        pass

    pm_legs = []
    for i, oc in enumerate(pm_o):
        ba = best_ask(pm_t[i])
        if ba <= 0:
            return []
        # проскальзывание: покупаем ~usd_target USD
        shares = usd_target / ba
        avg = walk_book(pm_t[i], shares)
        effective = avg if (avg and avg > 0) else ba
        pm_legs.append({
            "source": "Polymarket",
            "outcome": oc,
            "o": 1.0 / effective,
            "c": 0.0,
            "s": slip,
            "token": pm_t[i],
        })"""

# ---------- 2) новый блок ----------
new = """    # Размер PM-ноги в USD. Оценка — половина банка (в $), но не меньше $5.
    usd_target = 20.0
    try:
        if bank and rate and rate > 0:
            usd_target = max(5.0, min(200.0, bank / rate * 0.5))
    except Exception:
        pass

    pm_legs = []
    for i, oc in enumerate(pm_o):
        ba = best_ask(pm_t[i])
        if ba <= 0:
            # PM: нет best_ask — стакан пустой или не подписан
            return []
        # сколько shares нужно купить по целевой USD-сумме
        shares = usd_target / ba
        # walk_book: средняя цена за нужный объём.
        # Если объёма не хватает — возвращает None.
        avg = walk_book(pm_t[i], shares)
        if not avg or avg <= 0:
            # В стакане НЕ хватает объёма под целевую ставку.
            # НЕ подменяем на best_ask — это и был баг, дававший фейки.
            return []
        # доп. защита: если средняя цена хуже best_ask более чем на 10%,
        # стакан тонкий, вилка ненадёжна
        if avg > ba * 1.10:
            return []
        pm_legs.append({
            "source": "Polymarket",
            "outcome": oc,
            "o": 1.0 / avg,
            "c": 0.0,
            "s": slip,
            "token": pm_t[i],
        })"""

if "if avg > ba * 1.10:" in src:
    print("[=] already patched")
elif old in src:
    src = src.replace(old, new, 1)
    P.write_text(src, encoding="utf-8")
    print("[+] build_arb_candidates: PM depth check installed")
    print("    - removed `else ba` fallback (this was the fake-arb bug)")
    print("    - walk_book must satisfy full usd_target")
    print("    - extra check: avg <= best_ask * 1.10 (slip cap)")
else:
    print("[!] pattern not found")
    print("    send me the current build_arb_candidates block")
    sys.exit(1)

print()
print("Now restart the scanner:")
print("    python -B bot_live.py")

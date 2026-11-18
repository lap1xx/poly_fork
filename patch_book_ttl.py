# patch_book_ttl.py
# Добавляет timestamp к BOOKS и TTL=30s в best_ask/walk_book.
# Устаревшие записи → best_ask=0 → вилка отбрасывается.
import pathlib, sys

P = pathlib.Path("polyarb_v4.py")
if not P.exists():
    sys.exit("polyarb_v4.py not found")

src = P.read_text(encoding="utf-8").replace("\r\n", "\n")

# ---- 1) в Feed._a записывать ts ----
old_write = """        with LOCK:
            BOOKS[aid] = {"bids": bids, "asks": asks}"""
new_write = """        with LOCK:
            BOOKS[aid] = {"bids": bids, "asks": asks, "ts": time.time()}"""

# ---- 2) best_ask с TTL ----
old_ba = """def best_ask(aid):
    with LOCK:
        b = BOOKS.get(aid)
        if not b or not b["asks"]:
            return 0.0
        return b["asks"][0][0]"""
new_ba = """BOOK_TTL = 30.0  # сек: дольше — считаем стакан мёртвым

def best_ask(aid):
    with LOCK:
        b = BOOKS.get(aid)
        if not b or not b["asks"]:
            return 0.0
        ts = b.get("ts", 0.0)
        if ts <= 0 or (time.time() - ts) > BOOK_TTL:
            return 0.0
        return b["asks"][0][0]"""

# ---- 3) walk_book с TTL ----
old_wb = """def walk_book(aid, target):
    with LOCK:
        b = BOOKS.get(aid)
        if not b:
            return None
        levels = list(b["asks"])"""
new_wb = """def walk_book(aid, target):
    with LOCK:
        b = BOOKS.get(aid)
        if not b:
            return None
        ts = b.get("ts", 0.0)
        if ts <= 0 or (time.time() - ts) > BOOK_TTL:
            return None
        levels = list(b["asks"])"""

for old, new, name in (
    (old_write, new_write, "Feed._a"),
    (old_ba, new_ba, "best_ask"),
    (old_wb, new_wb, "walk_book"),
):
    if new in src:
        print("[=] %s already patched" % name)
    elif old in src:
        src = src.replace(old, new, 1)
        print("[+] %s patched" % name)
    else:
        sys.exit("FAIL: pattern for %s not found" % name)

P.write_text(src, encoding="utf-8")
print()
print("DONE. Restart:")
print("    python -B bot_live.py")

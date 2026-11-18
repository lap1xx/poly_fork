"""Restore f3/f4/f6 sends in bb_bridge._connect_and_listen."""
import re, shutil

PATH = "bb_bridge.py"
shutil.copy(PATH, PATH + ".bak")
print(f"[fix] backup: {PATH}.bak")

with open(PATH, encoding="utf-8") as f:
    src = f.read()

# 1) Fix req_settings: add uid param and correct payload
pat = re.compile(
    r'def req_settings\(\):\s*\n'
    r'\s*return msg\(3, enum\(1, 8\) \+ s\(2, "1_hours"\)\)'
)
new_settings = (
    'def req_settings(uid=None):\n'
    '    if uid is None:\n'
    '        uid = uuid.uuid4().hex[:6]\n'
    '    inner = s(1, uid) + s(2, "all") + enum(3, 8)\n'
    '    return msg(3, inner)'
)
src2, n1 = pat.subn(new_settings, src)
if n1:
    print("[fix] req_settings() rewritten")
    src = src2
else:
    print("[fix] WARN: req_settings() pattern not found (already fixed?)")

# 2) Restore f3/f4/f6 sends
old = (
    '            with self.lock:\n'
    '                self._ws_by_label[label] = ws\n'
    '            self.log(f"[BB {label}] attached to Chrome relay'
)
new = (
    '            with self.lock:\n'
    '                self._ws_by_label[label] = ws\n'
    '            ws.settimeout(8.0)\n'
    '            ws.send_binary(req_settings())\n'
    '            ws.send_binary(req_f4(domain, [LIVE]))\n'
    '            ws.send_binary(req_f6(sport_id, LIVE))\n'
    '            ws.send_binary(req_f6(sport_id, PREMATCH))\n'
    '            self.log(f"[BB {label}] subscribed (domain={domain}, sport={sport_id})")\n'
    '            self.log(f"[BB {label}] attached to Chrome relay'
)
if old in src:
    src = src.replace(old, new, 1)
    print("[fix] f3/f4/f6 sends restored")
else:
    print("[fix] WARN: _connect_and_listen pattern not found")
    print("     Открой bb_bridge.py, найди _connect_and_listen и добавь")
    print("     вручную после 'self._ws_by_label[label] = ws':")
    print("       ws.settimeout(8.0)")
    print("       ws.send_binary(req_settings())")
    print("       ws.send_binary(req_f4(domain, [LIVE]))")
    print("       ws.send_binary(req_f6(sport_id, LIVE))")
    print("       ws.send_binary(req_f6(sport_id, PREMATCH))")

with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)

print("[fix] done")

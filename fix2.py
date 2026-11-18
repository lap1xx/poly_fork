# fix2.py - full repair of bb_bridge.py: restore _run_loop + _connect_and_listen
import re, pathlib, shutil, ast

HERE = pathlib.Path(__file__).parent
P = HERE / "bb_bridge.py"
BAK = HERE / "bb_bridge.py.bak"

if BAK.exists():
    shutil.copy(str(BAK), str(P))
    print("restored from .bak")
else:
    print("no .bak, using current file")

src = P.read_text(encoding="utf-8")

NEW_RUN_LOOP = '''    def _run_loop(self, domain, sport_id, label):
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._connect_and_listen(domain, sport_id, label)
                backoff = 2.0
            except Exception as e:
                print(f"[BB {label}] LOOP-ERR {type(e).__name__}: {e}", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 20.0)

'''

NEW_CONNECT = '''    def _connect_and_listen(self, domain, sport_id, label):
        print(f"[BB {label}] connecting to {BB_URL} ...", flush=True)
        ws = websocket.create_connection(
            BB_URL, timeout=10, subprotocols=["protobuf"],
            origin=BB_ORIGIN,
            header={"User-Agent": BB_UA, "Referer": BB_REFERER},
        )
        try:
            with self.lock:
                self._ws_by_label[label] = ws
            print(f"[BB {label}] CONNECTED (domain={domain}, sport={sport_id})", flush=True)
            ws.settimeout(2.0)
            try:
                ws.send_binary(req_settings())
                time.sleep(0.2)
                ws.send_binary(req_f4(domain, [LIVE]))
                time.sleep(0.4)
                ws.send_binary(req_f6(sport_id, LIVE))
                ws.send_binary(req_f6(sport_id, PREMATCH))
                print(f"[BB {label}] SUBSCRIBED (domain={domain}, sport={sport_id})", flush=True)
            except Exception as e:
                print(f"[BB {label}] subscribe send FAILED: {type(e).__name__}: {e}", flush=True)
            while not self._stop.is_set():
                try:
                    r = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException as e:
                    print(f"[BB {label}] conn closed: {e}", flush=True)
                    break
                except Exception as e:
                    print(f"[BB {label}] recv err: {type(e).__name__}: {e}", flush=True)
                    break
                if not r or isinstance(r, str):
                    continue
                fn = top_field(r)
                print(f"[RAW {label}] fn={fn} len={len(r)}", flush=True)
                if fn == 29:
                    self._handle_f29(bytes(r), label)
                elif fn == 30:
                    self._handle_f30(bytes(r), label)
                elif fn == 31:
                    self._handle_f31(bytes(r), label)
                elif fn == 17:
                    self._handle_f17(bytes(r), label)
        finally:
            with self.lock:
                self._ws_by_label.pop(label, None)
            try: ws.close()
            except Exception: pass

'''

def replace(src, name, repl):
    pat = re.compile(r'^[ \t]*def ' + re.escape(name) + r'\(', re.MULTILINE)
    m = pat.search(src)
    if not m:
        print(f"  {name}: NOT FOUND")
        return src
    start = m.start()
    rest = src[m.end():]
    m2 = re.search(r'\n    def ', rest)
    m3 = re.search(r'\ndef ', rest)
    ends = []
    if m2: ends.append(m.end() + m2.start() + 1)
    if m3: ends.append(m.end() + m3.start() + 1)
    end = min(ends) if ends else len(src)
    print(f"  {name}: replacing {end - start} chars")
    return src[:start] + repl + src[end:]

src = replace(src, "_run_loop", NEW_RUN_LOOP)
src = replace(src, "_connect_and_listen", NEW_CONNECT)

P.write_text(src, encoding="utf-8")
print("written")

try:
    ast.parse(src)
    print("AST OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)
#!/usr/bin/env python3
# bot_live.py - AUTO launcher for polyarb_v4.py
# No imports from clob_utils. Just spawns polyarb_v4.py as subprocess.
import sys, subprocess, os, pathlib, secrets, socket, time, webbrowser
from runtime_settings import validate_cli_overrides

HERE = pathlib.Path(__file__).parent
POLYARB = str(HERE / "polyarb_v4.py")

def _wait_for_dashboard(web_proc, timeout=15.0):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if web_proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.25)
    return False

def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print("Usage: python bot_live.py [--min-roi 3.0] [--bank USDT_AMOUNT] [--poll SEC] [--watch-sec SEC] [--alert-gap SEC] [--open-browser] ...")
        print("Any extra args are passed through to polyarb_v4.py")
        return 0

    try:
        validate_cli_overrides(args)
    except ValueError as exc:
        print("[!] Некорректный параметр: " + str(exc))
        return 2

    min_roi = None
    bank = None
    open_browser = False
    extra = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-web":
            i += 1
            continue
        if a == "--open-browser":
            open_browser = True
            i += 1
            continue
        if a == "--min-roi" and i + 1 < len(args):
            min_roi = args[i + 1]
            i += 2
            continue
        if a == "--bank" and i + 1 < len(args):
            bank = args[i + 1]
            i += 2
            continue
        extra.append(a)
        i += 1

    cmd = [
        sys.executable, "-B", POLYARB,
        "--watch",
    ] + extra
    if min_roi is not None:
        cmd.extend(["--min-roi", min_roi])
    if bank is not None:
        cmd.extend(["--bank", bank])

    print("[>] AUTO mode:")
    print("    " + " ".join(cmd))

    # --- web_app (опционально) ---
    web_proc = None
    child_env = os.environ.copy()
    web_path = HERE / "web_app.py"
    if web_path.exists() and "--no-web" not in args:
        try:
            child_env["POLYARB_SCANNER_WS_TOKEN"] = secrets.token_urlsafe(32)
            web_proc = subprocess.Popen(
                [sys.executable, "-B", str(web_path)],
                cwd=str(HERE),
                env=child_env,
            )
            print("[>] web_app started: http://localhost:8080")
        except Exception as e:
            child_env.pop("POLYARB_SCANNER_WS_TOKEN", None)
            print("[!] web_app failed:", e)
            web_proc = None
    if open_browser and "--no-web" in args:
        print("[!] Browser not opened because --no-web disables the dashboard.")
    elif open_browser:
        dashboard_url = "http://127.0.0.1:8080"
        if web_proc is not None and _wait_for_dashboard(web_proc):
            if webbrowser.open(dashboard_url):
                print("[>] Dashboard opened in the default browser: " + dashboard_url)
            else:
                print("[!] Open this dashboard address manually: " + dashboard_url)
        else:
            print("[!] Dashboard did not become ready. Check the backend window/logs.")
    print()

    try:
        subprocess.run(cmd, check=False, cwd=str(HERE), env=child_env)
    except KeyboardInterrupt:
        print("\n[*] stopped by user")
    finally:
        if web_proc is not None:
            try:
                web_proc.terminate()
            except Exception:
                pass
    return 0

if __name__ == "__main__":
    sys.exit(main())

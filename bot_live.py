#!/usr/bin/env python3
# bot_live.py - AUTO launcher for polyarb_v4.py
# No imports from clob_utils. Just spawns polyarb_v4.py as subprocess.
import sys, subprocess, os, pathlib, secrets

HERE = pathlib.Path(__file__).parent
POLYARB = str(HERE / "polyarb_v4.py")

def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print("Usage: python bot_live.py [--min-roi 3.0] [--bank USDT_AMOUNT] ...")
        print("Any extra args are passed through to polyarb_v4.py")
        return 0

    min_roi = "3.0"
    bank = None
    extra = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-web":
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
        "--watch-sec", "20",
        "--poll", "0.3",
        "--alert-gap", "120",
        "--min-roi", min_roi,
    ] + extra
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

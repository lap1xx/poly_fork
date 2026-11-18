#!/usr/bin/env python3
# bot_live.py - AUTO launcher for polyarb_v4.py
# No imports from clob_utils. Just spawns polyarb_v4.py as subprocess.
import sys, subprocess, os, pathlib

HERE = pathlib.Path(__file__).parent
POLYARB = str(HERE / "polyarb_v4.py")

def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print("Usage: python bot_live.py [--min-roi 7.0] [--bank 2000] ...")
        print("Any extra args are passed through to polyarb_v4.py")
        return 0

    min_roi = "5.0"
    bank = "2000"
    extra = []
    i = 0
    while i < len(args):
        a = args[i]
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
        "--watch", "--live",
        "--watch-sec", "20",
        "--poll", "0.3",
        "--alert-gap", "120",
        "--min-roi", min_roi,
        "--bank", bank,
    ] + extra

    print("[>] AUTO mode:")
    print("    " + " ".join(cmd))

    # --- web_app (опционально) ---
    web_proc = None
    web_path = HERE / "web_app.py"
    if web_path.exists() and "--no-web" not in args:
        try:
            web_proc = subprocess.Popen(
                [sys.executable, "-B", str(web_path)],
                cwd=str(HERE),
            )
            print("[>] web_app started: http://localhost:8080")
        except Exception as e:
            print("[!] web_app failed:", e)
            web_proc = None
    print()

    try:
        subprocess.run(cmd, check=False)
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

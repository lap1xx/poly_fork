# bot_live_patch.py
# Патчит bot_live.py: запускает web_app.py параллельно с polyarb_v4.py
# Запускать ОДИН раз через install_web.bat

import pathlib, sys

HERE = pathlib.Path(__file__).parent
P = HERE / "bot_live.py"

if not P.exists():
    print("    [!] bot_live.py не найден")
    sys.exit(1)

src = P.read_text(encoding="utf-8")

if "web_app.py" in src:
    print("    [=] уже пропатчен, пропускаем")
    sys.exit(0)

OLD = """    cmd = [
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
    print()

    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\\n[*] stopped by user")
    return 0
"""

NEW = """    cmd = [
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
        print("\\n[*] stopped by user")
    finally:
        if web_proc is not None:
            try:
                web_proc.terminate()
            except Exception:
                pass
    return 0
"""

if OLD not in src:
    print("    [!] Якорь не найден. Пришли текущий main() в bot_live.py")
    sys.exit(1)

src = src.replace(OLD, NEW, 1)
P.write_text(src, encoding="utf-8")
print("    [+] bot_live.py успешно пропатчен")

#!/usr/bin/env python3
"""Installer for MultiLink Booster (Windows).

Creates Start Menu shortcuts (double-click install.bat, or run `py install.py`):

    MultiLink Booster            -> web dashboard UI (opens in your browser)
    MultiLink Booster Terminal   -> terminal opened in the app folder

Options:
    --desktop     also add a desktop shortcut for the GUI
    --uninstall   remove the shortcuts again
    --dry-run     print what would happen without doing it (works on any OS)
"""
import argparse
import os
import subprocess
import sys

APP_NAME = "MultiLink Booster"


def ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def shortcut_ps(link_path, target, arguments="", workdir="", icon=""):
    parts = [
        "$ws = New-Object -ComObject WScript.Shell",
        f"$sc = $ws.CreateShortcut({ps_quote(link_path)})",
        f"$sc.TargetPath = {ps_quote(target)}",
    ]
    if arguments:
        parts.append(f"$sc.Arguments = {ps_quote(arguments)}")
    if workdir:
        parts.append(f"$sc.WorkingDirectory = {ps_quote(workdir)}")
    parts.append(f"$sc.IconLocation = {ps_quote(icon or target)}")
    parts.append("$sc.Save()")
    return "; ".join(parts)


def run_ps(script, dry):
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    if dry:
        print("  [dry-run] would run: powershell -Command ...")
        print(f"            {script[:160]}...")
        return True
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(f"  shortcut failed: {p.stderr.strip()}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="Install Start Menu shortcuts for MultiLink Booster.")
    ap.add_argument("--desktop", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run

    if sys.platform != "win32" and not dry:
        sys.exit("This installer targets Windows. Use --dry-run to preview it elsewhere.")

    app_dir = os.path.dirname(os.path.abspath(__file__))
    dash_py = os.path.join(app_dir, "dashboard.py")
    if not os.path.exists(dash_py):
        sys.exit(f"dashboard.py not found in {app_dir}")

    py = sys.executable
    pyw = py[:-10] + "pythonw.exe" if py.lower().endswith("python.exe") else py
    if not dry and not os.path.exists(pyw):
        print(f"  warning: {pyw} not found, falling back to {py} (a console window will show)")
        pyw = py

    appdata = os.environ.get("APPDATA", "%APPDATA%")
    start_dir = os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                             "Programs", APP_NAME)
    cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")

    shortcuts = [
        (os.path.join(start_dir, f"{APP_NAME}.lnk"),
         pyw, f'"{dash_py}"', app_dir),
        (os.path.join(start_dir, f"{APP_NAME} Terminal.lnk"),
         cmd_exe, f'/k "cd /d {app_dir}"', app_dir),
    ]
    if args.desktop:
        desktop = os.path.join(os.environ.get("USERPROFILE", "%USERPROFILE%"), "Desktop")
        shortcuts.append((os.path.join(desktop, f"{APP_NAME}.lnk"),
                          pyw, f'"{dash_py}"', app_dir))

    if args.uninstall:
        print(f"Removing shortcuts for {APP_NAME}...")
        ok = True
        for link_path, *_ in shortcuts:
            if dry:
                print(f"  [dry-run] would delete {link_path}")
                continue
            try:
                os.remove(link_path)
                print(f"  deleted {link_path}")
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"  could not delete {link_path}: {exc}")
                ok = False
        if not dry:
            try:
                os.rmdir(start_dir)
            except OSError:
                pass
        print("Done." if ok else "Done with some errors.")
        return

    print(f"Installing {APP_NAME} shortcuts...")
    if not dry:
        os.makedirs(start_dir, exist_ok=True)
    else:
        print(f"  [dry-run] would create directory {start_dir}")
    ok = True
    for link_path, target, arguments, workdir in shortcuts:
        print(f"  shortcut: {link_path}")
        ok &= run_ps(shortcut_ps(link_path, target, arguments, workdir), dry)
    print("Done." if ok else "Done with some errors.")
    if ok and not dry:
        print(f"Find it in the Start Menu under '{APP_NAME}'.")


if __name__ == "__main__":
    main()

"""Install and compile the MQL5 calendar service into the running terminal.

    python tools/build_sidecar.py                 # install + compile
    python tools/build_sidecar.py --data-path ... # when the terminal is closed
    python tools/build_sidecar.py --copy-only

Finding things is the whole job of this script: the MQL5 source has to live under
the terminal's own data folder for MetaEditor to compile it, the data folder is
only discoverable through the terminal (or a guess), and MetaEditor is a GUI
program that has to be driven headless with /compile. Each of those steps reports
what it found rather than assuming.
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "mql5" / "CalendarExport.mq5"
TARGET_DIR = pathlib.Path("MQL5") / "Services" / "mt5trader"
TARGET_NAME = "CalendarExport.mq5"

EDITOR_NAMES = ("MetaEditor64.exe", "metaeditor64.exe", "MetaEditor.exe")
EDITOR_GUESSES = (
    pathlib.Path(r"C:\Program Files\MetaTrader 5"),
    pathlib.Path(r"C:\Program Files (x86)\MetaTrader 5"),
)


def terminal_data_path():
    """Ask the running terminal. Returns (path, reason)."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None, "the MetaTrader5 python module is not installed"
    if not mt5.initialize():
        return None, "the terminal did not start (it may be closed)"
    try:
        info = mt5.terminal_info()
        if info is None:
            return None, "the terminal did not answer"
        return pathlib.Path(info.data_path), "from the running terminal"
    finally:
        mt5.shutdown()


def guess_data_path():
    """The usual place, when the terminal is closed and cannot be asked."""
    base = pathlib.Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal"
    if not base.is_dir():
        return None, "%s does not exist" % base
    candidates = [p for p in base.iterdir() if (p / "MQL5").is_dir()]
    if not candidates:
        return None, "no terminal data folder under %s" % base
    candidates.sort(key=lambda p: (p / "MQL5" / "Services").exists(), reverse=True)
    return candidates[0], "guessed, newest-looking of %d" % len(candidates)


def find_editor(terminal_path=None):
    for folder in filter(None, [pathlib.Path(terminal_path) if terminal_path else None,
                                *EDITOR_GUESSES]):
        for name in EDITOR_NAMES:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    found = shutil.which("metaeditor64.exe") or shutil.which("MetaEditor64.exe")
    return pathlib.Path(found) if found else None


def read_log(path):
    if not path.exists():
        return ""
    raw = path.read_bytes()
    for encoding in ("utf-16", "utf-16-le", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding).replace("\x00", "").strip()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


def parse_result(log_text):
    """(errors, warnings) from MetaEditor's own summary line.

    The exit code cannot be trusted for this: MetaEditor returns 1 on a clean
    compile as well as on a warning, so a build reported as broken because of the
    exit code is a false alarm, and one reported as fine because of it would be a
    lie. The log has a Result line; use it.
    """
    import re
    match = re.search(r"Result:\s*(\d+)\s*errors?,\s*(\d+)\s*warnings?", log_text or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def compile_editor(editor, target, timeout=180):
    log = target.with_suffix(".compile.log")
    if log.exists():
        log.unlink()
    command = [str(editor), "/compile:%s" % target, "/log:%s" % log]
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout,
                              text=True, errors="replace")
        exit_code = done.returncode
    except subprocess.TimeoutExpired:
        return {"compiled": False, "exit_code": None,
                "error": "MetaEditor did not finish within %ds" % timeout}
    except OSError as exc:
        return {"compiled": False, "exit_code": None,
                "error": "could not run MetaEditor: %s" % exc}
    text = read_log(log)
    errors, warnings = parse_result(text)
    produced = target.with_suffix(".ex5")
    return {
        "compiled": bool(errors == 0 and produced.is_file()),
        "exit_code": exit_code,
        "errors": errors,
        "warnings": warnings,
        "ex5": str(produced) if produced.is_file() else None,
        "ex5_bytes": produced.stat().st_size if produced.is_file() else None,
        "log": text[-4000:],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-path", help="terminal data folder, if you know it")
    parser.add_argument("--editor", help="path to MetaEditor64.exe")
    parser.add_argument("--copy-only", action="store_true", help="install, do not compile")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = {"source": str(SOURCE), "steps": []}
    if not SOURCE.is_file():
        print("no MQL5 source at %s" % SOURCE, file=sys.stderr)
        return 2

    data_path, reason = None, None
    if args.data_path:
        data_path, reason = pathlib.Path(args.data_path), "given on the command line"
    else:
        data_path, reason = terminal_data_path()
        if data_path is None:
            data_path, guessed = guess_data_path()
            reason = "%s; %s" % (reason, guessed)
    if data_path is None or not data_path.is_dir():
        print("could not find the terminal data folder: %s" % reason, file=sys.stderr)
        print("pass --data-path, for example:", file=sys.stderr)
        print(r'  --data-path "%APPDATA%\MetaQuotes\Terminal\<hash>"', file=sys.stderr)
        return 2
    report["data_path"] = str(data_path)
    report["data_path_from"] = reason
    report["steps"].append("data folder: %s (%s)" % (data_path, reason))

    target_dir = data_path / TARGET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / TARGET_NAME
    shutil.copyfile(SOURCE, target)
    report["installed"] = str(target)
    report["steps"].append("copied to %s" % target)

    if args.copy_only:
        report["compiled"] = False
        report["summary"] = ("installed but not compiled. Open MetaEditor, compile "
                             "%s, then start it in the terminal's Navigator under "
                             "Services." % target)
    else:
        editor = pathlib.Path(args.editor) if args.editor else find_editor()
        if editor is None:
            report["compiled"] = False
            report["summary"] = ("installed, but MetaEditor64.exe was not found. "
                                 "Compile %s by hand." % target)
        else:
            report["editor"] = str(editor)
            outcome = compile_editor(editor, target)
            report.update(outcome)
            report["steps"].append("compiled with %s: %s errors, %s warnings, exit %s"
                                   % (editor, outcome.get("errors"), outcome.get("warnings"),
                                      outcome.get("exit_code")))
            if outcome.get("compiled"):
                report["summary"] = (
                    "compiled (%s bytes of .ex5). In MetaTrader: View -> Navigator -> "
                    "Services -> CalendarExport -> right click -> Start. A service needs "
                    "no chart and no Algo Trading. The export lands in the terminal's "
                    "common data folder as Files\\mt5trader_calendar.json."
                    % outcome.get("ex5_bytes"))
            else:
                report["summary"] = (
                    "the build did not produce a working .ex5 (%s errors). Read the log "
                    "below. The source in mql5/ is unchanged."
                    % outcome.get("errors"))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for step in report["steps"]:
            print("  %s" % step)
        print()
        print(report["summary"])
        if report.get("log"):
            print("\n--- MetaEditor log (tail) ---")
            print(report["log"])
    return 0 if report.get("compiled") or args.copy_only else 1


if __name__ == "__main__":
    sys.exit(main())

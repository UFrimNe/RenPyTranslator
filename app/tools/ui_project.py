from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path: sys.path.insert(0, str(SRC))
from rpts.ui_project import ensure_overrides, import_runtime_ui, install_runtime_capture, remove_runtime_capture, write_ui_coverage


def main(argv=None):
    ap = argparse.ArgumentParser(description="RPTS Stage 7.7 UI tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coverage"); c.add_argument("database", type=Path); c.add_argument("--output", type=Path, default=Path("work/ui")); c.add_argument("--overrides", type=Path, default=Path("ui_overrides.json"))
    i = sub.add_parser("import-runtime"); i.add_argument("database", type=Path); i.add_argument("tsv", type=Path)
    e = sub.add_parser("enable-capture"); e.add_argument("database", type=Path)
    d = sub.add_parser("disable-capture"); d.add_argument("database", type=Path); d.add_argument("--remove-log", action="store_true")
    o = sub.add_parser("ensure-overrides"); o.add_argument("path", type=Path, nargs="?", default=Path("ui_overrides.json"))
    args = ap.parse_args(argv)
    if args.cmd == "coverage": result = write_ui_coverage(args.database, args.output, args.overrides)
    elif args.cmd == "import-runtime": result = import_runtime_ui(args.database, args.tsv)
    elif args.cmd == "enable-capture": result = install_runtime_capture(args.database)
    elif args.cmd == "disable-capture": result = remove_runtime_capture(args.database, remove_log=args.remove_log)
    else: result = {"overrides": str(ensure_overrides(args.path).resolve())}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__": raise SystemExit(main())

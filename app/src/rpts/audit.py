from __future__ import annotations
import argparse, json
from collections import Counter
from pathlib import Path
from .parser import RenPySourceParser
from .tokens import scan_protected_tokens

def audit_file(path: Path):
    raw = path.read_bytes()
    encoding_warning = None
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        source = raw.decode("utf-8-sig", errors="replace")
        encoding_warning = {"code": "encoding_replacement", "line": raw[:exc.start].count(b"\n") + 1,
                            "column": 0, "message": "Повреждён UTF-8: часть символов заменена при чтении файла."}
    parser = RenPySourceParser()
    items = parser.parse(source)
    if encoding_warning:
        parser.diagnostics.insert(0, encoding_warning)
    counts = Counter(x.kind for x in items)
    dynamic = [x for x in items if x.dynamic]
    token_counts = Counter()
    for x in items:
        if x.text:
            token_counts.update(t.kind for t in scan_protected_tokens(x.text))
    return {
        "path": str(path),
        "count": len(items),
        "kinds": dict(sorted(counts.items())),
        "dynamic": len(dynamic),
        "protected_tokens": dict(sorted(token_counts.items())),
        "items": [x.to_dict() for x in items],
        "warnings": list(parser.diagnostics),
    }

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)
    reports = [audit_file(Path(p)) for p in ns.paths]
    if ns.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for r in reports:
            print(r["path"])
            print("  candidates:", r["count"])
            print("  kinds:", r["kinds"])
            print("  dynamic:", r["dynamic"])
            print("  tokens:", r["protected_tokens"])

if __name__ == "__main__":
    main()

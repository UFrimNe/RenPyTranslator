from __future__ import annotations
import argparse
import csv
from pathlib import Path


def unescape_probe(value: str) -> str:
    # Reverse exactly the escaping emitted by runtime_probe.rpy.
    out = []
    i = 0
    while i < len(value):
        if value[i] == '\\' and i + 1 < len(value):
            n = value[i + 1]
            if n == 'n': out.append('\n'); i += 2; continue
            if n == 'r': out.append('\r'); i += 2; continue
            if n == 't': out.append('\t'); i += 2; continue
            if n == '\\': out.append('\\'); i += 2; continue
        out.append(value[i]); i += 1
    return ''.join(out)


def load_known(path: Path) -> set[str]:
    known = set()
    if not path.is_file():
        return known
    with path.open('r', encoding='utf-8-sig', newline='') as fh:
        for row in csv.DictReader(fh):
            text = row.get('text')
            if text:
                known.add(text)
    return known


def read_runtime(path: Path):
    rows = []
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open('r', encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            raw = raw.rstrip('\r\n')
            if not raw:
                continue
            parts = raw.split('\t', 2)
            if len(parts) != 3:
                continue
            filename, line, text = parts
            try: line_i = int(line)
            except Exception: line_i = 0
            rows.append({
                'filename': unescape_probe(filename),
                'line': line_i,
                'text': unescape_probe(text),
            })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description='Compare RPTS runtime probe log with the static exact-text inventory.')
    ap.add_argument('runtime_log')
    ap.add_argument('--reports', default='reports')
    ns = ap.parse_args(argv)
    reports = Path(ns.reports)
    reports.mkdir(parents=True, exist_ok=True)
    known = load_known(reports / 'runtime_known_inventory.csv')
    runtime = read_runtime(Path(ns.runtime_log))
    novel, covered = [], []
    seen = set()
    for row in runtime:
        key = (row['filename'], row['line'], row['text'])
        if key in seen: continue
        seen.add(key)
        out = dict(row)
        out['exact_static_match'] = row['text'] in known
        (covered if out['exact_static_match'] else novel).append(out)
    fields = ['filename', 'line', 'text', 'exact_static_match']
    for name, rows in [('runtime_novel.csv', novel), ('runtime_known.csv', covered)]:
        with (reports / name).open('w', encoding='utf-8-sig', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader(); w.writerows(rows)
    print(f'Runtime rows: {len(runtime)}')
    print(f'Exact-known: {len(covered)}')
    print(f'Novel candidates: {len(novel)}')
    print(f'Wrote: {reports / "runtime_novel.csv"}')


if __name__ == '__main__':
    main()

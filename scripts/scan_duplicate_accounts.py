#!/usr/bin/env python3
"""Scan an operator-controlled consistent SQLite copy; never initializes Flask."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.identity_scan import scan


def csv_safe(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value


def render(report, format):
    if format == 'json':
        return json.dumps(report, indent=2, ensure_ascii=True) + '\n'
    if format == 'csv':
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(['left', 'right', 'priority', 'ranking_version', 'generated_at', 'delete_by', 'evidence', 'data_counts'])
        for pair in report['pairs']:
            writer.writerow([csv_safe(pair['left']['id']), csv_safe(pair['right']['id']), pair['priority'],
                             report['ranking_version'], report['generated_at'], report['delete_by'],
                             json.dumps(pair['evidence'], ensure_ascii=True), json.dumps(pair['data_counts'], ensure_ascii=True)])
        return stream.getvalue()
    lines = [report['notice'], f"Scanned {report['user_count']} accounts; {len(report['pairs'])} candidate pairs.",
             f"Ranking v{report['ranking_version']}; generated {report['generated_at']}; delete by {report['delete_by']}"]
    lines.extend(report['warnings'])
    for pair in report['pairs']:
        lines.append(json.dumps(pair, ensure_ascii=True))
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--format', choices=('console', 'json', 'csv'), default='console')
    parser.add_argument('--output', type=Path, help='New private report file; existing files are never overwritten')
    args = parser.parse_args(argv)
    try:
        source = args.database.resolve()
        if args.output and args.output.resolve() in {source, *(Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))}:
            raise ValueError('Output cannot be a database or its sidecar')
        report = scan(args.database)
        content = render(report, args.format)
        if args.output:
            # Exclusive creation prevents replacing the database, sidecars or existing reports.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                stream.write(content)
        else:
            sys.stdout.write(content)
        print(f"Admin-only report; delete within 30 days. {len(report['pairs'])} pairs; "
              f"{report['invalid_email_count']} invalid emails; {report['skipped_device_rows']} skipped device rows.", file=sys.stderr)
        for warning in report['warnings']:
            print(warning, file=sys.stderr)
        return 0
    except (OSError, sqlite3.Error, ValueError):
        print('Scan failed: check database schema/read permissions and use a new output path. No account changes made.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

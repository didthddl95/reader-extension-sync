#!/usr/bin/env python3
"""Merge public extension feeds into a private checkout. Never execute feed code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, unquote, urlsplit

ORIGIN = 'https://dc-manhwa-29bc3f.espr.page'
FEEDS = [ORIGIN + '/mangayomi/index.json', ORIGIN + '/mangayomi/extra/index.json']
MANIFEST = '.extension-sync.json'


def encoded(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def asset_path(url):
    p = urlsplit(url)
    path = unquote(p.path)
    if (p.scheme != 'https' or p.netloc != urlsplit(ORIGIN).netloc or
            p.fragment or not path.startswith('/mangayomi/') or
            any(s in ('', '.', '..') for s in path[1:].split('/')) or
            re.search(r'[\\\x00-\x20]', path)):
        raise ValueError('Unexpected upstream asset URL')
    return 'upstream/' + path.removeprefix('/mangayomi/')


def download(url):
    # No credentials are ever used for upstream downloads. No redirects.
    p = subprocess.run(['curl', '--fail', '--silent', '--show-error',
                        '--proto', '=https', '--max-time', '35', '--retry', '2',
                        '--max-filesize', '8388608', '--write-out', '\n%{http_code}', url],
                       capture_output=True)
    if p.returncode or not p.stdout.endswith(b'\n200'):
        raise ValueError('Upstream download failed; previous repository remains unchanged')
    data = p.stdout[:-4]
    if not data:
        raise ValueError('Empty upstream asset')
    return data


def raw_url(project, path):
    if not re.fullmatch(r'[1-9][0-9]*', project):
        raise ValueError('Invalid GitLab project ID')
    return f'https://gitlab.com/api/v4/projects/{project}/repository/files/{quote(path, safe="")}/raw?ref=main'


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+){1,3}', value):
        raise ValueError('Unsupported upstream version format')
    parts = [int(n) for n in value.split('.')]
    return tuple(parts + [0] * (4 - len(parts)))


def plan(root, project, fetch=download, validate_js=True):
    current = json.loads((root / 'index.json').read_text())
    previous = json.loads((root / MANIFEST).read_text()) if (root / MANIFEST).exists() else {'entries': {}, 'files': []}
    managed = previous['entries']
    if not isinstance(current, list):
        raise ValueError('Invalid destination index')
    custom = [row for row in current if str(row['id']) not in managed]
    custom_ids = {row['id'] for row in custom}
    rows, entries, assets, seen = [], {}, {}, set()
    for feed in FEEDS:
        source_rows = json.loads(fetch(feed))
        if not isinstance(source_rows, list) or not source_rows:
            raise ValueError('Upstream index must be a nonempty list')
        for source in source_rows:
            if (not isinstance(source, dict) or type(source.get('id')) is not int or
                    not isinstance(source.get('name'), str) or not source['name'] or
                    not isinstance(source.get('iconUrl'), str)):
                raise ValueError('Malformed upstream entry')
            sid = str(source['id'])
            if sid in seen or source['id'] in custom_ids:
                raise ValueError('Duplicate ID or custom extension conflict')
            seen.add(sid)
            version_key(source['version'])
            path = asset_path(source['sourceCodeUrl'])
            if not path.endswith('.js'):
                raise ValueError('Expected JavaScript extension')
            if path not in assets:
                assets[path] = fetch(source['sourceCodeUrl'])
            content_hash = digest(encoded(source) + assets[path])
            old = managed.get(sid)
            revision = 0
            if old:
                if version_key(source['version']) < version_key(old['upstream_version']):
                    raise ValueError('Upstream downgrade refused')
                if source['version'] == old['upstream_version']:
                    revision = old['revision'] + int(content_hash != old['digest'])
            effective = source['version'] + (f'.{revision}' if revision else '')
            version_key(effective)
            row = dict(source)
            row.update(sourceCodeUrl=raw_url(project, path), version=effective,
                       apiUrl=f'https://gitlab.com/api/v4/projects/{project}')
            # Icons stay on the original public host; image widgets don't use repo authentication.
            rows.append(row)
            entries[sid] = {'upstream_version': source['version'], 'revision': revision,
                            'digest': content_hash}
    if validate_js:
        with tempfile.TemporaryDirectory() as tmp:
            for n, data in enumerate(assets.values()):
                p = Path(tmp) / f'{n}.js'
                p.write_bytes(data)
                check = subprocess.run(['node', '--check', str(p)], capture_output=True)
                if check.returncode:
                    raise ValueError('JavaScript syntax validation failed')
    result = dict(assets)
    result['index.json'] = encoded(custom + rows)
    result[MANIFEST] = encoded({'schema': 1, 'entries': entries, 'files': sorted(assets)})
    deletes = set(previous['files']) - set(assets)
    if any(not p.startswith('upstream/') or '..' in p.split('/') for p in deletes):
        raise ValueError('Invalid previous manifest path')
    return result, deletes, len(rows), len(custom)


def apply(root, files, deletes):
    for rel in set(files) | deletes:
        target = (root / rel).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError('Destination path escapes checkout')
    for rel in deletes:
        (root / rel).unlink(missing_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('checkout', type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    files, deletes, count, custom = plan(args.checkout, args.project)
    changed = sum(not (args.checkout / p).exists() or (args.checkout / p).read_bytes() != data
                  for p, data in files.items()) + len(deletes)
    if args.apply:
        apply(args.checkout, files, deletes)
    print(f'Validated {count} upstream entries; preserved {custom} custom entries; {changed} changed files.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Logs are public. Never print remote responses, file contents or secrets.
        print('Sync failed. No update was pushed. Review inputs and credentials privately.', file=sys.stderr)
        sys.exit(1)

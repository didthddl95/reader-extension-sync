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


def effective_version(source, old, content_hash):
    if not old:
        return source['version']
    previous = old.get('effective_version') or old['upstream_version'] + (
        f".{old['revision']}" if old.get('revision') else '')
    if content_hash == old['digest']:
        return previous
    if version_key(source['version']) > version_key(previous):
        return source['version']
    # Reissues and version resets still need to update an installed extension.
    parts = list(version_key(previous))
    parts[-1] += 1
    return '.'.join(map(str, parts))


def source_identity_bridge(remaps):
    # Each extension runs in its own MProvider runtime. Keep upstream ID-based
    # selectors working while preferences and library records use the local ID.
    return ('''// Repository-local IDs; the adapter still sees its original source ID.
(() => {
  const ids = ''' + json.dumps(remaps, sort_keys=True) + ''';
  const descriptor = Object.getOwnPropertyDescriptor(MProvider.prototype, 'source');
  if (!descriptor || typeof descriptor.get !== 'function') throw Error('Source identity unavailable');
  Object.defineProperty(MProvider.prototype, 'source', {
    configurable: true,
    get() {
      const source = descriptor.get.call(this);
      return Object.prototype.hasOwnProperty.call(ids, source.id)
        ? Object.assign({}, source, {id: ids[source.id]}) : source;
    }
  });
})();
''').encode()


def plan(root, project, fetch=download, validate_js=True):
    current = json.loads((root / 'index.json').read_text())
    previous = json.loads((root / MANIFEST).read_text()) if (root / MANIFEST).exists() else {'entries': {}, 'files': []}
    managed = previous['entries']
    if not isinstance(current, list) or len({r['id'] for r in current}) != len(current):
        raise ValueError('Invalid destination index')
    custom = [row for row in current if str(row['id']) not in managed]
    custom_ids = {row['id'] for row in custom}
    # Keep source identities and version history across upstream removals.
    identities = dict(previous.get('id_map', {sid: int(sid) for sid in managed}))
    history = dict(previous.get('retired_entries', {}))
    history.update(managed)
    if (any(not str(k).isdigit() or type(v) is not int or v <= 0 or v > 2**53 - 1
            for k, v in identities.items()) or len(set(identities.values())) != len(identities)):
        raise ValueError('Invalid source identity map')
    upstream, seen = [], set()
    for feed in FEEDS:
        source_rows = json.loads(fetch(feed))
        if not isinstance(source_rows, list):
            raise ValueError('Upstream index must be a list')
        for source in source_rows:
            if (not isinstance(source, dict) or type(source.get('id')) is not int or
                    not 0 < source['id'] <= 2**53 - 1 or
                    not isinstance(source.get('name'), str) or not source['name'] or
                    not isinstance(source.get('iconUrl'), str)):
                raise ValueError('Malformed upstream entry')
            sid = str(source['id'])
            if sid in seen:
                raise ValueError('Duplicate upstream ID')
            seen.add(sid)
            version_key(source['version'])
            upstream.append(source)
    occupied = custom_ids | {r['id'] for r in upstream} | set(identities.values())
    next_id = 8_000_000_000
    for source in upstream:
        sid = str(source['id'])
        assigned = identities.get(sid)
        if assigned is None:
            if source['id'] not in custom_ids and source['id'] not in identities.values():
                assigned = source['id']
            else:
                while next_id in occupied:
                    next_id += 1
                assigned = next_id
                occupied.add(assigned)
            identities[sid] = assigned
        if assigned in custom_ids:
            raise ValueError('Previously assigned identity now conflicts with custom extension')
    rows, entries, assets, remaps = [], {}, {}, {}
    for source in upstream:
        sid = str(source['id'])
        local_id = identities[sid]
        path = asset_path(source['sourceCodeUrl'])
        if not path.endswith('.js'):
            raise ValueError('Expected JavaScript extension')
        if path not in assets:
            assets[path] = fetch(source['sourceCodeUrl'])
        if local_id != source['id']:
            remaps.setdefault(path, {})[str(local_id)] = source['id']
    for path, mapping in remaps.items():
        assets[path] += b'\n;\n' + source_identity_bridge(mapping)
    for source in upstream:
        sid = str(source['id'])
        local_id = identities[sid]
        path = asset_path(source['sourceCodeUrl'])
        content_hash = digest(encoded(source) + assets[path])
        old = history.get(str(local_id))
        effective = effective_version(source, old, content_hash)
        version_key(effective)
        row = dict(source)
        row.update(id=local_id, sourceCodeUrl=raw_url(project, path), version=effective,
                   apiUrl=f'https://gitlab.com/api/v4/projects/{project}')
        rows.append(row)
        entries[str(local_id)] = {'upstream_id': source['id'],
                                 'upstream_version': source['version'],
                                 'effective_version': effective, 'digest': content_hash}
    # Preserve each removed source's last listing and executable bytes. If an
    # active sibling now uses newer code at the same URL, snapshot the old code
    # separately rather than silently changing the retained adapter.
    for existing in current:
        local_id = str(existing['id'])
        if local_id not in managed or local_id in entries:
            continue
        prefix = f'https://gitlab.com/api/v4/projects/{project}/repository/files/'
        url = existing.get('sourceCodeUrl', '')
        if not url.startswith(prefix):
            raise ValueError('Invalid retained asset URL')
        path = unquote(urlsplit(url).path.split('/repository/files/', 1)[1].removesuffix('/raw'))
        if (path not in previous['files'] or not path.startswith('upstream/') or
                not path.endswith('.js') or '..' in path.split('/') or
                raw_url(project, path) != url):
            raise ValueError('Invalid retained asset path')
        stored = (root / path).resolve()
        if not stored.is_relative_to(root.resolve()):
            raise ValueError('Retained asset escapes checkout')
        code = stored.read_bytes()
        target = path
        if target in assets and assets[target] != code:
            target = f'upstream/retained/{local_id}.js'
            if target in assets and assets[target] != code:
                raise ValueError('Retained asset path collision')
        assets[target] = code
        row = dict(existing, sourceCodeUrl=raw_url(project, target))
        rows.append(row)
        entries[local_id] = dict(managed[local_id], retained=True)
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
    result[MANIFEST] = encoded({'schema': 3, 'entries': entries, 'files': sorted(assets),
                               'id_map': identities,
                               'retired_entries': {k: v for k, v in history.items() if k not in entries}})
    deletes = set(previous['files']) - set(assets)
    if any(not p.startswith('upstream/') or '..' in p.split('/') for p in deletes):
        raise ValueError('Invalid previous manifest path')
    return result, deletes, len(upstream), len(custom)


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
    retained = sum(bool(v.get('retained')) for v in json.loads(files[MANIFEST])['entries'].values())
    print(f'Validated {count} upstream entries; preserved {custom} custom and {retained} retained entries; {changed} changed files.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Logs are public. Never print remote responses, file contents or secrets.
        print('Sync failed. No update was pushed. Review inputs and credentials privately.', file=sys.stderr)
        sys.exit(1)

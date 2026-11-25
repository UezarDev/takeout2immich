#!/usr/bin/env python3
"""takeout2immich: import one or more Google Photos Takeouts into Immich, deduplicated.

Two stages, so the heavy sorting can happen wherever the zips are and the upload wherever Immich is reachable:

  prep    ZIP... --out DIR [--limit N]
          Reads every Takeout zip (English or Spanish folder names), pairs each photo/video with its JSON
          sidecar, and stores every unique file once (SHA-1) under
            DIR/library/normal/<year>/   timeline photos and videos (albums kept in the manifest)
            DIR/library/locked/<year>/   anything that was in a Locked Folder (goes to Immich's Locked Folder)
            DIR/library/trash/<year>/    only-in-Trash items (never uploaded)
          and writes DIR/manifest.json: per file its SHA-1, Google taken time, GPS, description, favourite,
          archived, albums, and every place it was found. A file found in a Locked Folder anywhere is locked;
          a file in Trash that also appears elsewhere counts as that other place.
          The zips are only read. --limit N takes only the first N media of each zip (for a trial run).

  upload  DIR --server URL (--key-file FILE | env IMMICH_API_KEY) [--dry-run] [--limit N]
          Uploads normal + locked files (skips trash), sets favourite/archived/locked, creates albums and fills
          them. Resumable: progress is kept in DIR/state.json, and Immich's checksum check turns an already
          uploaded file into a "duplicate" that is simply reused. --limit N uploads a small mixed sample.

  fixup   DIR --server URL (--key-file FILE | env IMMICH_API_KEY)
          After Immich has finished extracting metadata: adds Google's GPS where the file has none, and
          Google's description where Immich has none.

  status  DIR
          Counts from the manifest and the upload state.

Standard library only; uploads go through curl so multi-GB videos stream from disk.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

__version__ = '1.0.0'

MEDIA_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.avif', '.bmp', '.tif', '.tiff',
             '.cr2', '.cr3', '.dng', '.nef', '.arw', '.orf', '.rw2', '.raf',
             '.mp4', '.mov', '.m4v', '.3gp', '.avi', '.mkv', '.mts', '.m2ts', '.webm', '.wmv', '.mpg', '.mpeg'}
LOCKED_DIRS = {'locked folder', 'carpeta bloqueada'}
TRASH_DIRS = {'trash', 'bin', 'papelera'}
ALBUM_META = {'metadata.json', 'metadatos.json'}
SIDECAR = re.compile(r'^(?P<base>.+?)\.(?P<tag>s[a-z-]*)(?P<n>\(\d+\))?\.json$', re.I)
EDITED = re.compile(r'-(edited|editado)$', re.I)
ILLEGAL = re.compile(r'[<>:"\\|?*\x00-\x1f]')
RANK = {'locked': 3, 'normal': 2, 'trash': 1}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------- prep

def zip_name(info):
    """Entry name with UTF-8 restored when the zip did not flag it (zipfile then decodes as cp437)."""
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode('cp437').decode('utf-8')
        except UnicodeError:
            pass
    return name


def split_takeout(name):
    """'Takeout/Google Photos/Photos from 2022/x.jpg' -> ('Photos from 2022', 'x.jpg'), or None."""
    parts = name.split('/')
    if len(parts) < 4 or parts[0] != 'Takeout':
        return None
    return '/'.join(parts[2:-1]), parts[-1]


def sidecar_target(json_name):
    """Media name a sidecar describes, or None. 'IMG.jpg.supplemental-metadata(1).json' -> 'IMG(1).jpg'.

    Handles Google's truncated suffixes ('.supplemental-metad.json', '.suppl.json', ...) and the '(n)' counter
    that Takeout moves from the media name to the end of the sidecar name."""
    m = SIDECAR.match(json_name)
    if not m or not 'supplemental-metadata'.startswith(m.group('tag').lower()):
        return None
    base, n = m.group('base'), m.group('n') or ''
    if n:
        stem, ext = os.path.splitext(base)
        base = f'{stem}{n}{ext}'
    return base


def match_sidecars(media, jsons):
    """media: file names in one folder; jsons: {sidecar name: parsed JSON}. Returns {media name: parsed}."""
    by_target = {}
    for jn, data in jsons.items():
        t = sidecar_target(jn)
        if t:
            by_target[t] = data
    out = {}
    for name in media:
        stem, ext = os.path.splitext(name)
        unedited = EDITED.sub('', stem) + ext
        if name in by_target:
            out[name] = by_target[name]
        elif unedited in by_target:  # 'IMG-edited.jpg' shares the original's sidecar
            out[name] = by_target[unedited]
        else:
            pref = [t for t in by_target if len(t) >= 20 and name.startswith(t)]  # truncated long names
            titled = [d for d in jsons.values() if d.get('title') in (name, unedited)]
            if len(pref) == 1:
                out[name] = by_target[pref[0]]
            elif len(titled) == 1:
                out[name] = titled[0]
    return out


def meta_from_sidecar(d):
    if not d:
        return {}
    m = {}
    ts = (d.get('photoTakenTime') or {}).get('timestamp') or (d.get('creationTime') or {}).get('timestamp')
    if ts:
        m['taken'] = int(ts)
    for key in ('geoData', 'geoDataExif'):
        g = d.get(key) or {}
        if g.get('latitude') or g.get('longitude'):
            m['lat'], m['lon'] = g['latitude'], g['longitude']
            break
    if d.get('description'):
        m['description'] = d['description']
    if d.get('favorited'):
        m['favorite'] = True
    if d.get('archived'):
        m['archived'] = True
    return m


def folder_kind(folder, is_album):
    """(kind, album name or None) for a Takeout folder."""
    top = folder.split('/')[0]
    if top.lower() in LOCKED_DIRS:
        return 'locked', None
    if top.lower() in TRASH_DIRS:
        return 'trash', None
    # only folders with an album metadata.json are albums; year folders, Archive, Failed Videos are not
    return 'normal', (top if is_album else None)


def scan_zip(z, account, album_titles):
    folders = {}  # folder -> {'media': [(name, info)], 'json': {name: data}}
    for info in z.infolist():
        if info.is_dir():
            continue
        sp = split_takeout(zip_name(info))
        if not sp:
            continue
        folder, name = sp
        f = folders.setdefault(folder, {'media': [], 'json': {}})
        ext = os.path.splitext(name)[1].lower()
        if ext == '.json':
            try:
                data = json.loads(z.read(info).decode('utf-8'))
            except ValueError:
                continue
            if name.lower() in ALBUM_META:
                album_titles[(account, folder)] = data.get('title') or folder.split('/')[0]
            elif isinstance(data, dict):
                f['json'][name] = data
        elif ext in MEDIA_EXT:
            f['media'].append((name, info))
    return folders


def cmd_prep(a):
    out = Path(a.out)
    if (out / 'manifest.json').exists():
        sys.exit(f'{out / "manifest.json"} exists: this folder was already prepared (use a new --out)')
    pool = out / 'library' / '.pool'
    pool.mkdir(parents=True, exist_ok=True)
    assets, album_titles = {}, {}
    for zi, zpath in enumerate(a.zips, 1):
        account = f'takeout{zi}'
        with zipfile.ZipFile(zpath) as z:
            folders = scan_zip(z, account, album_titles)
            total = sum(len(f['media']) for f in folders.values())
            log(f'{zpath}: {total} media files')
            stats = {'media': 0, 'new': 0, 'duplicate': 0, 'no_sidecar': 0}
            for folder in sorted(folders):
                f = folders[folder]
                kind, album = folder_kind(folder, (account, folder) in album_titles)
                if album:
                    album = album_titles[(account, folder)]
                sidecars = match_sidecars([n for n, _ in f['media']], f['json'])
                for name, info in f['media']:
                    if a.limit and stats['media'] >= a.limit:
                        break
                    stats['media'] += 1
                    meta = meta_from_sidecar(sidecars.get(name))
                    if not meta:
                        stats['no_sidecar'] += 1
                    sha, tmp = copy_hashed(z, info, pool)
                    ext = os.path.splitext(name)[1].lower()
                    e = assets.get(sha)
                    if e is None:
                        os.replace(tmp, pool / f'{sha}{ext}')
                        stats['new'] += 1
                        mtime = dt.datetime(*info.date_time, tzinfo=dt.timezone.utc).timestamp()
                        e = assets[sha] = {'sha1': sha, 'name': name, 'ext': ext, 'size': info.file_size,
                                           'kind': kind, 'albums': [], 'sources': [], 'zip_mtime': int(mtime)}
                    else:
                        os.remove(tmp)
                        stats['duplicate'] += 1
                        if RANK[kind] > RANK[e['kind']]:
                            e['kind'] = kind
                    e['sources'].append(f'{account}:{folder}/{name}')
                    for k, v in meta.items():
                        if k in ('favorite', 'archived'):
                            e[k] = e.get(k, False) or v
                        else:
                            e.setdefault(k, v)
                    if album and kind == 'normal' and album not in e['albums']:
                        e['albums'].append(album)
                    if stats['media'] % 500 == 0:
                        log(f'  {stats["media"]}/{total} ...')
            log(f'{zpath}: {stats}')
    place(out, pool, assets)
    manifest = {'tool': f'takeout2immich {__version__}', 'created': dt.datetime.now(dt.timezone.utc).isoformat(),
                'zips': [os.path.basename(p) for p in a.zips], 'limit': a.limit,
                'assets': sorted(assets.values(), key=lambda e: e['path'])}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding='utf-8')
    print_status(out)


def copy_hashed(z, info, pool):
    h = hashlib.sha1()
    fd, tmp = tempfile.mkstemp(dir=pool, suffix='.part')
    with z.open(info) as src, os.fdopen(fd, 'wb') as dst:
        while True:
            chunk = src.read(4 << 20)
            if not chunk:
                break
            h.update(chunk)
            dst.write(chunk)
    return h.hexdigest(), tmp


def place(out, pool, assets):
    """Move every unique file from the pool to library/<kind>/<year>/<name>."""
    used = set()
    for e in sorted(assets.values(), key=lambda e: (e['kind'], e['name'], e['sha1'])):
        if e['kind'] == 'locked':
            e['albums'] = []  # Immich keeps locked items out of albums
        year = time.gmtime(e.get('taken', e['zip_mtime'])).tm_year
        name = ILLEGAL.sub('_', e['name']).rstrip(' .') or 'file'
        rel = f'library/{e["kind"]}/{year}/{name}'
        if rel.lower() in used:
            stem, ext = os.path.splitext(name)
            rel = f'library/{e["kind"]}/{year}/{stem}~{e["sha1"][:8]}{ext}'
        used.add(rel.lower())
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        os.replace(pool / f'{e["sha1"]}{e["ext"]}', out / rel)
        e['path'] = rel
    pool.rmdir()


# ---------------------------------------------------------------- Immich API

class Immich:
    def __init__(self, server, key):
        self.base = server.rstrip('/') + '/api'
        self.key = key

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'x-api-key': self.key, 'Accept': 'application/json',
                                              'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
        except urllib.error.HTTPError as err:
            raise RuntimeError(f'{method} {path}: HTTP {err.code} {err.read()[:300]!r}') from None
        return json.loads(raw) if raw else None

    def upload(self, path, e, visibility):
        taken = iso(e.get('taken', e['zip_mtime']))
        # the API key reaches curl through a config on stdin, so it never appears in the process list
        cmd = ['curl', '-sS', '--fail-with-body', '-K', '-', '-X', 'POST', self.base + '/assets',
               '-H', 'Accept: application/json',
               '-H', f'x-immich-checksum: {e["sha1"]}',
               '--form-string', f'deviceAssetId=takeout2immich-{e["sha1"]}',
               '--form-string', 'deviceId=takeout2immich',
               '--form-string', f'fileCreatedAt={taken}',
               '--form-string', f'fileModifiedAt={taken}',
               '--form-string', f'isFavorite={"true" if e.get("favorite") else "false"}',
               '--form-string', f'visibility={visibility}',
               '-F', f'assetData=@{path};filename={curl_safe(e["name"])}']
        config = 'header = "x-api-key: ' + self.key + '"\n'
        r = subprocess.run(cmd, input=config, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f'upload {path}: {r.stdout[:300]} {r.stderr[:300]}')
        return json.loads(r.stdout)


def curl_safe(name):
    return re.sub(r'[;,"\\]', '_', name)


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def load_json(p, default):
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default


def save_json(p, data):
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, p)


def client(a):
    key = Path(a.key_file).read_text().strip() if a.key_file else os.environ.get('IMMICH_API_KEY', '').strip()
    if not key:
        sys.exit('no API key: pass --key-file FILE or set IMMICH_API_KEY')
    return Immich(a.server, key)


def sample(todo, n):
    """A small mixed trial set: locked, album, favourite/archived, GPS and plain files."""
    picks, seen = [], set()
    for pick in (lambda e: e['kind'] == 'locked',
                 lambda e: e['albums'],
                 lambda e: e['kind'] == 'normal' and (e.get('favorite') or e.get('archived')),
                 lambda e: e['kind'] == 'normal' and 'lat' in e,
                 lambda e: e['kind'] == 'normal'):
        for e in [e for e in todo if pick(e) and e['sha1'] not in seen][:max(1, n // 5)]:
            picks.append(e)
            seen.add(e['sha1'])
    return picks[:n]


def cmd_upload(a):
    out = Path(a.dir)
    manifest = load_json(out / 'manifest.json', None)
    if manifest is None:
        sys.exit(f'{out / "manifest.json"} not found: run prep first')
    state = load_json(out / 'state.json', {'assets': {}, 'albums': {}})
    todo = [e for e in manifest['assets'] if e['kind'] != 'trash']
    if a.limit:
        todo = sample(todo, a.limit)
    left = [e for e in todo if not state['assets'].get(e['sha1'], {}).get('done')]
    if a.dry_run:
        log(f'dry run: {len(todo)} to import, {len(left)} not yet done, '
            f'{sum(e["size"] for e in left) / 1e9:.2f} GB to send')
        return
    im = client(a)
    me = im.call('GET', '/users/me')
    log(f'importing {len(left)} files as {me.get("name")!r} into {a.server}')
    last_save, errors = time.time(), 0
    for i, e in enumerate(left, 1):
        vis = 'locked' if e['kind'] == 'locked' else 'archive' if e.get('archived') else 'timeline'
        try:
            res = im.upload(out / e['path'], e, vis)
            st = {'id': res['id'], 'status': res.get('status'), 'done': True}
            if res.get('status') == 'duplicate':
                # already in Immich (earlier run or uploaded before): bring its flags in line
                body = {'ids': [res['id']], 'visibility': vis}
                if e.get('favorite'):
                    body['isFavorite'] = True
                try:
                    im.call('PUT', '/assets', body)
                except RuntimeError as err:
                    log(f'note: could not update flags of existing {e["path"]}: {err}')
        except RuntimeError as err:
            errors += 1
            log(f'ERROR {e["path"]}: {err}')
            st = {'error': str(err)[:500]}
        state['assets'][e['sha1']] = st
        if time.time() - last_save > 10:
            save_json(out / 'state.json', state)
            last_save = time.time()
            log(f'{i}/{len(left)} uploaded, {errors} errors')
    save_json(out / 'state.json', state)
    sync_albums(im, todo, state)
    save_json(out / 'state.json', state)
    done = sum(1 for s in state['assets'].values() if s.get('done'))
    log(f'upload finished: {done} done in total, {errors} errors this run')
    if errors:
        sys.exit(1)


def sync_albums(im, todo, state):
    albums = {}
    for e in todo:
        st = state['assets'].get(e['sha1'], {})
        for name in e['albums']:
            if st.get('id'):
                albums.setdefault(name, []).append(st['id'])
    existing = {al['albumName']: al['id'] for al in im.call('GET', '/albums')}
    for name, ids in sorted(albums.items()):
        aid = state['albums'].get(name) or existing.get(name)
        if not aid:
            aid = im.call('POST', '/albums', {'albumName': name})['id']
        state['albums'][name] = aid
        for k in range(0, len(ids), 500):
            im.call('PUT', f'/albums/{aid}/assets', {'ids': ids[k:k + 500]})
        log(f'album {name!r}: {len(ids)} items')


def cmd_fixup(a):
    out = Path(a.dir)
    manifest = load_json(out / 'manifest.json', None)
    state = load_json(out / 'state.json', {'assets': {}})
    im = client(a)
    fixed = pending = 0
    for e in manifest['assets']:
        st = state['assets'].get(e['sha1'], {})
        if e['kind'] == 'locked':
            continue  # locked assets are not readable with an API key; Immich keeps their own EXIF
        if not st.get('id') or st.get('fixed') or not ('lat' in e or 'description' in e):
            continue
        exif = im.call('GET', f'/assets/{st["id"]}').get('exifInfo') or {}
        if not exif:
            pending += 1  # metadata extraction not done yet
            continue
        body = {}
        if 'lat' in e and exif.get('latitude') is None:
            body['latitude'], body['longitude'] = e['lat'], e['lon']
        if e.get('description') and not exif.get('description'):
            body['description'] = e['description']
        if body:
            im.call('PUT', f'/assets/{st["id"]}', body)
            fixed += 1
        st['fixed'] = True
    save_json(out / 'state.json', state)
    log(f'fixup: {fixed} assets updated, {pending} not extracted yet' + (' (run fixup again later)' if pending else ''))


def print_status(out):
    manifest = load_json(out / 'manifest.json', None)
    if manifest is None:
        sys.exit(f'{out / "manifest.json"} not found')
    state = load_json(out / 'state.json', {'assets': {}})
    by = {}
    for e in manifest['assets']:
        k = by.setdefault(e['kind'], [0, 0, 0])
        k[0] += 1
        k[1] += e['size']
        k[2] += len(e['sources']) - 1
    for kind, (n, size, dups) in sorted(by.items()):
        print(f'{kind:7} {n:7} unique files  {size / 1e9:8.2f} GB  ({dups} duplicate copies merged)')
    albums = {}
    for e in manifest['assets']:
        for al in e['albums']:
            albums[al] = albums.get(al, 0) + 1
    print('albums:', ', '.join(f'{k} ({v})' for k, v in sorted(albums.items())) or 'none')
    print('with Google GPS:', sum('lat' in e for e in manifest['assets']),
          ' favourites:', sum(bool(e.get('favorite')) for e in manifest['assets']),
          ' archived:', sum(bool(e.get('archived')) for e in manifest['assets']),
          ' no sidecar:', sum('taken' not in e for e in manifest['assets']))
    if state['assets']:
        done = sum(1 for s in state['assets'].values() if s.get('done'))
        err = sum(1 for s in state['assets'].values() if 'error' in s)
        print(f'upload state: {done} imported, {err} failed')


def main(argv=None):
    p = argparse.ArgumentParser(prog='takeout2immich', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('prep', help='sort and deduplicate Takeout zips into a folder')
    s.add_argument('zips', nargs='+')
    s.add_argument('--out', required=True)
    s.add_argument('--limit', type=int, default=0, help='trial: only the first N media of each zip')
    for name, text in (('upload', 'upload a prepared folder to Immich'),
                       ('fixup', 'add Google GPS/descriptions after Immich extracted metadata')):
        s = sub.add_parser(name, help=text)
        s.add_argument('dir')
        s.add_argument('--server', default='http://localhost:2283', help='Immich URL (default %(default)s)')
        s.add_argument('--key-file', help='file holding an Immich API key (or set IMMICH_API_KEY)')
        if name == 'upload':
            s.add_argument('--dry-run', action='store_true')
            s.add_argument('--limit', type=int, default=0, help='trial: upload only a small mixed sample')
    s = sub.add_parser('status', help='show counts for a prepared folder')
    s.add_argument('dir')
    a = p.parse_args(argv)
    if a.cmd == 'prep':
        cmd_prep(a)
    elif a.cmd == 'upload':
        cmd_upload(a)
    elif a.cmd == 'fixup':
        cmd_fixup(a)
    else:
        print_status(Path(a.dir))


if __name__ == '__main__':
    main()

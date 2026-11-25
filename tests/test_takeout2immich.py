"""Tests: build small synthetic Takeout zips, prep them, and upload to a fake Immich server."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import takeout2immich as t2i  # noqa: E402


def sidecar(title, ts, lat=0.0, **extra):
    d = {'title': title, 'photoTakenTime': {'timestamp': str(ts)},
         'geoData': {'latitude': lat, 'longitude': lat}}
    d.update(extra)
    return json.dumps(d)


def make_zip(path, root, files):
    with zipfile.ZipFile(path, 'w') as z:
        for name, data in files.items():
            z.writestr(f'Takeout/{root}/{name}', data)


class SidecarNames(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(t2i.sidecar_target('IMG_1.jpg.supplemental-metadata.json'), 'IMG_1.jpg')
        self.assertEqual(t2i.sidecar_target('IMG_1.jpg.supplemental-metadata(1).json'), 'IMG_1(1).jpg')
        self.assertEqual(t2i.sidecar_target('IMG_1.jpg.suppl.json'), 'IMG_1.jpg')
        self.assertIsNone(t2i.sidecar_target('metadata.json'))
        self.assertIsNone(t2i.sidecar_target('print-subscriptions.json'))

    def test_edited_shares_original(self):
        js = {'a.jpg.supplemental-metadata.json': {'title': 'a.jpg', 'photoTakenTime': {'timestamp': '1'}}}
        got = t2i.match_sidecars(['a.jpg', 'a-edited.jpg'], js)
        self.assertEqual(set(got), {'a.jpg', 'a-edited.jpg'})


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # account 1: English layout
        make_zip(self.tmp / 'a.zip', 'Google Photos', {
            'Photos from 2022/one.jpg': b'ONE',
            'Photos from 2022/one.jpg.supplemental-metadata.json': sidecar('one.jpg', 1650000000, 10.5, favorited=True),
            'Photos from 2022/shared.jpg': b'SHARED',
            'Photos from 2022/shared.jpg.supplemental-metadata.json': sidecar('shared.jpg', 1650000100),
            'Trip/metadata.json': json.dumps({'title': 'Trip'}),
            'Trip/shared.jpg': b'SHARED',
            'Trip/shared.jpg.supplemental-metadata.json': sidecar('shared.jpg', 1650000100),
            'Locked Folder/secret.mp4': b'SECRET',
            'Locked Folder/secret.mp4.supplemental-metadata.json': sidecar('secret.mp4', 1700000000),
            'Trash/gone.jpg': b'GONE',
        })
        # account 2: Spanish layout, with a cross-account duplicate and an archived file
        make_zip(self.tmp / 'b.zip', 'Google Fotos', {
            'Fotos del 2022/shared.jpg': b'SHARED',
            'Fotos del 2022/shared.jpg.supplemental-metadata.json': sidecar('shared.jpg', 1650000100),
            'Fotos del 2023/two(1).jpg': b'TWO',
            'Fotos del 2023/two.jpg.supplemental-metadata(1).json': sidecar('two.jpg', 1680000000),
            'Archivo/old.jpg': b'OLD',
            'Archivo/old.jpg.supplemental-metadata.json': sidecar('old.jpg', 1600000000, archived=True),
            'Favs/metadatos.json': json.dumps({'title': 'Favs'}),
            'Favs/two(1).jpg': b'TWO',
        })
        self.out = self.tmp / 'out'
        with contextlib.redirect_stdout(io.StringIO()):
            t2i.main(['prep', str(self.tmp / 'a.zip'), str(self.tmp / 'b.zip'), '--out', str(self.out)])
        self.assets = {e['name']: e for e in json.loads((self.out / 'manifest.json').read_text('utf-8'))['assets']}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_prep_dedupes_and_sorts(self):
        self.assertEqual(sorted(self.assets), ['gone.jpg', 'old.jpg', 'one.jpg', 'secret.mp4', 'shared.jpg', 'two(1).jpg'])
        self.assertEqual(len(self.assets['shared.jpg']['sources']), 3)
        self.assertEqual(self.assets['shared.jpg']['albums'], ['Trip'])
        self.assertEqual(self.assets['two(1).jpg']['albums'], ['Favs'])
        self.assertEqual(self.assets['secret.mp4']['kind'], 'locked')
        self.assertEqual(self.assets['gone.jpg']['kind'], 'trash')
        self.assertTrue(self.assets['old.jpg']['archived'])
        self.assertEqual(self.assets['old.jpg']['albums'], [])  # "Archivo" is not an album
        self.assertTrue(self.assets['one.jpg']['favorite'])
        self.assertEqual(self.assets['one.jpg']['lat'], 10.5)
        self.assertEqual(self.assets['two(1).jpg']['taken'], 1680000000)  # matched via the (1) sidecar
        for e in self.assets.values():
            self.assertTrue((self.out / e['path']).is_file(), e['path'])
        self.assertFalse((self.out / 'library' / '.pool').exists())

    @unittest.skipUnless(shutil.which('curl'), 'curl not installed')
    def test_upload_against_fake_immich(self):
        server = FakeImmich()
        key = self.tmp / 'key'
        key.write_text('test-key')
        args = ['upload', str(self.out), '--server', server.url, '--key-file', str(key)]
        with contextlib.redirect_stdout(io.StringIO()):
            t2i.main(args)
            t2i.main(args)  # second run is a no-op
        server.stop()
        uploads = server.uploads
        self.assertEqual(len(uploads), 5)  # trash skipped, each unique file once
        vis = {u['filename']: u['visibility'] for u in uploads}
        self.assertEqual(vis['secret.mp4'], 'locked')
        self.assertEqual(vis['old.jpg'], 'archive')
        self.assertEqual(vis['one.jpg'], 'timeline')
        self.assertEqual(sorted(server.albums), ['Favs', 'Trip'])
        self.assertTrue(all(u['key'] == 'test-key' for u in uploads))


class FakeImmich:
    def __init__(self):
        self.uploads, self.albums = [], {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.reply({'name': 'tester'} if self.path == '/api/users/me' else [])

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/api/assets':
                    fields = parse_multipart(body, self.headers['Content-Type'])
                    fields['key'] = self.headers['x-api-key']
                    outer.uploads.append(fields)
                    self.reply({'id': f'id{len(outer.uploads)}', 'status': 'created'})
                elif self.path == '/api/albums':
                    name = json.loads(body)['albumName']
                    outer.albums[name] = []
                    self.reply({'id': name})

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.startswith('/api/albums/'):
                    outer.albums[self.path.split('/')[3]].extend(body['ids'])
                self.reply([])

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


def parse_multipart(body, ctype):
    boundary = ctype.split('boundary=')[1].encode()
    fields = {}
    for part in body.split(b'--' + boundary):
        head, _, value = part.partition(b'\r\n\r\n')
        head = head.decode(errors='replace')
        if 'name="' not in head:
            continue
        name = head.split('name="')[1].split('"')[0]
        if 'filename="' in head:
            fields['filename'] = head.split('filename="')[1].split('"')[0]
        else:
            fields[name] = value.rstrip(b'\r\n').decode()
    return fields


if __name__ == '__main__':
    unittest.main()

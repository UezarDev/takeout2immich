# takeout2immich

[![tests](https://github.com/UezarDev/takeout2immich/actions/workflows/tests.yml/badge.svg)](https://github.com/UezarDev/takeout2immich/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

takeout2immich moves a Google Photos library into [Immich](https://immich.app) from one or more
[Google Takeout](https://takeout.google.com) exports, and imports each file only once.

It is one Python file with no dependencies beyond the standard library and `curl`. It was built for a common
case: a family that exports several Google accounts and wants all of it in Immich, without duplicates and
without losing the Locked Folder.

## What it does

| Google Photos | In Immich |
| --- | --- |
| Photos and videos | Timeline, with Google's taken date, GPS and description |
| The same file in several Takeouts or albums | One asset (exact SHA-1 match) |
| Albums | Albums. An album that appears in two Takeouts becomes one album |
| Favourites | Favourites |
| Archived items | Archive |
| Locked Folder | Locked Folder (`visibility=locked`) |
| Trash | Sorted aside and never uploaded |

It handles the Takeout naming quirks: `.supplemental-metadata.json` sidecars (including truncated ones),
the `(1)` counter that Google moves to the end of the sidecar name, `-edited` copies that share the
original's sidecar, zips without the UTF-8 flag, and Takeouts exported in Spanish (`Google Fotos`,
`Fotos del 2022`, `metadatos.json`).

## How it works

The import has two stages, so the heavy sorting can run where the zips are, and the upload can run where
Immich is.

```
 Takeout zips ──prep──▶ import folder ──upload──▶ Immich ──fixup──▶ GPS and descriptions
                        ├─ library/normal/<year>/
                        ├─ library/locked/<year>/
                        ├─ library/trash/<year>/
                        ├─ manifest.json
                        └─ state.json   (written by upload, makes it resumable)
```

1. **prep** reads every zip and matches each file to its JSON sidecar. It hashes the file and writes each
   unique file once under `library/`. It also records `manifest.json`, which lists the metadata, albums and
   every original location of each file. The zips are only read, never changed.
2. **upload** sends the normal and locked files to Immich with the right visibility and favourite flag,
   then creates and fills the albums. It saves progress to `state.json`, so you can stop it and run it again
   at any time. Immich's checksum check turns a file it already has into a no-op.
3. **fixup** runs after Immich has finished its metadata jobs. It adds Google's GPS to files that have none
   of their own, and Google's description where Immich has none. Dates need no fixup: each upload sends
   Google's taken time as the file date, and Immich uses that date whenever the file has no EXIF date.

## Requirements

- Python 3.8 or newer
- `curl` (preinstalled on Linux, macOS and Windows 10+)
- An Immich server, recent v1.13x or any v2/v3, and an **API key** for the account to import into:
  in Immich, go to *Account Settings → API Keys → New API Key*, with all permissions.
- Free disk space for the `library/` folder. It needs about the size of the zips, or less after
  deduplication. Immich needs the same amount again for the originals.

## Usage

```bash
git clone https://github.com/UezarDev/takeout2immich.git
cd takeout2immich
```

### 1. Prep

```bash
python3 takeout2immich.py prep /path/to/takeout-*.zip --out /path/to/import
python3 takeout2immich.py status /path/to/import
```

Takeouts that are split into several zips (`-001.zip`, `-002.zip`, ...) can all be passed at once, along
with the zips of other accounts. Add `--limit 50` for a quick trial: it takes only the first 50 media of
each zip, and it needs a different `--out` from the real run.

### 2. Upload

Save the API key in a file that only you can read, for example `chmod 600 ~/immich.key`. You can also set
`IMMICH_API_KEY` instead of passing `--key-file`.

```bash
# what would be sent
python3 takeout2immich.py upload /path/to/import --server http://localhost:2283 --key-file ~/immich.key --dry-run

# a 20-file trial (a mix of locked, album, favourite and plain files), then check it in Immich
python3 takeout2immich.py upload /path/to/import --server http://localhost:2283 --key-file ~/immich.key --limit 20

# everything; safe to interrupt and rerun
python3 takeout2immich.py upload /path/to/import --server http://localhost:2283 --key-file ~/immich.key
```

For a long upload over SSH, run it under `nohup ... > upload.log 2>&1 &`, or in `tmux` or `screen`.

### 3. Fixup

Wait until *Administration → Jobs* in Immich shows no active metadata extraction, then run:

```bash
python3 takeout2immich.py fixup /path/to/import --server http://localhost:2283 --key-file ~/immich.key
```

If it reports items that have not been extracted yet, run it again later.

## Good to know

- **Locked Folder:** you need your Immich PIN code to see those items. Set one under *Account Settings*.
  Immich keeps locked items out of albums, so takeout2immich does too.
- **Duplicates** means byte-identical files. A re-compressed copy of the same photo counts as a different
  file. Immich's own *Utilities → Duplicates* view can find those afterwards.
- **Storage quota:** check that the account's quota fits the total that `status` prints before you upload.
- **Privacy:** the API key is passed to `curl` through stdin, so it never appears in the process list.
  `manifest.json` and `state.json` hold file names and GPS positions, so keep the import folder private.
- **Cleanup:** once the import has been checked in Immich, the import folder can be deleted. Keep the
  original zips as your backup.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests build small synthetic Takeouts (English and Spanish, with duplicates, albums, Locked Folder,
Archive and Trash), prep them, and upload them to a fake Immich server.

## License

[MIT](LICENSE)

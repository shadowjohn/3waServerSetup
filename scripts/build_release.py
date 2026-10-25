#!/usr/bin/env python3
"""Build a deterministic release from an explicit source and locked wheel allowlist."""
import argparse
import base64
from datetime import date
import email.parser
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile


REPOSITORY = 'shadowjohn/3waServerSetup'
PRODUCT = '3waServerSetting'
VERSION = re.compile(r'v(([0-9]{4})\.([0-9]{2})\.([0-9]{2})\.([0-9]{3})(?:-rc\.[1-9][0-9]*)?)')
MAX_ARCHIVE = 64*1024*1024
MAX_PAYLOAD = 128*1024*1024


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2)+'\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def release_version(value):
    match = VERSION.fullmatch(value) if isinstance(value,str) else None
    if not match or match[5]=='000':
        raise ValueError('Use vYYYY.MM.DD.NNN with a valid date and sequence 001..999.')
    try:
        date(int(match[2]),int(match[3]),int(match[4]))
    except ValueError:
        raise ValueError('The release tag contains an invalid calendar date.') from None
    return match[1]


def read_source(root, name):
    path = root/name
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise ValueError('Source symlinks are not release inputs: '+name)
        if item == root:
            break
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Missing or oversized release input: '+name)
    data = path.read_bytes()
    if name.endswith('.png'):
        return data
    if data.startswith(b'\xef\xbb\xbf'):
        raise ValueError('Release text inputs must be UTF-8 without BOM: '+name)
    data.decode('utf-8')
    # Match the repository's text=auto/eol=lf tree, including Windows checkout inputs.
    return data.replace(b'\r\n',b'\n')


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def locked_wheels(source, wheelhouse):
    records = {}
    for line in read_source(source, 'requirements-tui.lock').decode('utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9.!+_-]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)', line)
        if not match or canonical(match[1]) in records:
            raise ValueError('Release dependencies must be individually pinned with SHA256 hashes.')
        records[canonical(match[1])] = (match[2], set(re.findall(r'--hash=sha256:([0-9a-f]{64})', match[3])))
    if not records:
        raise ValueError('An empty dependency lock cannot build a runtime release.')
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ValueError('A local locked wheelhouse is required.')
    result, found = {}, set()
    for path in sorted(wheelhouse.iterdir()):
        if path.is_symlink() or not path.is_file() or not path.name.endswith('-py3-none-any.whl'):
            raise ValueError('Only reviewed universal Python wheels belong in this release.')
        parts = path.name[:-4].split('-')
        if len(parts) not in {5, 6}:
            raise ValueError('Unknown wheel filename.')
        name, version = canonical(parts[0]), parts[1]
        if name not in records or version != records[name][0] or name in found:
            raise ValueError('Unexpected, duplicate or unlocked dependency wheel.')
        raw = path.read_bytes()
        if len(raw) > MAX_ARCHIVE or digest(raw) not in records[name][1]:
            raise ValueError('Dependency wheel does not match its pinned SHA256.')
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
                metadata = [item for item in wheel.namelist() if item.endswith('.dist-info/METADATA')]
                if len(metadata) != 1:
                    raise ValueError('Unknown wheel metadata.')
                fields = email.parser.Parser().parsestr(wheel.read(metadata[0]).decode('utf-8'))
                if canonical(fields.get('Name', '')) != name or fields.get('Version') != version:
                    raise ValueError('Wheel metadata disagrees with its lock entry.')
        except (zipfile.BadZipFile, UnicodeError, KeyError):
            raise ValueError('Dependency wheel is invalid.') from None
        result['wheelhouse/'+path.name] = raw
        found.add(name)
    if found != set(records):
        raise ValueError('Not every locked runtime dependency has a wheel.')
    return result


def signer_text(source):
    value = read_source(source, 'release/allowed_signers').decode('ascii')
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError('One fixed release signer is required.')
    parts = lines[0].split()
    if len(parts) < 3 or parts[:2] != ['3waServerSetup-release', 'ssh-ed25519']:
        raise ValueError('The release signer identity or key type is invalid.')
    try:
        wire = base64.b64decode(parts[2], validate=True)
    except ValueError:
        raise ValueError('The release public key is malformed.') from None
    if len(wire) != 51 or not wire.startswith(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'):
        raise ValueError('The release public key is not Ed25519.')
    return lines[0]+'\n'


def build_release(*, source, wheelhouse, output, version, commit, validated_platforms=None):
    source, wheelhouse, output = Path(source).absolute(), Path(wheelhouse).absolute(), Path(output).absolute()
    parsed_version = release_version(version)
    if not isinstance(commit,str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Use a full lowercase commit SHA.')
    requested_platforms = ['ubuntu26.04'] if validated_platforms is None else validated_platforms
    if not isinstance(requested_platforms,(list,tuple)) or any(not isinstance(p,str) for p in requested_platforms):
        raise ValueError('Validated platforms must be an explicit list.')
    platforms = sorted(set(requested_platforms))
    if not platforms or any(p not in {'ubuntu24.04', 'ubuntu26.04'} for p in platforms):
        raise ValueError('Only actually validated Ubuntu platforms may be advertised.')
    key = signer_text(source)
    identity = {'schema':1, 'product':PRODUCT, 'repository':REPOSITORY, 'version':parsed_version,
                'tag':version, 'commit':commit, 'data_schema':1, 'python_requires':'>=3.12',
                'validated_platforms':platforms}
    fixed = ['README.md','LICENSE','run.sh','nat.sh','firewall_plan.py','requirements-tui.txt',
             'requirements-tui.lock','tests/sqlite-smoke.php','release/allowed_signers']
    paths = set(fixed)
    for folder, patterns in [('server_setting', ['*.py']), ('config', ['*.example.json','*.example.conf']),
                             ('docs', ['*.md']), ('docs/previews', ['*.png','*.svg'])]:
        directory = source/folder
        if directory.is_symlink():
            raise ValueError('Source directory symlinks are not release inputs.')
        for pattern in patterns:
            paths.update(p.relative_to(source).as_posix() for p in directory.glob(pattern))
    required = {'server_setting/__init__.py','server_setting/release_update.py'}
    if not required <= paths:
        raise ValueError('The runtime and standalone update entry must be present.')
    payload = {name:read_source(source,name) for name in sorted(paths)}
    # The same pinned key bytes must be embedded in the bootstrap and payload.
    payload['release/allowed_signers'] = key.encode('ascii')
    payload.update(locked_wheels(source, wheelhouse))
    payload['RELEASE.json'] = encoded(identity)
    if len(payload) > 2048 or sum(map(len, payload.values())) > MAX_PAYLOAD:
        raise ValueError('The release payload exceeds its fixed bound.')
    template = read_source(source, 'scripts/install.sh.in').decode('utf-8')
    verifier = read_source(source, 'scripts/bootstrap_verify.py').decode('utf-8')
    if template.count('@ALLOWED_SIGNERS@') != 1 or template.count('@BOOTSTRAP_PYTHON@') != 1:
        raise ValueError('Unknown bootstrap template placeholders.')
    installer = template.replace('@ALLOWED_SIGNERS@', key.rstrip('\n')).replace('@BOOTSTRAP_PYTHON@',verifier.rstrip('\n')).encode('utf-8')
    files = {name:{'sha256':digest(raw),'size':len(raw),'mode':0o755 if name in {'run.sh','nat.sh'} else 0o644}
             for name,raw in sorted(payload.items())}
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed,mode='wb',filename='',mtime=0) as zipper:
        with tarfile.open(fileobj=zipper,mode='w|',format=tarfile.USTAR_FORMAT) as archive:
            for name, raw in sorted(payload.items()):
                item = tarfile.TarInfo(PRODUCT+'/'+name)
                item.size, item.mode, item.uid, item.gid, item.mtime = len(raw),files[name]['mode'],0,0,0
                archive.addfile(item,io.BytesIO(raw))
    archive_data = compressed.getvalue()
    if len(archive_data) > MAX_ARCHIVE:
        raise ValueError('The compressed release exceeds its bound.')
    archive_name = PRODUCT+'-'+version+'.tar.gz'
    manifest = dict(identity, archive={'name':archive_name,'sha256':digest(archive_data),'size':len(archive_data)},
                    installer={'name':'install.sh','sha256':digest(installer),'size':len(installer)},files=files)
    assets = {archive_name:archive_data,'release-manifest.json':encoded(manifest),'install.sh':installer}
    assets['SHA256SUMS'] = ''.join(digest(raw)+'  '+name+'\n' for name,raw in sorted(assets.items())).encode('ascii')
    output.mkdir(parents=True,exist_ok=True)
    if output.is_symlink():
        raise ValueError('Output directory must not be a symlink.')
    if any(p.name not in set(assets)|{'release-manifest.json.sig'} or not p.is_file() or p.is_symlink() for p in output.iterdir()):
        raise ValueError('The output directory contains unrelated files; use a new release directory.')
    for name,raw in assets.items():
        destination = output/name
        if destination.is_symlink():
            raise ValueError('An output asset path is a symlink.')
        destination.write_bytes(raw)
        destination.chmod(0o755 if name=='install.sh' else 0o644)
    return {'archive':str(output/archive_name),'manifest':str(output/'release-manifest.json'),
            'version':parsed_version,'commit':commit,'files':len(files),'archive_sha256':digest(archive_data)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--wheelhouse',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--validated-platform',dest='validated_platforms',action='append',choices=('ubuntu24.04','ubuntu26.04'))
    args = vars(parser.parse_args(argv))
    try:
        result = build_release(**args)
    except (ValueError,OSError,UnicodeError) as error:
        parser.exit(1,'Release build refused: '+str(error)+'\n')
    print(json.dumps(result,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

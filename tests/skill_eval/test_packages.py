import io
import stat
import zipfile

import pytest

from backend.skill_eval.packages import Limits, parse_package, export_package
from backend.skill_eval.storage import Store


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, (data, mode) in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buf.getvalue()


def test_whole_package_roundtrip_keeps_bytes_paths_modes(tmp_path, skill_files):
    store = Store(tmp_path)
    package = parse_package('sample.zip', zip_bytes({'root/'+p: v for p,v in skill_files.items()}), store)
    assert package.name == 'sample'
    assert package.version == '0.0.1'
    assert {e.path: (store.read_blob(e.hash), e.mode) for e in package.entries} == skill_files
    zipped = export_package(package.entries, store, {'name':'sample','version':'0.0.1'})
    again = parse_package('sample.zip', zipped, store)
    assert again.content_hash == package.content_hash
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        assert b'octagon.skill' in archive.comment
        assert archive.read('assets/data.bin') == b'\x00\xff\x00'


@pytest.mark.parametrize('name,data', [
    ('../evil',b'x'),('/evil',b'x'),('a\\evil',b'x'),
    ('scripts',b'x'),
])
def test_bad_paths_and_file_directory_conflicts(tmp_path, skill_files, name, data):
    files = dict(skill_files)
    files[name] = (data,0o644)
    with pytest.raises(ValueError):
        parse_package('a.zip',zip_bytes(files),Store(tmp_path))


@pytest.mark.parametrize('body', [b'\xff',b'no frontmatter',b'---\nname: [a]\n---\nx',b'---\nname: a\nname: b\n---\nx',b'---\nname: &a a\nversion: *a\n---\nx'])
def test_bad_body_rejected_without_success_manifest(tmp_path, body):
    with pytest.raises(ValueError):
        parse_package('SKILL.md',body,Store(tmp_path))
    assert not list(tmp_path.rglob('manifests/*.json'))


def test_symlink_duplicate_and_multiple_roots(tmp_path,skill_files):
    for scenario in ('symlink','duplicate','roots'):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('SKILL.md',skill_files['SKILL.md'][0])
            if scenario=='symlink':
                info=zipfile.ZipInfo('link');info.external_attr=(stat.S_IFLNK|0o777)<<16
                z.writestr(info,'/etc/passwd')
            elif scenario=='duplicate':
                z.writestr('e\u0301.txt','1');z.writestr('é.txt','2')
            else:
                z.writestr('second/SKILL.md',skill_files['SKILL.md'][0])
        with pytest.raises(ValueError):
            parse_package('a.zip',buf.getvalue(),Store(tmp_path))


def test_limits_apply_to_single_body_and_archive(tmp_path,skill_files):
    cases=[Limits(max_upload=10),Limits(max_file=10),Limits(max_total=10),Limits(max_files=1)]
    for limits in cases:
        with pytest.raises(ValueError,match='limit'):
            parse_package('a.zip',zip_bytes(skill_files),Store(tmp_path),limits)
    with pytest.raises(ValueError,match='limit'):
        parse_package('SKILL.md',skill_files['SKILL.md'][0],Store(tmp_path),Limits(max_file=10))
    bomb=dict(skill_files);bomb['big.txt']=(b'0'*100000,0o644)
    with pytest.raises(ValueError,match='ratio'):
        parse_package('a.zip',zip_bytes(bomb),Store(tmp_path),Limits(max_ratio=2))


def test_version_disagreement_is_visible(tmp_path):
    package=parse_package('SKILL.md',b'---\nname: a\nversion: 1\nmetadata:\n  version: 2\n---\nx',Store(tmp_path))
    assert package.version is None
    assert package.version_candidates == ['2','1']


@pytest.mark.parametrize('files',[
    {'readme.txt':(b'no skill',0o644)},
    {'root/SKILL.md':(b'---\nname: a\n---\nx',0o644),'other.txt':(b'x',0o644)},
    {'root/nested/SKILL.md':(b'---\nname: a\n---\nx',0o644)},
])
def test_missing_or_ambiguous_root_is_rejected(tmp_path,files):
    with pytest.raises(ValueError):parse_package('a.zip',zip_bytes(files),Store(tmp_path))



def test_encrypted_and_corrupt_crc_are_rejected_before_any_manifest(tmp_path,skill_files):
    raw=bytearray(zip_bytes({'SKILL.md':skill_files['SKILL.md']}))
    for signature,offset in ((b'PK\x03\x04',6),(b'PK\x01\x02',8)):
        position=raw.index(signature)+offset;raw[position]|=1
    with pytest.raises(ValueError,match='encrypted'):parse_package('encrypted.zip',bytes(raw),Store(tmp_path/'encrypted'))
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_STORED) as z:z.writestr('SKILL.md',skill_files['SKILL.md'][0])
    corrupt=bytearray(output.getvalue());body_start=30+len('SKILL.md');corrupt[body_start]^=1
    with pytest.raises(ValueError,match='invalid ZIP'):parse_package('corrupt.zip',bytes(corrupt),Store(tmp_path/'crc'))
    assert not list(tmp_path.rglob('manifests/*.json'))


def test_explicit_directories_are_allowed_but_file_directory_collision_is_not(tmp_path,skill_files):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:
        z.writestr('root/','');z.writestr('root/scripts/','')
        z.writestr('root/SKILL.md',skill_files['SKILL.md'][0]);z.writestr('root/scripts/run.sh','echo okay')
    assert len(parse_package('valid.zip',output.getvalue(),Store(tmp_path/'valid')).entries)==2
    with zipfile.ZipFile(output,'a') as z:z.writestr('root/scripts','conflicting file')
    with pytest.raises(ValueError):parse_package('invalid.zip',output.getvalue(),Store(tmp_path/'invalid'))

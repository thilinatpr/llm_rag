"""Publishing the index + vectors as a pair: whatever fails, the live pair must stay consistent. Offline (a fake S3)."""
import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from billrag.store import Store

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("upload_r2", ROOT / "scripts" / "upload_r2.py")
up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(up)


class NotFound(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "404"}}


class FakeS3:
    class exceptions:
        ClientError = NotFound

    def __init__(self, live=None):
        self.o = dict(live or {})
        self.fail_upload = {}  # staging file name -> failures left
        self.fail_copy_to = set()  # keys a copy into always fails
        self.corrupt = set()  # staged file names stored wrongly
        self.calls = []

    def upload_file(self, path, bucket, key, Config=None, ExtraArgs=None):
        name = key.rsplit("/", 1)[1]
        self.calls.append(("upload", key))
        if self.fail_upload.get(name, 0) > 0:
            self.fail_upload[name] -= 1
            raise ConnectionError("Connection was closed")
        self.o[key] = b"bad" if name in self.corrupt else Path(path).read_bytes()

    def download_file(self, bucket, key, path):
        Path(path).write_bytes(self.o[key])

    def head_object(self, Bucket, Key):
        if Key not in self.o:
            raise NotFound()
        return {"ContentLength": len(self.o[Key])}

    def copy_object(self, Bucket, Key, CopySource):
        self.calls.append(("copy", Key))
        if Key in self.fail_copy_to:
            raise ConnectionError("copy failed")
        self.o[Key] = self.o[CopySource["Key"]]

    def delete_object(self, Bucket, Key):
        self.o.pop(Key, None)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda _s: None)


@pytest.fixture
def pair(tmp_path):
    s = Store(tmp_path / "index.sqlite")
    s.replace_bill({"key": "001/2026", "year": 2026, "date": "2026-01-01T00", "title": "T", "kind": "Bill"},
                   [{"kind": "section", "section_no": 1, "text": "x", "ctx": "x"}])
    s.commit()
    np.savez(tmp_path / "vectors.npz", ids=np.array([1]), vectors=np.zeros((1, 2)), fingerprint=np.asarray(s.fingerprint()))
    s.db.close()
    return tmp_path


OLD = {"bills/index.sqlite": b"old index", "bills/vectors.npz": b"old vectors"}


def live(s3):
    return {k: s3.o[k] for k in OLD if k in s3.o}


def test_publish_swaps_the_pair_keeps_a_backup_and_clears_staging(pair):
    s3 = FakeS3(OLD)
    up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert s3.o["bills/index.sqlite"] == (pair / "index.sqlite").read_bytes()
    assert s3.o["bills/vectors.npz"] == (pair / "vectors.npz").read_bytes()
    assert s3.o["backups/bills/T/index.sqlite"] == b"old index" and s3.o["backups/bills/T/vectors.npz"] == b"old vectors"
    assert not [k for k in s3.o if ".staging" in k]
    assert not [c for c in s3.calls if c[0] == "upload" and ".staging" not in c[1]]  # live keys only ever change by copy


def test_the_failure_seen_in_production_leaves_the_live_pair_alone(pair):
    s3 = FakeS3(OLD)
    s3.fail_upload["index.sqlite"] = 99  # the vectors would succeed; the index never does
    with pytest.raises(up.PublishError, match="not touched"):
        up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert live(s3) == OLD


def test_one_lost_connection_is_retried(pair):
    s3 = FakeS3(OLD)
    s3.fail_upload["index.sqlite"] = 1
    up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert s3.o["bills/index.sqlite"] == (pair / "index.sqlite").read_bytes()


def test_a_corrupt_staged_copy_never_goes_live(pair):
    s3 = FakeS3(OLD)
    s3.corrupt.add("vectors.npz")
    with pytest.raises(up.PublishError, match="different checksum"):
        up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert live(s3) == OLD


def test_a_failed_second_copy_puts_the_first_back(pair):
    s3 = FakeS3(OLD)
    s3.fail_copy_to.add("bills/vectors.npz")
    with pytest.raises(up.PublishError, match="previous pair was put back"):
        up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert live(s3) == OLD


def test_a_failed_rollback_says_the_pair_is_mismatched_and_how_to_fix_it(pair):
    s3 = FakeS3(OLD)
    real = s3.copy_object

    def copy(Bucket, Key, CopySource):  # the index swap works, then both the vectors swap and the way back fail
        if Key == "bills/vectors.npz" or (Key == "bills/index.sqlite" and "backups" in CopySource["Key"]):
            raise ConnectionError("down")
        real(Bucket, Key, CopySource)

    s3.copy_object = copy
    with pytest.raises(up.PublishError, match=r"MISMATCHED.*backups/bills/T/"):
        up.publish_index(s3, "b", "bills", pair, stamp="T")


def test_a_failed_backup_changes_nothing(pair):
    s3 = FakeS3(OLD)
    s3.fail_copy_to.add("backups/bills/T/vectors.npz")
    with pytest.raises(up.PublishError, match="not touched"):
        up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert live(s3) == OLD


def test_a_pair_that_could_not_serve_is_refused_before_any_network_call(pair):
    z = dict(np.load(pair / "vectors.npz"))
    np.savez(pair / "vectors.npz", **{**z, "fingerprint": np.asarray("stale")})
    s3 = FakeS3(OLD)
    with pytest.raises(up.PublishError, match="python -m billrag embed"):
        up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert s3.calls == []
    os.remove(pair / "vectors.npz")
    with pytest.raises(up.PublishError, match="missing"):
        up.publish_index(s3, "b", "bills", pair, stamp="T")


def test_first_publish_with_nothing_live(pair):
    s3 = FakeS3()
    up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert set(live(s3)) == set(OLD) and not [k for k in s3.o if k.startswith("backups/")]


def test_a_failed_first_publish_leaves_nothing_live(pair):
    s3 = FakeS3()
    s3.fail_copy_to.add("bills/vectors.npz")
    with pytest.raises(up.PublishError):
        up.publish_index(s3, "b", "bills", pair, stamp="T")
    assert live(s3) == {}


def _fetch_module():
    spec = importlib.util.spec_from_file_location("fetch_index", ROOT / "scripts" / "fetch_index.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _other_pair(tmp_path, title):
    d = tmp_path / title
    d.mkdir()
    s = Store(d / "index.sqlite")
    s.replace_bill({"key": "001/2026", "year": 2026, "date": "2026-01-01T00", "title": title, "kind": "Bill"},
                   [{"kind": "section", "section_no": 1, "text": title, "ctx": title}])
    s.commit()
    np.savez(d / "vectors.npz", ids=np.array([1]), vectors=np.zeros((1, 2)), fingerprint=np.asarray(s.fingerprint()))
    s.db.close()
    return {f"bills/{n}": (d / n).read_bytes() for n in up.INDEX_FILES}


def test_a_restore_that_overlaps_a_publish_fetches_again(tmp_path):
    fetch = _fetch_module()
    old, new = _other_pair(tmp_path, "old"), _other_pair(tmp_path, "new")
    s3 = FakeS3(old)
    real = s3.download_file

    def download(bucket, key, path):  # the publish lands right after the old index was read
        real(bucket, key, path)
        if key.endswith("index.sqlite") and s3.o["bills/index.sqlite"] == old["bills/index.sqlite"]:
            s3.o.update(new)

    s3.download_file = download
    root = tmp_path / "restore"
    root.mkdir()
    fetch.fetch_pair(s3, "b", "bills", root, False, wait=0)
    assert (root / "index.sqlite").read_bytes() == new["bills/index.sqlite"]
    assert (root / "vectors.npz").read_bytes() == new["bills/vectors.npz"]


def test_a_restore_of_a_mismatch_that_stays_fails_loudly(tmp_path):
    fetch = _fetch_module()
    a, b = _other_pair(tmp_path, "a"), _other_pair(tmp_path, "b")
    s3 = FakeS3({"bills/index.sqlite": a["bills/index.sqlite"], "bills/vectors.npz": b["bills/vectors.npz"]})
    root = tmp_path / "restore"
    root.mkdir()
    with pytest.raises(SystemExit, match="do not match"):
        fetch.fetch_pair(s3, "b", "bills", root, False, wait=0)


def test_existing_files_are_not_fetched_or_checked_again(tmp_path):
    fetch = _fetch_module()
    root = tmp_path / "restore"
    root.mkdir()
    (root / "index.sqlite").write_bytes(b"x")
    (root / "vectors.npz").write_bytes(b"y")
    s3 = FakeS3()
    fetch.fetch_pair(s3, "b", "bills", root, False, wait=0)  # a corrupt pair here would raise if it were checked
    assert s3.calls == []

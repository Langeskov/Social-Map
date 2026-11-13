"""Tiny generated fixtures only; media storage has no database dependency."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import multiprocessing
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from PIL import Image
import pytest

from app.domain.errors import MediaValidationError
from app.storage import media_store as module
from app.storage.media_store import MediaStore


@pytest.fixture
def store(tmp_path):
    return MediaStore(tmp_path / "data")


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "tiny.png"
    Image.new("RGB", (3, 2), (20, 40, 60)).save(path)
    return path


def _publish(store, workspace, staged):
    with store.content_lock(workspace, staged.sha256):
        return store.publish(workspace, staged)


def _process_publish(data_dir, source, workspace, start, ready, results, active, maximum):
    try:
        store = MediaStore(Path(data_dir))
        staged = store.stage(Path(source))
        ready.put(True)
        if not start.wait(15):
            raise RuntimeError("start timeout")
        try:
            with store.content_lock(workspace, staged.sha256):
                with active.get_lock():
                    active.value += 1
                    maximum.value = max(maximum.value, active.value)
                time.sleep(0.04)
                receipt = store.publish(workspace, staged)
                with active.get_lock():
                    active.value -= 1
                results.put((receipt.created_new, receipt.storage_path))
        finally:
            store.discard(staged)
    except Exception as exc:
        import traceback
        results.put(("ERROR", type(exc).__name__, traceback.format_exc()))


def _crash_with_lock(data_dir, workspace, digest, connection):
    store = MediaStore(Path(data_dir))
    with store.content_lock(workspace, digest):
        connection.send("locked")
        connection.close()
        os._exit(0)


def test_stage_hash_dimensions_actual_mime_and_original_unchanged(store, image):
    original = image.read_bytes()
    disguised = image.with_suffix(".bin")
    disguised.write_bytes(original)
    staged = store.stage(disguised)
    assert staged.path.parent == store.staging_dir
    assert staged.sha256 == hashlib.sha256(original).hexdigest()
    assert staged.size_bytes == len(original)
    assert staged.mime_type == "image/png"
    assert (staged.width, staged.height) == (3, 2)
    assert staged.path.read_bytes() == original == image.read_bytes()
    store.discard(staged)
    store.discard(staged)  # idempotent
    assert not staged.path.exists()


def test_nonimage_is_binary(store, tmp_path):
    source = tmp_path / "note.dat"
    source.write_bytes(b"ordinary nonimage content")
    staged = store.stage(source)
    assert (staged.mime_type, staged.width, staged.height) == ("application/octet-stream", None, None)
    store.discard(staged)


@pytest.mark.parametrize("content,name", [(b"", "empty.dat"), (b"not an image", "fake.PNG"),
    (b"\x89PNG\r\n\x1a\n", "truncated.bin"), (b"\xff\xd8\xff\xe0", "truncated.jpg")])
def test_invalid_files_clean_staging_and_errors_are_generic(store, tmp_path, content, name):
    source = tmp_path / name
    source.write_bytes(content)
    with pytest.raises(MediaValidationError) as caught:
        store.stage(source)
    assert str(source) not in str(caught.value)
    assert name not in str(caught.value)
    assert caught.value.__suppress_context__
    assert list(store.staging_dir.iterdir()) == []


def test_corrupt_png_pixel_data_is_rejected(store, image):
    content = bytearray(image.read_bytes())
    offset = content.index(b"IDAT") + 4
    content[offset] ^= 0xFF
    image.write_bytes(content)
    with pytest.raises(MediaValidationError):
        store.stage(image)
    assert list(store.staging_dir.iterdir()) == []


@pytest.mark.parametrize("failure", ["source", "read", "write", "validation"])
def test_staging_io_failure_cleanup(store, image, monkeypatch, failure):
    if failure == "source":
        source = image.with_name("missing-private-image.png")
    else:
        source = image
    if failure == "read":
        original_open = Path.open

        class BrokenReader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def read(self, *args):
                raise OSError("private read detail")

        def opening(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return BrokenReader(stream) if path == source else stream

        monkeypatch.setattr(Path, "open", opening)
    elif failure == "write":
        def fail(*args):
            raise OSError("private write detail")
        monkeypatch.setattr(module.os, "fsync", fail)
    elif failure == "validation":
        def fail(*args):
            raise RuntimeError("private decode detail")
        monkeypatch.setattr(store, "_inspect", fail)
    with pytest.raises(MediaValidationError) as caught:
        store.stage(source)
    assert "private" not in str(caught.value)
    assert list(store.staging_dir.iterdir()) == []


def test_size_and_decompression_limits_clean_staging(store, image, monkeypatch):
    monkeypatch.setattr(module, "MAX_SIZE_BYTES", image.stat().st_size - 1)
    with pytest.raises(MediaValidationError):
        store.stage(image)
    assert list(store.staging_dir.iterdir()) == []
    monkeypatch.setattr(module, "MAX_SIZE_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(module, "MAX_IMAGE_PIXELS", 5)
    with pytest.raises(MediaValidationError):
        store.stage(image)
    assert list(store.staging_dir.iterdir()) == []


def test_pillow_decompression_warning_is_rejected(store, image, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 4)
    with pytest.raises(MediaValidationError):
        store.stage(image)
    assert list(store.staging_dir.iterdir()) == []


def test_animated_image_full_load_and_pixel_budget(store, tmp_path, monkeypatch):
    source = tmp_path / "tiny.gif"
    frames = [Image.new("RGB", (3, 2), color) for color in ("red", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=10)
    staged = store.stage(source)
    assert staged.mime_type == "image/gif"
    assert (staged.width, staged.height) == (3, 2)
    store.discard(staged)
    monkeypatch.setattr(module, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(MediaValidationError):
        store.stage(source)
    assert list(store.staging_dir.iterdir()) == []


def test_publish_reuses_immutable_content_and_retains_staging(store, image):
    workspace = uuid4()
    staged = store.stage(image)
    first = _publish(store, workspace, staged)
    final = store.path_for(first.storage_path)
    original = final.read_bytes()
    before = final.stat().st_mtime_ns
    second = _publish(store, workspace, staged)
    assert first.created_new is True and second.created_new is False
    assert first.storage_path == f"media/{workspace}/{staged.sha256[:2]}/{staged.sha256}"
    assert final.read_bytes() == original == image.read_bytes()
    assert final.stat().st_mtime_ns == before
    assert staged.path.exists()
    assert store.verify(first.storage_path, staged.sha256)
    store.discard(staged)
    assert final.read_bytes() == original


def test_same_content_different_workspaces_gets_different_paths(store, image):
    staged = store.stage(image)
    first = _publish(store, uuid4(), staged)
    second = _publish(store, uuid4(), staged)
    assert first.created_new and second.created_new
    assert first.storage_path != second.storage_path
    assert store.verify(first.storage_path) and store.verify(second.storage_path)
    store.discard(staged)


def test_corrupt_existing_final_is_never_overwritten(store, image):
    workspace = uuid4()
    staged = store.stage(image)
    first = _publish(store, workspace, staged)
    store.discard(staged)
    final = store.path_for(first.storage_path)
    # Same length, different hash: size alone must not establish integrity.
    corrupted = b"x" * first.size_bytes
    final.write_bytes(corrupted)
    new_stage = store.stage(image)
    with pytest.raises(MediaValidationError):
        _publish(store, workspace, new_stage)
    assert final.read_bytes() == corrupted
    assert not store.verify(first.storage_path)
    assert new_stage.path.exists()
    store.discard(new_stage)


def test_publish_rejects_mutated_staged_bytes_and_metadata(store, image):
    staged = store.stage(image)
    with pytest.raises(MediaValidationError):
        _publish(store, uuid4(), replace(staged, size_bytes=staged.size_bytes + 1))
    staged.path.write_bytes(b"modified")
    with pytest.raises(MediaValidationError):
        _publish(store, uuid4(), staged)
    assert not [path for path in store.media_dir.iterdir() if not path.name.startswith(".")]
    store.discard(staged)


@pytest.mark.parametrize("bad", ["", "/media/abc", "../media/abc", "media/../secret",
    "media/.staging/anything", "media/.locks/anything", "C:/private/file", "media\\file",
    "media//file", "media/./file", "media/file/", "media/file\x00", "not-media/file"])
def test_path_guards_reject_noncontent_paths(store, bad):
    with pytest.raises(MediaValidationError):
        store.path_for(bad)
    assert store.verify(bad) is False


def test_path_guards_canonical_uuid_hash_prefix_and_expected_hash(store, image):
    staged = store.stage(image)
    receipt = _publish(store, uuid4(), staged)
    path = receipt.storage_path
    assert store.path_for(path).is_absolute()
    for bad in (path.upper(), path + "/", path.replace("media/", "media//", 1),
                path.replace("/" + staged.sha256[:2] + "/", "/zz/"),
                path.replace("/" + staged.sha256[:2] + "/", "/00/")):
        if bad != path:
            with pytest.raises(MediaValidationError):
                store.path_for(bad)
    assert not store.verify(path, "0" * 64)
    assert not store.verify(path, "invalid")
    store.discard(staged)


def _symlink(target, link, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("OS does not grant temporary-test symlink creation")


@pytest.mark.parametrize("component", ["media", "workspace", "prefix", "leaf"])
def test_symlink_components_cannot_escape_or_be_deleted(store, image, tmp_path, component):
    staged = store.stage(image)
    workspace = uuid4()
    storage_path = store._storage_path(workspace, staged.sha256)
    final = store.path_for(storage_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / staged.sha256
    sentinel.write_bytes(image.read_bytes())
    if component == "media":
        store.discard(staged)
        store.staging_dir.rmdir()
        store.locks_dir.rmdir()
        store.media_dir.rmdir()
        _symlink(outside, store.media_dir, directory=True)
    elif component == "workspace":
        _symlink(outside, store.media_dir / str(workspace), directory=True)
    elif component == "prefix":
        final.parent.parent.mkdir()
        _symlink(outside, final.parent, directory=True)
    else:
        final.parent.mkdir(parents=True)
        _symlink(sentinel, final)
    with pytest.raises(MediaValidationError):
        store.path_for(storage_path)
    assert not store.verify(storage_path)
    assert not store.delete_if_unreferenced(module.PublishedMedia(storage_path, True,
        staged.sha256, staged.size_bytes), media_exists=False, references_exist=False)
    assert sentinel.read_bytes() == image.read_bytes()


def test_staging_and_lock_symlinks_rejected(store, image, tmp_path):
    staged = store.stage(image)
    outside = tmp_path / "private-original"
    outside.write_bytes(b"never delete")
    staged.path.unlink()
    _symlink(outside, staged.path)
    with pytest.raises(MediaValidationError):
        store.discard(staged)
    assert outside.read_bytes() == b"never delete"
    workspace = uuid4()
    directory = store.locks_dir / str(workspace)
    directory.mkdir()
    _symlink(outside, directory / (staged.sha256 + ".lock"))
    with pytest.raises(MediaValidationError):
        with store.content_lock(workspace, staged.sha256):
            pytest.fail("must reject symlinked lock")


@pytest.mark.parametrize("media_exists,references_exist", [(True, False), (False, True),
    (True, True), (None, False), (False, None), (0, False), (False, 0)])
def test_delete_requires_two_literal_negative_proofs(store, image, media_exists, references_exist):
    staged = store.stage(image)
    workspace = uuid4()
    with store.content_lock(workspace, staged.sha256):
        receipt = store.publish(workspace, staged)
        assert not store.delete_if_unreferenced(receipt, media_exists=media_exists,
            references_exist=references_exist)
    assert store.verify(receipt.storage_path)
    store.discard(staged)


def test_delete_only_new_receipt_and_checked_content(store, image):
    workspace = uuid4()
    staged = store.stage(image)
    with store.content_lock(workspace, staged.sha256):
        first = store.publish(workspace, staged)
        reused = store.publish(workspace, staged)
        assert not store.delete_if_unreferenced(reused, media_exists=False, references_exist=False)
        assert not store.delete_if_unreferenced(replace(first, sha256="0" * 64),
            media_exists=False, references_exist=False)
        assert not store.delete_if_unreferenced(replace(first, size_bytes=0),
            media_exists=False, references_exist=False)
        assert store.delete_if_unreferenced(first, media_exists=False, references_exist=False)
        assert not store.delete_if_unreferenced(first, media_exists=False, references_exist=False)
    assert staged.path.exists()
    assert not store.verify(first.storage_path)
    store.discard(staged)


def test_delete_retains_corrupt_content(store, image):
    staged = store.stage(image)
    workspace = uuid4()
    with store.content_lock(workspace, staged.sha256):
        receipt = store.publish(workspace, staged)
        store.discard(staged)
        final = store.path_for(receipt.storage_path)
        final.write_bytes(b"damaged")
        assert not store.delete_if_unreferenced(receipt, media_exists=False, references_exist=False)
        assert final.read_bytes() == b"damaged"


def test_multithread_lock_excludes_and_same_hash_publish_deduplicates(store, image):
    workspace = uuid4()
    barrier = threading.Barrier(6)
    active = 0
    maximum = 0
    mutex = threading.Lock()

    def worker():
        nonlocal active, maximum
        # Independent instances must share the process-local thread registry.
        own_store = MediaStore(store.data_dir)
        staged = own_store.stage(image)
        barrier.wait(timeout=10)
        try:
            with own_store.content_lock(workspace, staged.sha256):
                with mutex:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.01)
                result = own_store.publish(workspace, staged)
                with mutex:
                    active -= 1
                return result
        finally:
            own_store.discard(staged)

    with ThreadPoolExecutor(max_workers=6) as executor:
        receipts = list(executor.map(lambda _: worker(), range(6)))
    assert maximum == 1
    assert sum(r.created_new for r in receipts) == 1
    assert len({r.storage_path for r in receipts}) == 1
    assert list(store.staging_dir.iterdir()) == []
    lock_file = store.locks_dir / str(workspace) / (receipts[0].sha256 + ".lock")
    assert lock_file.is_file()  # must persist after release


def test_lock_timeout_is_finite_and_different_workspace_independent(store, image, monkeypatch):
    staged = store.stage(image)
    workspace = uuid4()
    monkeypatch.setattr(module, "LOCK_TIMEOUT_SECONDS", 0.1)
    with store.content_lock(workspace, staged.sha256):
        with ThreadPoolExecutor(max_workers=1) as executor:
            def blocked():
                with store.content_lock(workspace, staged.sha256):
                    return True
            before = time.monotonic()
            with pytest.raises(MediaValidationError):
                executor.submit(blocked).result(timeout=2)
            assert time.monotonic() - before < 1.5
        with store.content_lock(uuid4(), staged.sha256):
            pass
    with store.content_lock(workspace, staged.sha256):
        pass
    store.discard(staged)


@pytest.mark.parametrize("attempt", range(5))
def test_multiprocess_lock_excludes_and_publish_reuses(store, image, attempt):
    context = multiprocessing.get_context("spawn")
    workspace = uuid4()
    start = context.Event()
    ready = context.Queue()
    results = context.Queue()
    active = context.Value("i", 0)
    maximum = context.Value("i", 0)
    workers = [context.Process(target=_process_publish, args=(str(store.data_dir), str(image),
        workspace, start, ready, results, active, maximum)) for _ in range(3)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=20) is True
        start.set()
        receipts = [results.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
        assert maximum.value == 1
        assert sum(result[0] is True for result in receipts) == 1
        assert all(result[0] != "ERROR" for result in receipts), "\n".join(result[2] for result in receipts if result[0] == "ERROR")
        assert len({result[1] for result in receipts}) == 1
        assert store.verify(receipts[0][1])
        assert list(store.staging_dir.iterdir()) == []
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        ready.close()
        results.close()


def test_process_crash_releases_os_lock_and_keeps_lock_file(store):
    context = multiprocessing.get_context("spawn")
    workspace = uuid4()
    digest = "a" * 64
    parent, child = context.Pipe(duplex=False)
    worker = context.Process(target=_crash_with_lock,
        args=(str(store.data_dir), workspace, digest, child))
    try:
        worker.start()
        child.close()
        assert parent.poll(20)
        assert parent.recv() == "locked"
        worker.join(timeout=20)
        assert worker.exitcode == 0
        with store.content_lock(workspace, digest):
            pass
        assert (store.locks_dir / str(workspace) / (digest + ".lock")).is_file()
    finally:
        parent.close()
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)

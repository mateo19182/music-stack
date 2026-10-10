import os
from pathlib import Path

import pytest
from app.sharing import Sharing


def setup(tmp_path, scan=None):
    library = tmp_path / 'library'
    library.mkdir()
    audio = library / 'Artist' / 'Album' / 'Track.mp3'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'original-audio')
    audio.with_suffix('.json').write_text('private metadata')
    return Sharing(library, tmp_path / 'shared', tmp_path / 'state', scan), audio


def test_defaults_exclusions_and_restart_preserve_library(tmp_path):
    sharing, audio = setup(tmp_path)
    sharing.reconcile()
    target = sharing.root / audio.relative_to(sharing.library)
    assert os.path.samefile(target, audio)
    assert list(sharing.root.rglob('*.json')) == []
    sharing.configure(path=audio, shared=False)
    assert not target.exists()
    assert audio.read_bytes() == b'original-audio'
    restored = Sharing(sharing.library, sharing.root, tmp_path / 'state')
    restored.reconcile()
    assert not restored.selected(audio)
    assert restored.status()['excluded_count'] == 1
    restored.configure(path=audio, shared=True)
    assert os.path.samefile(target, audio)


def test_global_disable_retains_choices_and_new_files_default_shared(tmp_path):
    sharing, audio = setup(tmp_path)
    sharing.configure(path=audio, shared=False)
    new = sharing.library / 'New.flac'
    new.write_bytes(b'new audio')
    sharing.refresh()
    assert (sharing.root / 'New.flac').exists()
    sharing.configure(enabled=False)
    assert not list(sharing.root.rglob('*.flac'))
    assert new.read_bytes() == b'new audio'
    sharing.configure(enabled=True)
    assert (sharing.root / 'New.flac').exists()
    assert not (sharing.root / audio.relative_to(sharing.library)).exists()


def test_share_replacement_and_stale_removal_never_change_source(tmp_path):
    sharing, audio = setup(tmp_path)
    sharing.reconcile()
    replacement = audio.with_suffix('.temporary')
    replacement.write_bytes(b'replaced audio')
    replacement.replace(audio)
    sharing.reconcile()
    target = sharing.root / audio.relative_to(sharing.library)
    assert target.read_bytes() == b'replaced audio'
    assert os.path.samefile(target, audio)
    audio.unlink()
    sharing.reconcile()
    assert not target.exists()


def test_traversal_symlink_and_unmanaged_collision(tmp_path):
    sharing, audio = setup(tmp_path)
    outside = tmp_path / 'outside.mp3'
    outside.write_bytes(b'private')
    (sharing.library / 'escape.mp3').symlink_to(outside)
    with pytest.raises(ValueError):
        sharing.configure(path='../outside.mp3', shared=True)
    sharing.reconcile()
    assert not (sharing.root / 'escape.mp3').exists()
    target = sharing.root / 'Another.mp3'
    target.write_bytes(b'unmanaged')
    (sharing.library / 'Another.mp3').write_bytes(b'library')
    with pytest.raises(ValueError):
        sharing.reconcile()
    assert target.read_bytes() == b'unmanaged'
    assert audio.read_bytes() == b'original-audio'
    with pytest.raises(ValueError):
        Sharing(sharing.library, sharing.library / 'nested', tmp_path / 'state')


def test_failed_scan_retries_saved_policy(tmp_path):
    attempts = []
    def scan():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError('offline')
        return True
    sharing, audio = setup(tmp_path, scan)
    sharing.refresh()
    assert sharing.status()['scan_pending']
    assert 'unavailable' in sharing.status()['error']
    sharing.refresh()
    assert not sharing.status()['scan_pending']
    assert sharing.status()['error'] is None


def test_file_replaced_during_scan_is_skipped(tmp_path, monkeypatch):
    sharing, audio = setup(tmp_path)
    sharing.reconcile()
    gone = audio.with_name('Old.ogg')
    gone.write_bytes(b'lossy')
    scan = Path.rglob

    def vanishing(self, pattern):
        yield from list(scan(self, pattern))
        gone.unlink(missing_ok=True)

    monkeypatch.setattr(Path, 'rglob', vanishing)
    sharing.reconcile()
    assert sharing.error is None
    assert os.path.samefile(sharing.root / audio.relative_to(sharing.library), audio)
    assert not (sharing.root / gone.relative_to(sharing.library)).exists()

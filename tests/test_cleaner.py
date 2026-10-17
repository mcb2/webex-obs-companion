import os
from pathlib import Path
from unittest.mock import patch

import pytest

from webex_obs.cleaner import MediaCleaner


def media(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"synthetic media")
    os.utime(path, (1, 1))
    return path


@pytest.mark.parametrize("retention", [0, 14])
def test_retention_only_deletes_exact_successfully_processed_files(tmp_path, retention):
    eligible = media(tmp_path, "eligible.mkv")
    unrelated = media(tmp_path, "family.mp4")
    legacy = media(tmp_path, "2026-01-01_01-00-00 - Synthetic meeting.mkv")
    external = media(tmp_path, "external.wav")
    MediaCleaner.mark_processed([str(eligible)], tmp_path)
    MediaCleaner.prune_old_recordings(tmp_path, retention)
    assert not eligible.exists()
    assert all(path.exists() for path in (unrelated, legacy, external))


@pytest.mark.parametrize("state", ["active", "queued", "failed", "recoverable"])
def test_protected_recordings_revoke_persisted_eligibility(tmp_path, state):
    protected = media(tmp_path, f"{state}.mkv")
    MediaCleaner.mark_processed([str(protected)], tmp_path)
    MediaCleaner.protect_recordings([str(protected)])
    # Revocation persists even after in-memory protection is lost on restart.
    with patch.object(MediaCleaner, "_protected", set()):
        MediaCleaner.prune_old_recordings(tmp_path, 0)
    assert protected.exists()
    assert not MediaCleaner._marker(protected).exists()


def test_processing_success_is_persisted_for_next_startup(tmp_path):
    eligible = media(tmp_path, "processed.mkv")
    MediaCleaner.mark_processed([str(eligible)], tmp_path)
    with patch.object(MediaCleaner, "_protected", set()):
        MediaCleaner.prune_old_recordings(tmp_path, 14)
    assert not eligible.exists()


def test_zero_retention_deletes_new_media_only_after_success(tmp_path):
    fresh = tmp_path / "fresh.mkv"
    fresh.write_bytes(b"synthetic")
    MediaCleaner.protect_recordings([str(fresh)])
    MediaCleaner.prune_old_recordings(tmp_path, 0)
    assert fresh.exists()
    MediaCleaner.mark_processed([str(fresh)], tmp_path)
    MediaCleaner.prune_old_recordings(tmp_path, 0)
    assert not fresh.exists()


@pytest.mark.parametrize("change", ["modify", "replace", "symlink"])
def test_reused_or_changed_media_identity_is_not_deleted(tmp_path, change):
    eligible = media(tmp_path, "processed.mkv")
    MediaCleaner.mark_processed([str(eligible)], tmp_path)
    if change == "modify":
        eligible.write_bytes(b"new contents")
    elif change == "replace":
        replacement = media(tmp_path, "replacement.mkv")
        replacement.replace(eligible)
    else:
        eligible.unlink()
        target = media(tmp_path, "personal.mp4")
        eligible.symlink_to(target)
    MediaCleaner.prune_old_recordings(tmp_path, 0)
    assert eligible.exists()


def test_cleanup_respects_age_and_directory_boundaries(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    old = media(recordings, "old.mkv")
    fresh = recordings / "fresh.mkv"
    fresh.write_bytes(b"synthetic")
    outside = media(tmp_path, "outside.mkv")
    MediaCleaner.mark_processed([str(old), str(fresh), str(outside)], recordings)
    MediaCleaner.prune_old_recordings(recordings, 14)
    assert not old.exists()
    assert fresh.exists()
    assert outside.exists()
    assert not MediaCleaner._marker(outside).exists()


def test_malformed_marker_never_authorizes_deletion(tmp_path):
    unrelated = media(tmp_path, "unrelated.mkv")
    marker = MediaCleaner._marker(unrelated)
    marker.parent.mkdir()
    marker.write_text('{"name": "../unrelated.mkv", "identity": []}')
    MediaCleaner.prune_old_recordings(tmp_path, 0)
    assert unrelated.exists()


def test_unavailable_retention_index_preserves_media(tmp_path):
    eligible = media(tmp_path, "processed.mkv")
    MediaCleaner.mark_processed([str(eligible)], tmp_path)
    with patch.object(Path, "glob", side_effect=OSError("synthetic unavailable directory")):
        MediaCleaner.prune_old_recordings(tmp_path, 0)
    assert eligible.exists()


def test_file_replaced_during_processing_cannot_be_enrolled(tmp_path):
    path = media(tmp_path, "processed.mkv")
    MediaCleaner.protect_recordings([str(path)])
    identities = MediaCleaner.recording_identities([str(path)])
    replacement = media(tmp_path, "replacement.mkv")
    replacement.replace(path)
    MediaCleaner.mark_processed([str(path)], tmp_path, identities=identities)
    MediaCleaner.prune_old_recordings(tmp_path, 0)
    assert path.exists()
    assert not MediaCleaner._marker(path).exists()


def test_symlinked_index_cannot_change_files_outside_recordings_dir(tmp_path):
    root, elsewhere = tmp_path / "recordings", tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    path = media(root, "processed.mkv")
    index = root / MediaCleaner._index_name
    index.symlink_to(elsewhere, target_is_directory=True)
    unrelated_marker = elsewhere / MediaCleaner._marker(path).name
    unrelated_marker.write_bytes(b"unrelated synthetic metadata")
    MediaCleaner.protect_recordings([str(path)])
    MediaCleaner.mark_processed([str(path)], root)
    MediaCleaner.prune_old_recordings(root, 0)
    assert path.exists()
    assert unrelated_marker.read_bytes() == b"unrelated synthetic metadata"


def test_failed_index_write_preserves_processed_media(tmp_path):
    path = media(tmp_path, "processed.mkv")
    MediaCleaner.protect_recordings([str(path)])
    with patch.object(Path, "replace", side_effect=OSError("synthetic unavailable index")):
        MediaCleaner.mark_processed([str(path)], tmp_path)
    MediaCleaner.prune_old_recordings(tmp_path, 0)
    assert path.exists()
    assert not MediaCleaner._marker(path).exists()


def test_immediate_session_cleanup_is_scoped_and_ignores_future_timestamp(tmp_path):
    selected = media(tmp_path, "selected.mkv")
    other = media(tmp_path, "other.mkv")
    os.utime(selected, (4102444800, 4102444800))
    MediaCleaner.mark_processed([str(selected), str(other)], tmp_path)
    MediaCleaner.prune_old_recordings(tmp_path, 0, media_files=[str(selected)])
    assert not selected.exists()
    assert other.exists()

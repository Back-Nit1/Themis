"""Regression coverage for scripts/build_assets.py's dependency-vendoring safety.

No network/pip calls here: these test the file-management logic in isolation
(restage/wipe-before-vendor), which is what actually caused the original defect
(a plain build silently stripping a previously vendored boto3/botocore).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("themis_build_assets", ROOT / "scripts/build_assets.py")
assert SPEC and SPEC.loader
build_assets = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_assets
SPEC.loader.exec_module(build_assets)


def test_restage_preserves_paths_it_does_not_manage(tmp_path):
    """The bug: a plain `build` used to rmtree the whole agent-runtime dir, wiping
    boto3/botocore staged by an earlier `--with-boto3` run. restage() must only
    touch the paths it's told to manage."""
    dest = tmp_path / "agent-runtime"
    (dest / "boto3").mkdir(parents=True)
    (dest / "boto3" / "__init__.py").write_text("vendored", encoding="utf-8")
    (dest / "src").mkdir()
    (dest / "src" / "stale.py").write_text("old", encoding="utf-8")

    build_assets.restage(dest, ("src",))

    assert (dest / "boto3" / "__init__.py").read_text(encoding="utf-8") == "vendored"
    assert not (dest / "src" / "stale.py").exists()


def test_restage_is_idempotent_on_a_missing_directory(tmp_path):
    dest = tmp_path / "agent-runtime"
    build_assets.restage(dest, ("src", "customers.json"))
    assert dest.is_dir()


def test_lock_versions_parses_pinned_hashed_requirements(tmp_path):
    lock = tmp_path / "requirements-aws.lock.txt"
    lock.write_text(
        "# comment\n"
        "boto3==1.43.100 \\\n"
        "    --hash=sha256:deadbeef\n"
        "botocore==1.43.100 \\\n"
        "    --hash=sha256:deadbeef\n",
        encoding="utf-8",
    )
    original_lock = build_assets.LOCK
    build_assets.LOCK = lock
    try:
        versions = build_assets._lock_versions()
    finally:
        build_assets.LOCK = original_lock
    assert versions == {"boto3": "1.43.100", "botocore": "1.43.100"}


def test_import_dirs_covers_every_package_in_the_real_lock_file():
    """Repeatable packaging depends on this mapping never falling behind the lock file:
    a package vendor() can't clean up before reinstall correctly if a name is missing."""
    versions = build_assets._lock_versions()
    assert versions, "expected at least one pinned package"
    for dist_name in versions:
        assert dist_name in build_assets.IMPORT_DIRS


def test_real_lock_file_installs_with_require_hashes_and_matches_import_dirs():
    """Sanity check that requirements-aws.lock.txt and build_assets.py agree on
    exactly the same package set (guards against version drift between edits)."""
    versions = build_assets._lock_versions()
    assert set(versions) == set(build_assets.IMPORT_DIRS)

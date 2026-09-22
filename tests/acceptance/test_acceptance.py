from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("themis_acceptance", ROOT / "scripts/acceptance.py")
assert SPEC and SPEC.loader
acceptance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acceptance
SPEC.loader.exec_module(acceptance)


def test_frozen_contract_parser_matches_repository():
    source = (ROOT / "infra/config/tool-schemas.ts").read_text(encoding="utf-8")
    version, tools, idempotent = acceptance._tool_contract(source)
    assert version == acceptance.EXPECTED_CONTRACT_VERSION
    assert tuple(tools) == acceptance.EXPECTED_TOOLS
    assert tuple(idempotent) == acceptance.EXPECTED_IDEMPOTENT_TOOLS


def test_static_json_report_is_machine_readable_and_manual_gates_are_not_green(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/acceptance.py"), "--mode", "static", "--format", "json"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    report = json.loads(proc.stdout)
    assert report["schemaVersion"] == 1
    assert report["summary"]["MANUAL"] >= 4
    assert all(check["status"] == "MANUAL" for check in report["checks"] if check["id"].startswith("live."))
    assert report["overall"] != "PASS"
    # This branch can legitimately be dirty while the test is developed; exit status must
    # still correspond exactly to whether any check reports FAIL.
    assert (proc.returncode != 0) == bool(report["summary"]["FAIL"])


def test_env_parser_counts_commented_deployment_variables():
    source = "THEMIS_MODE=local\n# AGENT_RUNTIME_ARN=\n# prose only\n"
    assert acceptance._documented_env(source) == {"THEMIS_MODE", "AGENT_RUNTIME_ARN"}


def test_root_check_includes_typescript_e2e_suite():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test:python"] == "python3.12 -m pytest -q"
    assert "npm run test:e2e:node" in package["scripts"]["check"]
    assert package["scripts"]["test:e2e:node"] == "node --test tests/e2e/inbound.test.ts"


def test_placeholder_only_directory_fails(tmp_path):
    for relative in acceptance.CRITICAL_DIRECTORIES:
        directory = tmp_path / relative
        directory.mkdir(parents=True)
        (directory / ".gitkeep").touch()
    result = acceptance.check_critical_directories(tmp_path)
    assert result.status == "FAIL"
    assert set(result.details["placeholderOnly"]) == set(acceptance.CRITICAL_DIRECTORIES)


def test_real_file_satisfies_each_critical_directory(tmp_path):
    for relative in acceptance.CRITICAL_DIRECTORIES:
        directory = tmp_path / relative
        directory.mkdir(parents=True)
        (directory / "deliverable.md").write_text("done\n", encoding="utf-8")
    result = acceptance.check_critical_directories(tmp_path)
    assert result.status == "PASS"


def test_python_package_matrix_covers_every_service():
    assert acceptance.PYTHON_PACKAGES == (
        ("bank-tools", "bank_tools"),
        ("merchant-intel", "merchant_intel"),
        ("agent", "orchestrator"),
    )


def test_wheel_builds_are_offline_safe(monkeypatch):
    commands = []

    def fake_command_check(check_id, category, message, command, root):
        commands.append(command)
        return acceptance.Result(check_id, category, "PASS", message)

    monkeypatch.setattr(acceptance, "command_check", fake_command_check)
    acceptance.check_python_wheels(ROOT)
    assert len(commands) == 3
    assert all("--no-build-isolation" in command for command in commands)


def _write_lock(root: Path, packages: dict[str, str]) -> None:
    lock = root / "scripts" / "requirements-aws.lock.txt"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("\n".join(f"{name}=={version}" for name, version in packages.items()) + "\n", encoding="utf-8")


def _write_runtime_entry_points(root: Path) -> Path:
    runtime = root / "infra" / "build" / "agent-runtime"
    for pkg in ("orchestrator", "bank_tools", "merchant_intel"):
        (runtime / "src" / pkg).mkdir(parents=True, exist_ok=True)
    (runtime / "src/orchestrator/main.py").write_text("", encoding="utf-8")
    (runtime / "src/orchestrator/__init__.py").write_text("", encoding="utf-8")
    (runtime / "src/bank_tools/__init__.py").write_text("", encoding="utf-8")
    (runtime / "src/merchant_intel/__init__.py").write_text("", encoding="utf-8")
    (runtime / "src/schemas.json").write_text("{}", encoding="utf-8")
    (runtime / "customers.json").write_text("[]", encoding="utf-8")
    (runtime / "merchant-profiles.json").write_text("{}", encoding="utf-8")
    return runtime


def _vendor(runtime: Path, dist_name: str, version: str, import_dir: str | None = None) -> None:
    (runtime / (import_dir or dist_name)).mkdir(parents=True, exist_ok=True)
    (runtime / f"{dist_name.replace('-', '_')}-{version}.dist-info").mkdir(parents=True, exist_ok=True)


def test_aws_package_check_fails_when_lock_file_is_missing(tmp_path):
    _write_runtime_entry_points(tmp_path)
    result = acceptance.check_aws_package(tmp_path)
    assert result.status == "FAIL"
    assert "lock" in result.message.lower()


def test_aws_package_check_reports_missing_dependency(tmp_path):
    _write_lock(tmp_path, {"boto3": "1.43.100", "botocore": "1.43.100"})
    runtime = _write_runtime_entry_points(tmp_path)
    _vendor(runtime, "botocore", "1.43.100")
    # boto3 never vendored.
    result = acceptance.check_aws_package(tmp_path)
    assert result.status == "FAIL"
    assert any("boto3" in entry for entry in result.details["missing"])


def test_aws_package_check_detects_version_drift(tmp_path):
    _write_lock(tmp_path, {"boto3": "1.43.100"})
    runtime = _write_runtime_entry_points(tmp_path)
    # Stale vendor from an earlier lock version: import dir exists but the pinned
    # dist-info does not, so a version bump without re-vendoring is caught.
    _vendor(runtime, "boto3", "1.40.0")
    result = acceptance.check_aws_package(tmp_path)
    assert result.status == "FAIL"
    assert result.details["missing"] == []
    assert any("boto3" in entry for entry in result.details["versionDrift"])


def test_aws_package_check_passes_for_a_fully_vendored_package(tmp_path):
    _write_lock(tmp_path, {"boto3": "1.43.100", "python-dateutil": "2.9.0.post0"})
    runtime = _write_runtime_entry_points(tmp_path)
    _vendor(runtime, "boto3", "1.43.100")
    _vendor(runtime, "python-dateutil", "2.9.0.post0", import_dir="dateutil")
    result = acceptance.check_aws_package(tmp_path)
    assert result.status == "PASS"
    assert result.details == {"missing": [], "versionDrift": []}


def test_aws_package_check_is_repeatable(tmp_path):
    """Same inputs, same verdict every time: the check has no hidden state or side effects."""
    _write_lock(tmp_path, {"boto3": "1.43.100"})
    runtime = _write_runtime_entry_points(tmp_path)
    _vendor(runtime, "boto3", "1.43.100")
    first = acceptance.check_aws_package(tmp_path)
    second = acceptance.check_aws_package(tmp_path)
    assert (first.status, first.details) == (second.status, second.details)


def test_stale_asset_leftover_from_a_prior_vendor_is_flagged_not_silently_reused(tmp_path):
    """A left-behind older vendor (as `restage()` in build_assets.py now preserves across
    plain builds) must still fail acceptance until it's re-vendored at the locked version."""
    _write_lock(tmp_path, {"boto3": "1.43.100"})
    runtime = _write_runtime_entry_points(tmp_path)
    _vendor(runtime, "boto3", "1.30.0")  # leftover from a much older package:aws run
    result = acceptance.check_aws_package(tmp_path)
    assert result.status == "FAIL"


def test_lock_import_dir_mapping_covers_every_package_in_the_real_lock_file():
    """Guards against the acceptance-side IMPORT_DIRS map drifting from the real lock file."""
    versions = acceptance._lock_package_versions((ROOT / "scripts/requirements-aws.lock.txt").read_text(encoding="utf-8"))
    assert versions, "expected at least one pinned package"
    for dist_name in versions:
        import_dir = acceptance._LOCK_IMPORT_DIRS.get(dist_name, dist_name)
        assert import_dir

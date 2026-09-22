"""Stage the Python deployment assets under infra/build/ (gitignored). Run by `npm run build` in infra.

tools-adapter/  Gateway Lambda: handler + router; bank_tools, merchant_intel,
                orchestrator packages; synthetic merchant profiles; and contract schemas.
agent-runtime/  AgentCore Runtime zip root: all three Python packages under src/ (entry
                `python3 src/orchestrator/main.py`), customer/profile fixtures, and contract schemas.
                `--with-boto3` vendors boto3/botocore (not in the Runtime image) from the
                pinned, hashed scripts/requirements-aws.lock.txt. A plain build only ever
                refreshes src/ and the fixtures, so it never strips a previously vendored
                dependency set staged by an earlier --with-boto3 run.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "infra" / "build"
LOCK = ROOT / "scripts" / "requirements-aws.lock.txt"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")
PACKAGES = {
    "bank_tools": ROOT / "services/bank-tools/src/bank_tools",
    "merchant_intel": ROOT / "services/merchant-intel/src/merchant_intel",
    "orchestrator": ROOT / "services/agent/src/orchestrator",
}


def stage(name: str) -> Path:
    """Recreate an asset directory from scratch (nothing in it is vendored)."""
    dest = OUT / name
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    return dest


def restage(dest: Path, managed: tuple[str, ...]) -> None:
    """Refresh only the paths this script owns; leave everything else (e.g. a
    previously vendored boto3/botocore from --with-boto3) untouched, so a plain
    `build` run can never silently strip runtime dependencies staged earlier."""
    for entry in managed:
        path = dest / entry
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    dest.mkdir(parents=True, exist_ok=True)


def require(asset: str, root: Path, paths: tuple[str, ...]) -> None:
    missing = [path for path in paths if not (root / path).exists()]
    if missing:
        raise RuntimeError(f"{asset} asset is incomplete; missing: {', '.join(missing)}")


# Import path(s) each locked distribution installs (name doesn't always match the
# pip name, e.g. python-dateutil -> dateutil/). Read alongside requirements-aws.lock.txt
# so the wipe-before-vendor step below never drifts from what's actually pinned.
IMPORT_DIRS = {
    "boto3": ("boto3",),
    "botocore": ("botocore",),
    "jmespath": ("jmespath",),
    "python-dateutil": ("dateutil",),
    "s3transfer": ("s3transfer",),
    "six": ("six.py",),
    "urllib3": ("urllib3",),
}


def _lock_versions() -> dict[str, str]:
    versions = {}
    for line in LOCK.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "==" in line:
            name, _, rest = line.partition("==")
            versions[name.strip()] = rest.split()[0]
    return versions


def vendor_aws_deps(runtime: Path) -> None:
    """Wipe any previously vendored deps, then install the exact locked/hashed set.
    Reproducible regardless of what an earlier --with-boto3 run left behind."""
    versions = _lock_versions()
    entries = ["bin", "__pycache__"]
    for dist_name, version in versions.items():
        entries.append(f"{dist_name.replace('-', '_')}-{version}.dist-info")
        entries.extend(IMPORT_DIRS[dist_name])
    for entry in entries:
        path = runtime / entry
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--require-hashes",
         "--target", str(runtime), "-r", str(LOCK)],
        check=True,
    )


def main() -> None:
    tools = stage("tools-adapter")
    for f in ("handler.py", "router.py"):
        shutil.copy(ROOT / "infra/lambda/tools-adapter" / f, tools / f)
    for name, source in PACKAGES.items():
        shutil.copytree(source, tools / name, ignore=IGNORE)
    shutil.copy(ROOT / "fixtures/merchants/demo-profiles.json", tools / "merchant-profiles.json")
    shutil.copy(ROOT / "packages/contracts/schemas.json", tools / "schemas.json")

    runtime = OUT / "agent-runtime"
    restage(runtime, ("src", "customers.json", "merchant-profiles.json"))
    for name, source in PACKAGES.items():
        shutil.copytree(source, runtime / "src" / name, ignore=IGNORE)
    shutil.copy(ROOT / "fixtures/customers/demo.json", runtime / "customers.json")
    shutil.copy(ROOT / "fixtures/merchants/demo-profiles.json", runtime / "merchant-profiles.json")
    shutil.copy(ROOT / "packages/contracts/schemas.json", runtime / "src/schemas.json")
    if "--with-boto3" in sys.argv:
        vendor_aws_deps(runtime)

    require("tools-adapter", tools, (
        "handler.py", "router.py", "bank_tools/__init__.py",
        "merchant_intel/__init__.py", "orchestrator/__init__.py", "merchant-profiles.json", "schemas.json",
    ))
    require("agent-runtime", runtime, (
        "src/orchestrator/main.py", "src/bank_tools/__init__.py", "src/merchant_intel/__init__.py",
        "src/orchestrator/__init__.py", "src/schemas.json", "customers.json", "merchant-profiles.json",
    ))
    require("messaging Lambda", ROOT / "services/messaging/dist", ("index.mjs",))
    if "--with-boto3" in sys.argv:
        require("agent-runtime", runtime, ("boto3/__init__.py", "botocore/__init__.py"))


if __name__ == "__main__":
    main()

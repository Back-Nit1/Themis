#!/usr/bin/env python3
"""Non-mutating release acceptance checks for Themis.

The runner never stages, commits, resets, or otherwise changes git state. Build tools may
write their normal ignored output directories. Live AWS and messaging checks are deliberately
reported as MANUAL so a local green result cannot be mistaken for production verification.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONTRACT_VERSION = "2026-09-22"
EXPECTED_TOOLS = (
    "search_transactions",
    "get_transaction_details",
    "get_transaction_auth_signals",
    "find_related_transactions",
    "get_customer_dispute_history",
    "resolve_merchant",
    "get_merchant_profile",
    "get_merchant_risk_signals",
    "get_case",
    "create_case",
    "update_case",
    "save_evidence",
    "generate_case_report",
    "propose_payment_block",
    "propose_card_replacement",
    "propose_dispute_creation",
    "propose_provisional_credit",
    "submit_review_decision",
    "escalate_case",
    "send_customer_message",
    "send_case_email",
)
EXPECTED_IDEMPOTENT_TOOLS = (
    "create_case",
    "update_case",
    "save_evidence",
    "generate_case_report",
    "propose_payment_block",
    "propose_card_replacement",
    "propose_dispute_creation",
    "propose_provisional_credit",
    "submit_review_decision",
    "escalate_case",
    "send_customer_message",
    "send_case_email",
)
REQUIRED_ENV_DOCUMENTATION = (
    "THEMIS_MODE",
    "AWS_REGION",
    "CDK_DEFAULT_REGION",
    "CDK_DEFAULT_ACCOUNT",
    "ENABLE_RCS",
    "ENABLE_SMS_FALLBACK",
    "ENABLE_SES",
    "ENABLE_BROWSER_RESEARCH",
    "ENABLE_PROACTIVE_DETECTION",
    "ENABLE_REASONING_ESCALATION",
    "THEMIS_STRUCTURED_TOOLS",
    "THEMIS_ESCALATION_CONFIDENCE",
    "BEDROCK_MODEL_ID_FAST",
    "BEDROCK_MODEL_ID_REASONING",
    "DEMO_AUTONOMOUS_CREDIT_LIMIT",
    "DEMO_CREDIT_CONFIDENCE_THRESHOLD",
    "MERCHANT_STORE",
    "MERCHANT_PROFILE_TTL_SECONDS",
    "MERCHANT_RESEARCH_ENABLED",
    "MERCHANT_RESEARCH_DOMAINS",
    "MERCHANT_RESEARCH_MAX_PAGES",
    "MERCHANT_RESEARCH_TIMEOUT_SECONDS",
    "THEMIS_RCS_POOL_ID",
    "THEMIS_SMS_IDENTITY",
    "THEMIS_SES_FROM",
    "THEMIS_SUPPORT",
    "SES_SENDER_DOMAIN",
    "THEMIS_CUSTOMER_DIRECTORY",
    "TRANSACTIONS_TABLE",
    "CASES_TABLE",
    "MERCHANTS_TABLE",
    "AUDIT_TABLE",
    "IDEMPOTENCY_TABLE",
    "ARTIFACTS_BUCKET",
    "MESSAGING_FUNCTION_NAME",
    "THEMIS_RCS_TOPIC_ARN",
    "THEMIS_SMS_TOPIC_ARN",
    "THEMIS_DELIVERY_EVENT_TOPIC_ARN",
    "AGENT_RUNTIME_ARN",
    "AGENT_RUNTIME_QUALIFIER",
    "ACTIVE_MENU_TABLE",
    "GATEWAY_IDENTIFIER",
    "GATEWAY_URL",
    "MEMORY_ID",
)
CRITICAL_DIRECTORIES = (
    "docs/architecture",
    "docs/submission",
    "tests/e2e",
    "scripts/demo",
)
PYTHON_PACKAGES = (
    ("bank-tools", "bank_tools"),
    ("merchant-intel", "merchant_intel"),
    ("agent", "orchestrator"),
)


@dataclass
class Result:
    id: str
    category: str
    status: str
    message: str
    duration_seconds: float = 0.0
    details: dict[str, object] = field(default_factory=dict)


def _result(check_id: str, category: str, status: str, message: str, started: float, **details: object) -> Result:
    return Result(check_id, category, status, message, round(time.monotonic() - started, 3), details)


def _run(command: list[str], root: Path, timeout: int = 900, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _tail(output: str, lines: int = 30) -> str:
    return "\n".join(output.rstrip().splitlines()[-lines:])


def check_clean(root: Path) -> Result:
    started = time.monotonic()
    proc = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], root)
    if proc.returncode:
        return _result("repository.clean", "repository", "FAIL", "Could not inspect git status.", started, output=_tail(proc.stdout))
    dirty = [line for line in proc.stdout.splitlines() if line]
    if dirty:
        return _result("repository.clean", "repository", "FAIL", "Repository has tracked or untracked changes.", started, paths=dirty)
    return _result("repository.clean", "repository", "PASS", "Repository is clean; the runner did not mutate git state.", started)


def _tool_contract(source: str) -> tuple[str | None, list[str], list[str]]:
    version_match = re.search(r"TOOL_CONTRACT_VERSION\s*=\s*'([^']+)'", source)
    block_match = re.search(r"TOOL_DEFINITIONS[^=]*=\s*\[(.*?)\n\];", source, re.DOTALL)
    if not block_match:
        return version_match.group(1) if version_match else None, [], []
    lines = [line for line in block_match.group(1).splitlines() if "{ name:" in line]
    tools: list[str] = []
    idempotent: list[str] = []
    for line in lines:
        match = re.search(r"\{ name: '([a-z_]+)'", line)
        if match:
            tools.append(match.group(1))
            if "idem()" in line:
                idempotent.append(match.group(1))
    return version_match.group(1) if version_match else None, tools, idempotent


def check_contract(root: Path) -> Result:
    started = time.monotonic()
    path = root / "infra/config/tool-schemas.ts"
    if not path.is_file():
        return _result("contract.frozen-tools", "contract", "FAIL", "Tool contract source is missing.", started, path=str(path))
    version, tools, idempotent = _tool_contract(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if version != EXPECTED_CONTRACT_VERSION:
        problems.append(f"version is {version!r}, expected {EXPECTED_CONTRACT_VERSION!r}")
    if tuple(tools) != EXPECTED_TOOLS:
        problems.append(f"tool names/order differ from the frozen {len(EXPECTED_TOOLS)}-tool contract")
    if len(set(tools)) != len(EXPECTED_TOOLS):
        problems.append(f"found {len(tools)} entries and {len(set(tools))} unique names, expected {len(EXPECTED_TOOLS)}")
    if tuple(idempotent) != EXPECTED_IDEMPOTENT_TOOLS:
        problems.append("required idempotencyKey tools differ from the frozen mutation set")
    status = "FAIL" if problems else "PASS"
    message = "; ".join(problems) if problems else f"Frozen versioned {len(EXPECTED_TOOLS)}-tool contract and mutation idempotency fields match."
    return _result("contract.frozen-tools", "contract", status, message, started, version=version, tools=tools, idempotentTools=idempotent)


def check_idempotency_storage(root: Path) -> Result:
    started = time.monotonic()
    expectations = {
        "infra/stacks/data-stack.ts": "partitionKey: { name: 'idempotencyKey'",
        "services/bank-tools/src/bank_tools/dynamo_store.py": '"idempotencyKey": slot',
        "services/messaging/src/idempotency.ts": "idempotencyKey: await hashDynamoKey",
    }
    missing: list[str] = []
    for relative, marker in expectations.items():
        path = root / relative
        if not path.is_file() or marker not in path.read_text(encoding="utf-8"):
            missing.append(f"{relative}: {marker}")
    status = "FAIL" if missing else "PASS"
    message = "DynamoDB idempotency partition-key usage is aligned across infra and services." if not missing else "Idempotency storage markers are missing or inconsistent."
    return _result("contract.idempotency-storage", "contract", status, message, started, missing=missing)


def _documented_env(source: str) -> set[str]:
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", source, re.MULTILINE))


def check_env_example(root: Path) -> Result:
    started = time.monotonic()
    path = root / ".env.example"
    if not path.is_file():
        return _result("configuration.env-example", "configuration", "FAIL", ".env.example is missing.", started)
    documented = _documented_env(path.read_text(encoding="utf-8"))
    missing = sorted(set(REQUIRED_ENV_DOCUMENTATION) - documented)
    status = "FAIL" if missing else "PASS"
    message = "All runtime and deployment variables are documented." if not missing else f".env.example is missing {len(missing)} runtime/deployment variables."
    return _result("configuration.env-example", "configuration", status, message, started, missing=missing)


def check_critical_directories(root: Path) -> Result:
    started = time.monotonic()
    empty: list[str] = []
    evidence: dict[str, list[str]] = {}
    for relative in CRITICAL_DIRECTORIES:
        directory = root / relative
        files = sorted(str(path.relative_to(root)) for path in directory.rglob("*") if path.is_file() and path.name != ".gitkeep") if directory.is_dir() else []
        evidence[relative] = files
        if not files:
            empty.append(relative)
    status = "FAIL" if empty else "PASS"
    message = "Critical release, architecture, E2E, and demo directories contain deliverables." if not empty else "Critical directories are missing or contain placeholders only."
    return _result("repository.critical-directories", "repository", status, message, started, placeholderOnly=empty, files=evidence)


def check_integration_coverage(root: Path) -> Result:
    started = time.monotonic()
    expected = (
        "tests/integration/test_composition.py",
        "tests/integration/composition.test.ts",
        "tests/integration/test_policy_compat.py",
        "tests/integration/test_tool_mapping.py",
    )
    missing = [path for path in expected if not (root / path).is_file()]
    status = "FAIL" if missing else "PASS"
    message = "Local cross-service composition, policy, and frozen-tool coverage is present." if not missing else "Required integration coverage is missing."
    return _result("tests.integration-coverage", "tests", status, message, started, missing=missing)


def check_assets(root: Path) -> Result:
    started = time.monotonic()
    expected = (
        "infra/build/tools-adapter/handler.py",
        "infra/build/tools-adapter/router.py",
        "infra/build/tools-adapter/bank_tools/__init__.py",
        "infra/build/tools-adapter/merchant_intel/__init__.py",
        "infra/build/tools-adapter/orchestrator/__init__.py",
        "infra/build/tools-adapter/schemas.json",
        "infra/build/agent-runtime/src/orchestrator/main.py",
        "infra/build/agent-runtime/src/bank_tools/__init__.py",
        "infra/build/agent-runtime/src/merchant_intel/__init__.py",
        "infra/build/agent-runtime/src/schemas.json",
        "services/messaging/dist/index.mjs",
    )
    missing = [path for path in expected if not (root / path).is_file()]
    mismatched: list[str] = []
    canonical = root / "packages/contracts/schemas.json"
    if canonical.is_file():
        for relative in ("infra/build/tools-adapter/schemas.json", "infra/build/agent-runtime/src/schemas.json"):
            staged = root / relative
            if staged.is_file() and staged.read_bytes() != canonical.read_bytes():
                mismatched.append(relative)
    status = "FAIL" if missing or mismatched else "PASS"
    message = "CDK deployment assets are complete and carry the canonical schemas." if status == "PASS" else "Deployment asset staging is incomplete or stale."
    return _result("build.deployment-assets", "build", status, message, started, missing=missing, schemaMismatches=mismatched)


def command_check(check_id: str, category: str, message: str, command: list[str], root: Path, timeout: int = 1200, env: dict[str, str] | None = None) -> Result:
    started = time.monotonic()
    print(f"[{check_id}] {' '.join(command)}", file=sys.stderr, flush=True)
    try:
        proc = _run(command, root, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as error:
        return _result(check_id, category, "FAIL", f"Timed out after {timeout} seconds.", started, output=_tail(error.stdout or ""))
    status = "PASS" if proc.returncode == 0 else "FAIL"
    final_message = message if status == "PASS" else f"Command failed with exit code {proc.returncode}."
    return _result(check_id, category, status, final_message, started, command=command, output=_tail(proc.stdout))


def check_python_imports(root: Path) -> Result:
    paths = [str(root / "services" / service / "src") for service, _ in PYTHON_PACKAGES]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
    modules = ", ".join(module for _, module in PYTHON_PACKAGES)
    return command_check(
        "python.imports",
        "python",
        "All three service packages import from their declared source trees.",
        [sys.executable, "-c", f"import {modules}"],
        root,
        env=env,
    )


def check_python_wheels(root: Path) -> list[Result]:
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="themis-acceptance-wheels-") as wheel_dir:
        for service, module in PYTHON_PACKAGES:
            results.append(command_check(
                f"python.wheel.{service}",
                "python",
                f"{service} builds as an installable wheel for {module}.",
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-build-isolation",
                    "--no-deps",
                    "--wheel-dir",
                    wheel_dir,
                    f"services/{service}",
                ],
                root,
            ))
    return results


def skipped_full_checks() -> list[Result]:
    checks = (
        ("build.root-check", "build", "Run with --mode full to compile packages and execute all local tests."),
        ("python.imports", "python", "Run with --mode full to verify Python package imports."),
        ("python.wheels", "python", "Run with --mode full to build all Python wheels."),
        ("infra.cdk-synth", "infra", "Run with --mode full to synthesize CDK and stage deployment assets."),
        ("build.deployment-assets", "build", "Run with --mode full to validate freshly staged asset contents."),
    )
    return [Result(check_id, category, "SKIP", message) for check_id, category, message in checks]


def manual_checks() -> list[Result]:
    gates = (
        ("live.aws-deploy", "Deploy all CDK stacks in an approved AWS account and verify CloudFormation completes."),
        ("live.agentcore-wire", "Exercise real AgentCore Runtime, Memory, Gateway invocation envelopes, and Cedar denial responses."),
        ("live.messaging-identities", "Register/attach RCS and SMS identities, verify the SES sender, and confirm delivery telemetry."),
        ("live.end-to-end", "Send an approved synthetic live message and verify the case, audit, report, escalation, and customer notification path."),
        ("live.browser-research", "Keep browser research disabled or approve and verify a bounded page client/source provider."),
    )
    return [Result(check_id, "live", "MANUAL", message) for check_id, message in gates]


def metadata(root: Path, mode: str) -> dict[str, object]:
    commit = _run(["git", "rev-parse", "HEAD"], root).stdout.strip()
    branch = _run(["git", "branch", "--show-current"], root).stdout.strip()
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "repository": str(root),
        "branch": branch,
        "commit": commit,
        "mode": mode,
    }


def run_acceptance(root: Path, mode: str) -> dict[str, object]:
    checks: list[Result] = [
        check_clean(root),
        check_contract(root),
        check_idempotency_storage(root),
        check_env_example(root),
        check_critical_directories(root),
        check_integration_coverage(root),
    ]
    if mode == "full":
        checks.append(command_check("build.root-check", "build", "Root build and all JavaScript/TypeScript/Python/integration tests pass.", ["npm", "run", "check"], root))
        checks.append(check_python_imports(root))
        checks.extend(check_python_wheels(root))
        checks.append(command_check("infra.cdk-synth", "infra", "CDK synthesis completes locally.", ["npm", "run", "synth", "--workspace=infra"], root))
        checks.append(check_assets(root))
    else:
        checks.extend(skipped_full_checks())
    checks.extend(manual_checks())
    counts = {status: sum(check.status == status for check in checks) for status in ("PASS", "FAIL", "SKIP", "MANUAL")}
    overall = "FAIL" if counts["FAIL"] else "PASS_WITH_MANUAL_GATES" if counts["MANUAL"] else "PASS"
    return {**metadata(root, mode), "overall": overall, "summary": counts, "checks": [asdict(check) for check in checks]}


def render_text(report: dict[str, object]) -> str:
    lines = [
        f"Themis release acceptance: {report['overall']}",
        f"commit={report['commit']} branch={report['branch']} mode={report['mode']}",
        "",
    ]
    for check in report["checks"]:  # type: ignore[assignment]
        lines.append(f"{check['status']:>6}  {check['id']}: {check['message']}")
        missing = check["details"].get("missing")
        if missing:
            lines.append(f"        missing: {', '.join(missing)}")
        placeholder = check["details"].get("placeholderOnly")
        if placeholder:
            lines.append(f"        placeholder-only: {', '.join(placeholder)}")
    counts = report["summary"]
    lines.extend(("", f"PASS={counts['PASS']} FAIL={counts['FAIL']} SKIP={counts['SKIP']} MANUAL={counts['MANUAL']}"))
    return "\n".join(lines)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("static", "full"), default="full", help="static skips builds/tests; full is the release gate")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path, help="also write the complete report to this path")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_acceptance(args.root.resolve(), args.mode)
    rendered = json.dumps(report, indent=2) if args.format == "json" else render_text(report)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 1 if report["summary"]["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

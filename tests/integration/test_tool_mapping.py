"""Frozen mapping of the 21 Gateway tools: owner, camelCase arguments, idempotencyKey."""
import re

import harness  # noqa: F401
from bank_tools.dispatch import NOT_OWNED_TOOLS, SPECS
from router import TOOL_OWNERS, build_specs

ROOT = harness.ROOT
SCHEMA_TS = (ROOT / "infra/config/tool-schemas.ts").read_text(encoding="utf-8")
KIND = {"str": "str", "num": "num", "bool": "bool", "arr": "list"}


def gateway_tools():
    """name -> {param: (kind, required)} parsed from the TOOL_DEFINITIONS lines of tool-schemas.ts."""
    tools = {}
    for line in re.findall(r"^\s*\{ name: '(\w+)'.*params: \[(.*)\] \},$", SCHEMA_TS, re.M):
        name, params = line
        entries = {m.group(2): (KIND[m.group(1)], (m.group(3) or ("false" if m.group(1) in ("arr", "bool") else "true")) == "true")
                   for m in re.finditer(r"\b(str|num|arr|bool)\('(\w+)'(?:,\s*(true|false))?", params)}
        if "idem()" in params:
            entries["idempotencyKey"] = ("str", True)
        tools[name] = entries
    return tools


def adapter_specs():
    comp = harness.Composition()
    return {**SPECS, **comp.adapter.specs}


def test_gateway_declares_exactly_the_21_owned_tools():
    assert set(gateway_tools()) == set(TOOL_OWNERS) and len(TOOL_OWNERS) == 21
    assert set(TOOL_OWNERS.values()) == {"bank-tools", "merchant-intel", "agentcore", "messaging"}


def test_tools_bank_tools_disclaims_are_registered_by_their_owner():
    specs = adapter_specs()
    assert {t for t, o in TOOL_OWNERS.items() if o in ("agentcore", "messaging")} == set(NOT_OWNED_TOOLS)
    assert set(NOT_OWNED_TOOLS) <= set(build_specs(None, None, None))
    assert set(specs) == set(TOOL_OWNERS)


def test_every_gateway_argument_matches_the_service_argument():
    specs = adapter_specs()
    for tool, declared in gateway_tools().items():
        actual = {p.camel: (p.kind, p.required) for p in specs[tool].params}
        assert actual == declared, tool


def test_idempotency_key_is_consistent_everywhere():
    declared = {t for t, params in gateway_tools().items() if "idempotencyKey" in params}
    assert declared == {t for t, s in adapter_specs().items() if s.mutating}
    assert declared == set(re.findall(r"'(\w+)'", SCHEMA_TS.split("IDEMPOTENT_TOOL_NAMES")[1]))
    # one PK name across the DynamoDB table, bank-tools rows and messaging claims
    assert "partitionKey: { name: 'idempotencyKey'" in (ROOT / "infra/stacks/data-stack.ts").read_text()
    assert "idempotencyKey=slot" in (ROOT / "services/bank-tools/src/bank_tools/dynamo_store.py").read_text().replace(" ", "")
    assert "idempotencyKey: await hashDynamoKey" in (ROOT / "services/messaging/src/idempotency.ts").read_text()


def test_owner_routing_reaches_the_owning_service():
    comp = harness.Composition()
    case_id = comp.adapter.call("create_case", {"customerId": "customer_demo_001", "claimType": "UNRECOGNIZED_MERCHANT",
                                                 "transactionIds": ["txn_demo_0001"], "idempotencyKey": "k1"})["case"]["caseId"]
    assert comp.adapter.call("resolve_merchant", {"descriptor": "ASTDIGITAL"})["merchantId"] == "merchant_demo_001"      # merchant-intel
    assert comp.adapter.call("generate_case_report", {"caseId": case_id, "idempotencyKey": "k2"})["status"] == "ok"      # agentcore
    assert comp.reports.get(case_id)["caseId"] == case_id
    sent = comp.adapter.call("send_customer_message", {"caseId": case_id, "channel": "SMS", "text": "hi", "idempotencyKey": "k3"})
    assert sent["status"] == "ok" and comp.sent[-1]["message"]["customerExternalId"] == "+15555550100"                  # messaging
    email = comp.adapter.call("send_case_email", {"caseId": case_id, "idempotencyKey": "k4"})
    assert email["status"] == "ok" and comp.sent[-1]["themisTool"] == "send_case_email" and comp.sent[-1]["recipient"]
    assert comp.adapter.call("send_customer_message", {"caseId": case_id, "channel": "SMS", "text": "hi", "idempotencyKey": "k3"}) == sent
    assert len(comp.sent) == 2  # replayed idempotencyKey does not message twice
    bad = comp.adapter.call("send_customer_message", {"caseId": case_id, "channel": "EMAIL", "text": "x", "idempotencyKey": "k5"})
    assert bad["error"]["code"] == "VALIDATION_ERROR"

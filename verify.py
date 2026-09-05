#!/usr/bin/env python3
"""Prove the deployed API actually works, as REST and as MCP.

An HTTP 200 proves almost nothing here, which is the entire reason this file
exists rather than a curl in the README:

  * An MCP endpoint answers 200 on a JSON-RPC *error*. The failure is in the
    body, so the body is what gets asserted.
  * A trailing slash does not reach the MCP entrypoint. `/mcp` handshakes
    properly; `/mcp/` misses the entrypoint path match, falls through to the
    http-proxy entrypoint and is forwarded to the backend, which serves GET
    only and answers 501. Both are asserted, so a future version that starts
    matching the trailing slash says so rather than quietly changing which
    server answers.
  * Listing a tool does not mean calling it works. The gateway can advertise
    a tool whose upstream call fails on the shared secret, so the last check
    drives a real `tools/call` and requires actual vessels back.

Usage:
    make verify
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent


def load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

GATEWAY = os.environ.get("GRAVITEE_GATEWAY_URL", "https://gateway.prodger.cc").rstrip("/")
BASE = f"{GATEWAY}/poole-ais"
API_KEY = os.environ.get("AIS_API_KEY", "")
TOOL_NAME = "get_live_vessels"

if not API_KEY:
    # Without this the run does not fail usefully, it fails confusingly: every
    # request goes out as "?api-key=" and comes back 401, producing six
    # unrelated-looking failures that read like a broken deployment. The key is
    # not a deploy credential and is deliberately not in .env.example's
    # required set, so an empty one is a setup mistake, not a fault in the API.
    sys.exit(
        "AIS_API_KEY is not set.\n"
        "  Subscribe to the Public plan in the developer portal, then put the\n"
        "  key in .env as AIS_API_KEY=... (see .env.example)."
    )

failures: list[str] = []


def pace() -> None:
    """Stay under the API's own rate limit while testing it.

    The Public plan allows five requests a second, and this script now makes
    enough calls in quick succession to trip it. When it does, the refusal does
    not announce itself as a rate limit: a later check simply gets no payload
    and reports "came back with vessel data" as a failure, which reads exactly
    like a broken API and is not.

    That happened when the spec-agreement check was added: one extra request
    made two unrelated checks fail. So the pause is here, named, rather than
    left for somebody to rediscover as a flaky suite.
    """
    time.sleep(0.4)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(label)


def url(path: str) -> str:
    """The key goes in the query string, not a header.

    This installation's gateway does not accept X-Gravitee-Api-Key (or any
    other header form) and answers 401; `?api-key=` is what works. Verified
    against the live gateway, both directly and through Cloudflare. It is
    also what makes the MCP endpoint usable by clients that take a URL and
    nothing else — at the cost of the key appearing in URLs and access logs.
    """
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}api-key={API_KEY}"


def headers() -> dict:
    return {
        "Content-Type": "application/json",
        # Streamable HTTP wraps the payload in SSE frames, so ask for both
        # and parse accordingly below.
        "Accept": "application/json, text/event-stream",
    }


def rpc(target: str, method: str, params: dict | None = None, rpc_id: int = 1):
    """One JSON-RPC call. Returns (http_status, parsed_body_or_None, raw_text).

    The response may be a bare JSON object or an SSE frame sequence
    (`event: message` then `data: {...}`), so the data line is pulled out by
    hand rather than assuming `.json()` will work.
    """
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    r = requests.post(url(target), headers=headers(), json=body, timeout=20)
    text = r.text
    parsed = None
    try:
        parsed = r.json()
    except ValueError:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    parsed = json.loads(line[5:].strip())
                    break
                except ValueError:
                    continue
    return r.status_code, parsed, text


print("REST")
r = requests.get(url(f"{BASE}/vessels"), timeout=20)
check("GET /vessels returns 200", r.status_code == 200, f"got {r.status_code}")
rest_ok = False
if r.status_code == 200:
    try:
        payload = r.json()
        vessels = payload.get("vessels", [])
        check("the response carries vessels", isinstance(vessels, list) and len(vessels) > 0,
              f"{len(vessels)} vessel(s)")
        check("and a generated_at", bool(payload.get("generated_at")), str(payload.get("generated_at")))
        # The privacy boundary, asserted at the far end of the whole chain
        # rather than only in the backend's own tests.
        blob = json.dumps(payload)
        check("no station block survived to the gateway", "\"station\"" not in blob)
        check("no per-vessel distance/bearing survived",
              "\"distance\"" not in blob and "\"bearing\"" not in blob)
        rest_ok = True
    except ValueError:
        check("the response is JSON", False, r.text[:200])

print()
print("Auth")
r = requests.get(f"{BASE}/vessels", timeout=20)
check("a call with no API key is refused", r.status_code in (401, 403), f"got {r.status_code}")

print()
print("MCP")
# Both spellings, because they have been seen to route to different servers.
for path in (f"{BASE}/mcp",):
    print(f"  -- {path}")
    status, body, raw = rpc(path, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "poole-ais-api-verify", "version": "1.0.0"},
    })
    result = (body or {}).get("result") or {}
    server_info = result.get("serverInfo") or {}
    check(f"{path} handshake has no error", bool(body) and "error" not in body,
          json.dumps((body or {}).get("error", ""))[:200] or raw[:120])
    check(f"{path} returns a protocolVersion", bool(result.get("protocolVersion")),
          str(result.get("protocolVersion")))
    check(f"{path} identifies itself", bool(server_info.get("name")), str(server_info.get("name")))

    status, body, raw = rpc(path, "tools/list", {}, rpc_id=2)
    tools = ((body or {}).get("result") or {}).get("tools") or []
    names = [t.get("name") for t in tools]
    check(f"{path} lists at least one tool", len(tools) > 0, f"{names}")
    check(f"{path} lists {TOOL_NAME}", TOOL_NAME in names, f"{names}")

print()
print("Trailing slash does NOT reach the MCP entrypoint")
# /mcp/ misses the mcp entrypoint's path match and falls through to the
# http-proxy entrypoint, which forwards to the backend. The backend serves
# GET only, so a POST there comes back 501 from Python's own http.server
# rather than anything MCP-shaped. Asserted so that if a future version
# starts matching the trailing slash, this says so rather than silently
# changing which server answers.
r = requests.post(url(f"{BASE}/mcp/"), headers=headers(), json={"jsonrpc": "2.0", "id": 9, "method": "initialize"}, timeout=20)
check("/mcp/ does not answer MCP (it falls through to http-proxy)",
      r.status_code == 501 or "jsonrpc" not in r.text, f"status {r.status_code}")

pace()
print()
print("The published spec and the live API agree")

# The spec is what a consumer generates a client from, so a field the API
# returns and the spec omits produces a type that silently drops it. That is
# exactly what happened to `source` on 5 Sep 2026: it was added to the API and
# not to openapi.yaml, and nothing noticed, because every other check here asks
# whether particular fields are PRESENT and none asks whether the two agree.
#
# Compared BOTH ways deliberately. A field in the API and not the spec is an
# undocumented one; a field in the spec and not the API is a promise the
# service does not keep, and that is the worse direction.
try:
    import yaml
except ImportError:
    sys.exit("PyYAML is needed to check the spec against the API: pip install -r requirements.txt")

spec = yaml.safe_load(pathlib.Path(__file__).resolve().parent.joinpath("openapi.yaml").read_text())
spec_fields = set(spec["components"]["schemas"]["Vessel"]["properties"])
live = requests.get(url(f"{BASE}/vessels"), timeout=20).json()
live_fields = set(live["vessels"][0]) if live.get("vessels") else set()
check("there is a vessel to compare against at all", bool(live_fields))
# check() here is (label, ok, detail), NOT (label, got, want) like the tests in
# the poole-ais repo. Passing a set as `ok` inverts the check: empty is falsy,
# so it fails precisely when the two agree. Done exactly that once already.
undocumented = sorted(live_fields - spec_fields)
unkept = sorted(spec_fields - live_fields)
check("every field the API returns is in the spec", not undocumented,
      f"undocumented: {undocumented}")
check("and every field the spec promises is returned", not unkept,
      f"promised but absent: {unkept}")

pace()
print()
print("A real tool call, not just a listing")
status, body, raw = rpc(f"{BASE}/mcp", "tools/call", {"name": TOOL_NAME, "arguments": {}}, rpc_id=3)
result = (body or {}).get("result") or {}
content = result.get("content") or []
text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
check("the call returned without an error", bool(body) and "error" not in body,
      json.dumps((body or {}).get("error", ""))[:300] or raw[:150])
has_vessels = '"mmsi"' in text or '"vessels"' in text
check("and came back with vessel data", has_vessels, text[:150])

# Absence has to be asserted against something. Checking that "station" is not
# in an empty string passes on every failure, which is exactly what happened on
# 2026-09-05: the tool call 401'd, text was "", and the one check guarding the
# receiver's position reported PASS while everything around it failed. Require
# a real payload first, so the check can only pass by actually being true.
leaked = [f for f in ('"station"', '"distance"', '"bearing"', '"level"', '"ppm"') if f in text]
check("with the receiver position still absent",
      has_vessels and not leaked,
      "no payload to assert against" if not has_vessels else ", ".join(leaked))

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("all checks passed")

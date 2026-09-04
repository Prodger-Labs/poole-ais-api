#!/usr/bin/env python3
"""Prove the deployed API actually works, as REST and as MCP.

An HTTP 200 proves almost nothing here, which is the entire reason this file
exists rather than a curl in the README:

  * An MCP endpoint answers 200 on a JSON-RPC *error*. The failure is in the
    body, so the body is what gets asserted.
  * A trailing slash can route somewhere else entirely. `/mcp/` and `/mcp`
    have been observed landing on different servers, one of which completes
    an `initialize` handshake perfectly happily and then lists zero tools.
    So: probe both, and require the tool list to be non-empty and to contain
    the tool this API is supposed to expose.
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

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(label)


def headers() -> dict:
    return {
        "X-Gravitee-Api-Key": API_KEY,
        "Content-Type": "application/json",
        # Streamable HTTP wraps the payload in SSE frames, so ask for both
        # and parse accordingly below.
        "Accept": "application/json, text/event-stream",
    }


def rpc(url: str, method: str, params: dict | None = None, rpc_id: int = 1):
    """One JSON-RPC call. Returns (http_status, parsed_body_or_None, raw_text).

    The response may be a bare JSON object or an SSE frame sequence
    (`event: message` then `data: {...}`), so the data line is pulled out by
    hand rather than assuming `.json()` will work.
    """
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    r = requests.post(url, headers=headers(), json=body, timeout=20)
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
r = requests.get(f"{BASE}/vessels", headers={"X-Gravitee-Api-Key": API_KEY}, timeout=20)
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
for path in (f"{BASE}/mcp", f"{BASE}/mcp/"):
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
print("A real tool call, not just a listing")
status, body, raw = rpc(f"{BASE}/mcp", "tools/call", {"name": TOOL_NAME, "arguments": {}}, rpc_id=3)
result = (body or {}).get("result") or {}
content = result.get("content") or []
text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
check("the call returned without an error", bool(body) and "error" not in body,
      json.dumps((body or {}).get("error", ""))[:300] or raw[:150])
check("and came back with vessel data", '"mmsi"' in text or '"vessels"' in text, text[:150])
check("with the receiver position still absent", '"station"' not in text and '"distance"' not in text)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("all checks passed")

#!/usr/bin/env python3
"""Publish the Poole Harbour AIS API to Gravitee, as REST and as MCP.

One V4 API, two entrypoints on one listener: `http-proxy` for callers who
want JSON over HTTP, and `mcp` for agents that want a tool. Both run the same
flows, so "the agent calls the tool" and "curl calls the endpoint" are
governed identically rather than nearly-identically.

RUN IT MORE THAN ONCE. That is the whole design constraint here, and it is
why this is not a delete-then-reimport script:

    A public API has SUBSCRIBERS. Deleting and reimporting closes every plan,
    which cancels every subscription and revokes every key that anyone signed
    up for in the developer portal. Fine for a demo nobody has subscribed to,
    ruinous the second somebody has. So: create once, update in place, and
    never touch a plan that already exists.

TWO THINGS THAT WILL WASTE AN EVENING IF YOU DO NOT KNOW THEM

1.  The gateway is sharded, and an untagged API is invisible. This gateway
    runs with `gravitee_tags=gamma`. An API with no matching tag reads
    STARTED and PUBLISHED in the console, looks completely healthy, and 404s
    at the gateway for ever. `"tags": ["gamma"]` below is not decoration.

2.  Starting an API is not deploying it. `_start` changes the API's state;
    it does not push the definition to the data plane. Without the explicit
    POST to `/deployments` at the end, changes sit in the console and never
    reach the gateway.

Usage:
    cp .env.example .env && $EDITOR .env
    make deploy
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parent


def load_dotenv() -> None:
    """Populate os.environ from .env for anything not already set, so this
    works whether or not the caller sourced it. Never overrides."""
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

MANAGEMENT_URL = os.environ.get("GRAVITEE_MANAGEMENT_URL", "https://gravitee.prodger.cc/management")
ADMIN_USER = os.environ.get("GRAVITEE_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("GRAVITEE_ADMIN_PASSWORD", "")

# Where the gateway will send traffic. The backend is a loopback-only service
# on the receiver, published through a Cloudflare Tunnel hostname, so this is
# a public URL that only answers to callers presenting the shared secret.
UPSTREAM_URL = os.environ.get("AIS_UPSTREAM_URL", "https://ais-api.prodger.cc")
UPSTREAM_SECRET = os.environ.get("AIS_UPSTREAM_SECRET", "")

# The sharding tag this gateway serves. See the header.
GATEWAY_TAG = os.environ.get("GRAVITEE_GATEWAY_TAG", "gamma")

API_NAME = "Poole Harbour AIS"
LISTENER_PATH = "/poole-ais/"
MCP_PATH = "/mcp"
PLAN_NAME = "Public"
RATE_LIMIT_PER_SECOND = int(os.environ.get("AIS_RATE_LIMIT", "5"))

V2 = f"{MANAGEMENT_URL}/v2/environments/DEFAULT"

session = requests.Session()
session.auth = (ADMIN_USER, ADMIN_PASSWORD)
session.headers.update({"Content-Type": "application/json"})


def log(message: str) -> None:
    print(f"[deploy] {message}", flush=True)


# =============================================================================
# The API definition
# =============================================================================

# The secret is injected by the gateway and never accepted from the caller:
# transform-headers overwrites unconditionally, so a client sending its own
# X-Gateway-Secret has it replaced rather than passed through.
#
# It is read from an API PROPERTY rather than written literally into the
# policy. Properties marked encrypted are stored encrypted at rest, which
# matters because the alternative sits in clear in Mongo and in every nightly
# database backup taken off that box.
SECRET_FLOW = {
    "name": "inject-upstream-secret",
    "enabled": True,
    "selectors": [{"type": "HTTP", "path": "/", "pathOperator": "STARTS_WITH", "methods": []}],
    "request": [
        {
            "name": "Present the shared secret to the receiver",
            "enabled": True,
            "policy": "transform-headers",
            "configuration": {
                "addHeaders": [{"name": "X-Gateway-Secret", "value": "{#properties['upstream_secret']}"}]
            },
        }
    ],
    "response": [], "subscribe": [], "publish": [], "entrypointConnect": [], "interact": [], "tags": [],
}

# One rate limit, and nothing else. A quota on top of it, a cache in front of
# it and a response template for each of them are all things this API would
# carry for the look of the thing rather than because anyone is going to hit
# them. The limit that matters is the one protecting a Raspberry Pi from a
# runaway client.
RATE_LIMIT_FLOW = {
    "name": "rate-limit",
    "enabled": True,
    "selectors": [{"type": "HTTP", "path": "/", "pathOperator": "STARTS_WITH", "methods": []}],
    "request": [
        {
            "name": "Rate limit per key",
            "enabled": True,
            "policy": "rate-limit",
            "configuration": {
                "async": False,
                "addHeaders": True,
                "rate": {"useKeyOnly": False, "periodTime": 1, "periodTimeUnit": "SECONDS", "limit": RATE_LIMIT_PER_SECOND, "key": ""},
            },
        }
    ],
    "response": [], "subscribe": [], "publish": [], "entrypointConnect": [], "interact": [], "tags": [],
}

# One tool. The API has one endpoint, so it has one tool, and a tool per
# imagined use case would be three names for the same call.
#
# No path parameters and no request body, which sidesteps both of the MCP
# entrypoint's sharp edges: path params substitute as ":name" rather than
# "{name}", and a request body has to be a single argument called
# "bodySchema" or the gateway sends an empty one.
MCP_TOOLS = [
    {
        "gatewayMapping": {"http": {"path": "/vessels", "method": "GET"}},
        "toolDefinition": {
            "name": "get_live_vessels",
            "description": (
                "Every vessel currently audible to a single AIS receiver in Poole, Dorset, UK "
                "(approximately [coordinates removed], about 21 nautical miles of range over Poole Bay "
                "and the Channel approaches). Returns live positions only - there is no history "
                "and nothing is stored. Typically 25-40 vessels, mostly small leisure craft, "
                "plus cross-Channel ferries and occasionally a Coastguard search-and-rescue "
                "helicopter. Use for questions about what is on the water near Poole right now."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    }
]


def api_definition() -> dict:
    return {
        "api": {
            "definitionVersion": "V4",
            "type": "PROXY",
            "name": API_NAME,
            "apiVersion": "1.0.0",
            "description": (
                "Live AIS vessel positions from a home-built receiver in Poole, Dorset. "
                "One endpoint, current state only, no history."
            ),
            "state": "STARTED",
            # PUBLIC so it is visible to anyone browsing the developer portal;
            # the API-key plan is what actually gates calling it.
            "visibility": "PUBLIC",
            "lifecycleState": "PUBLISHED",
            # Without this the gateway never serves it. See the header.
            "tags": [GATEWAY_TAG],
            "labels": ["ais", "maritime", "mcp"],
            "categories": [],
            "properties": [
                {
                    "key": "upstream_secret",
                    "value": UPSTREAM_SECRET,
                    "encrypted": True,
                    "dynamic": False,
                }
            ],
            "resources": [],
            "listeners": [
                {
                    "type": "HTTP",
                    "paths": [{"path": LISTENER_PATH, "overrideAccess": False}],
                    "pathMappings": [],
                    "entrypoints": [
                        {"type": "http-proxy", "qos": "AUTO", "configuration": {}},
                        {
                            "type": "mcp",
                            "qos": "AUTO",
                            "configuration": {
                                "mcpPath": MCP_PATH,
                                "description": "Live AIS vessel positions from Poole Harbour.",
                                "tools": MCP_TOOLS,
                            },
                        },
                    ],
                    "servers": [],
                }
            ],
            "endpointGroups": [
                {
                    "name": "receiver",
                    "type": "http-proxy",
                    "loadBalancer": {"type": "ROUND_ROBIN"},
                    # 5s read timeout, not the 30s a default gives. The backend
                    # answers a loopback read on the receiver and gives up on it
                    # after 2s itself, so anything past 5 here is the gateway
                    # holding a connection open for a backend that has already
                    # decided to fail.
                    "sharedConfiguration": json.dumps(
                        {
                            "proxy": {"useSystemProxy": False, "enabled": False},
                            "http": {"connectTimeout": 3000, "readTimeout": 5000, "version": "HTTP_1_1"},
                            "ssl": {"trustAll": False},
                        }
                    ),
                    "endpoints": [
                        {
                            "name": "poolepi",
                            "type": "http-proxy",
                            "weight": 1,
                            "inheritConfiguration": True,
                            "configuration": {"target": UPSTREAM_URL},
                            "sharedConfigurationOverride": "{}",
                            "services": {},
                            "secondary": False,
                            "tenants": [],
                        }
                    ],
                    "services": {},
                }
            ],
            # Payload logging is deliberately OFF, and headers especially so.
            # `headers: true` writes every caller's API key into Elasticsearch
            # in clear, on a box whose indices are shared with other work.
            # Metrics are the part worth having.
            "analytics": {
                "enabled": True,
                "logging": {
                    "content": {"headers": False, "payload": False},
                    "phase": {"request": True, "response": True},
                    "mode": {"endpoint": True, "entrypoint": True},
                },
            },
            "flowExecution": {"mode": "DEFAULT", "matchRequired": False},
            "flows": [SECRET_FLOW, RATE_LIMIT_FLOW],
        },
        "plans": [plan_definition()],
    }


def plan_definition() -> dict:
    """A V4 API-key plan.

    `definitionVersion: "V4"` is load-bearing and its absence is not obvious
    from the failure. Without it the import returns 400 with

        Cannot invoke "AbstractPlan.getSecurity()" because the return value
        of "Plan.getPlanDefinitionV4()" is null

    which reads like a problem with the security block rather than a missing
    version discriminator on the plan. The rest of the fields below are
    likewise not optional decoration: the importer wants the full V4 plan
    shape, not a minimal one.
    """
    return {
        "definitionVersion": "V4",
        "name": PLAN_NAME,
        "description": "Free. Sign up in the developer portal and use the key straight away.",
        "security": {"type": "API_KEY", "configuration": {}},
        # AUTO, or every signup sits PENDING until somebody notices and
        # approves it by hand, which for a public API is the same as being
        # closed.
        "validation": "AUTO",
        "type": "API",
        "mode": "STANDARD",
        "status": "PUBLISHED",
        "order": 1,
        "characteristics": [],
        "commentRequired": False,
        "excludedGroups": [],
        "tags": [],
        "flows": [],
    }


# =============================================================================
# Deploy
# =============================================================================


def find_api() -> dict | None:
    r = session.get(f"{V2}/apis", params={"perPage": 100}, timeout=15)
    r.raise_for_status()
    for api in r.json().get("data", []):
        if api.get("name") == API_NAME:
            return api
    return None


def create() -> str:
    log(f"'{API_NAME}' not found, importing...")
    r = session.post(f"{V2}/apis/_import/definition", json=api_definition(), timeout=45)
    if r.status_code >= 400:
        log(f"  import failed ({r.status_code}): {r.text[:1500]}")
    r.raise_for_status()
    api_id = r.json()["id"]

    # Inline plans do not always come up published, and an API cannot start
    # without at least one that is.
    r = session.get(f"{V2}/apis/{api_id}/plans", timeout=15)
    r.raise_for_status()
    for plan in r.json().get("data", []):
        if (plan.get("status") or "").upper() != "PUBLISHED":
            session.post(f"{V2}/apis/{api_id}/plans/{plan['id']}/_publish", timeout=15).raise_for_status()
            log(f"  published plan '{plan.get('name')}'")

    session.post(f"{V2}/apis/{api_id}/_start", timeout=15).raise_for_status()
    publish_to_portal(api_id)
    log(f"  created and started -> {api_id}")
    return api_id


def publish_to_portal(api_id: str) -> None:
    """Move the API to lifecycleState PUBLISHED, which is what puts it in the
    developer portal catalogue.

    The import definition sets "lifecycleState": "PUBLISHED" inline and it is
    SILENTLY IGNORED - the API comes back CREATED. Nothing errors, the API
    works perfectly through the gateway, and it simply never appears in the
    portal. Every other PUBLIC API on the installation this was built against
    was sitting in CREATED for the same reason, which is why that portal's
    catalogue was empty rather than broken.

    Separate from `visibility: PUBLIC`: visibility decides who may see it,
    lifecycleState decides whether it is listed at all. Both are needed.
    """
    r = session.get(f"{V2}/apis/{api_id}", timeout=15)
    r.raise_for_status()
    api = r.json()
    if api.get("lifecycleState") == "PUBLISHED":
        return
    api["lifecycleState"] = "PUBLISHED"
    r = session.put(f"{V2}/apis/{api_id}", json=api, timeout=30)
    if r.status_code >= 400:
        log(f"  publish failed ({r.status_code}): {r.text[:500]}")
    r.raise_for_status()
    log("  published to the developer portal catalogue")


def update(api_id: str) -> None:
    """Update the definition in place, leaving plans and their subscriptions
    completely alone.

    The flows are then read back and checked. Some Gravitee versions drop the
    `flows` array on a PUT, which would silently disable every policy on the
    API - the secret injection included, so the receiver would start refusing
    the gateway rather than anything failing loudly at deploy time. Measured
    on 4.12.x and flows DO survive the PUT there, but the check stays: it
    costs one request and the failure it guards against is silent.
    """
    log(f"'{API_NAME}' exists ({api_id}), updating in place (plans untouched)...")
    definition = api_definition()["api"]

    r = session.get(f"{V2}/apis/{api_id}", timeout=15)
    r.raise_for_status()
    current = r.json()

    # Carry over the fields the API owns rather than the definition: ids,
    # timestamps, and anything the console has set that this script does not
    # model.
    merged = {**current, **definition, "id": api_id}
    r = session.put(f"{V2}/apis/{api_id}", json=merged, timeout=45)
    if r.status_code >= 400:
        log(f"  update failed ({r.status_code}): {r.text[:1500]}")
    r.raise_for_status()

    r = session.get(f"{V2}/apis/{api_id}", timeout=15)
    r.raise_for_status()
    flows = r.json().get("flows") or []
    if len(flows) != len(definition["flows"]):
        raise SystemExit(
            f"After the update the API carries {len(flows)} flow(s), expected "
            f"{len(definition['flows'])}. This version drops flows on PUT, which "
            "would leave the API deployed with no policies at all. Nothing further "
            "has been deployed. Fix by importing a new definition rather than "
            "updating, accepting that it recreates plans."
        )
    log(f"  updated, {len(flows)} flow(s) intact")
    publish_to_portal(api_id)


def deploy(api_id: str) -> None:
    """Push the definition to the data plane.

    Separate from _start on purpose: starting sets the API's state, deploying
    is what actually reaches the gateway. Skip this and every change sits in
    the console looking applied.
    """
    r = session.post(f"{V2}/apis/{api_id}/deployments", json={"deploymentLabel": "deploy.py"}, timeout=30)
    if r.status_code >= 400:
        log(f"  deployment failed ({r.status_code}): {r.text[:1000]}")
    r.raise_for_status()
    log("  deployed to the gateway")


def main() -> int:
    if not ADMIN_PASSWORD:
        print("GRAVITEE_ADMIN_PASSWORD is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2
    if not UPSTREAM_SECRET:
        print(
            "AIS_UPSTREAM_SECRET is not set. Without it the gateway would call the receiver "
            "with an empty secret and every request would be refused with 403.",
            file=sys.stderr,
        )
        return 2

    existing = find_api()
    if existing is None:
        api_id = create()
    else:
        api_id = existing["id"]
        update(api_id)

    deploy(api_id)

    gateway = os.environ.get("GRAVITEE_GATEWAY_URL", "https://gateway.prodger.cc").rstrip("/")
    log("")
    log(f"REST:  {gateway}{LISTENER_PATH}vessels")
    log(f"MCP:   {gateway}{LISTENER_PATH.rstrip('/')}{MCP_PATH}")
    log("Both need an API key from the developer portal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

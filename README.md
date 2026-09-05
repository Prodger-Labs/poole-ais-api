# Poole Harbour AIS

Live AIS vessel positions from a single home-built receiver in Poole, Dorset,
published as a REST API and as an MCP server.

One endpoint. Everything the receiver can hear **right now** — typically 25 to
40 vessels across Poole Bay and the Channel approaches. There is no history
behind it and nothing is stored.

```
REST   https://gateway.prodger.cc/poole-ais/vessels
MCP    https://gateway.prodger.cc/poole-ais/mcp
```

Both need a free API key. Sign up in the developer portal, subscribe to the
**Public** plan, and the key works immediately.

## The station

An RTL-SDR Blog V4 dongle and a hand-cut quarter-wave dipole in a loft,
running [AIS-catcher](https://github.com/jvde-github/AIS-catcher) on a
Raspberry Pi 4. About 21 nautical miles of usable range: the whole harbour,
Poole Bay, and the cross-Channel ferry routes out past Old Harry.

Reception is not a circle. The loft blocks the landward side almost
completely, so the coverage is roughly a 160° arc facing the water and
essentially nothing behind it. Vessels appear and disappear at the edges of
that arc rather than at a neat range limit.

The same receiver feeds AISHub, MarineTraffic, VesselFinder, ShipFinder and
BoatBeacon. This API is the same data, without the aggregator in the middle.

## Quick start

```bash
curl "https://gateway.prodger.cc/poole-ais/vessels?api-key=$KEY"
```

The key is a **query parameter, not a header**. This gateway does not accept
the header form and answers 401 to it. The upside is that the MCP endpoint
works with any client that takes a URL and nothing else; the cost is that the
key ends up in URLs and access logs, so treat it as a shared secret rather
than a credential.

```json
{
  "vessels": [
    {
      "mmsi": 227289000,
      "shipname": "BARFLEUR",
      "callsign": "FNIE",
      "country": "FR",
      "lat": 50.706169,
      "lon": -1.991,
      "speed": 1.8,
      "cog": 40,
      "heading": 131,
      "shiptype": 69,
      "status": 0,
      "destination": "GBPOO",
      "imo": 9007130,
      "draught": 5,
      "validated": 1,
      "last_signal_seconds": 2,
      "last_seen": "2026-09-04T21:14:05Z"
    }
  ],
  "count": 36,
  "generated_at": "2026-09-04T21:14:07Z"
}
```

The full schema is in [`openapi.yaml`](openapi.yaml).

## As an MCP server

One tool, `get_live_vessels`, taking no arguments. Point any MCP client at the
`/mcp` endpoint with the key in the URL.

```json
{
  "mcpServers": {
    "poole-harbour-ais": {
      "url": "https://gateway.prodger.cc/poole-ais/mcp?api-key=your-key"
    }
  }
}
```

Note the path has **no trailing slash**. `/mcp` reaches the MCP entrypoint;
`/mcp/` misses it, falls through to the plain HTTP proxy and answers 501.

Questions it can answer: what's on the water near Poole right now, whether the
Barfleur is running, how many boats are moving in the harbour, whether a
Coastguard SAR helicopter is up. It cannot answer anything historical, because
there is no history.

## Reading the data

A few things worth knowing before trusting a field.

**Everything comes from the vessel itself.** AIS is an open broadcast; each
vessel transmits its own identity and position. Completeness varies enormously
— a Class B set on a small yacht sends far less than a ferry does.

**Static data lags position data.** Name, callsign and destination are
broadcast much less often than position, so a vessel can show up with a
position and no name for several minutes after it is first heard. Roughly one
in ten entries has no name at any given moment.

**`destination` is free text typed by a human**, usually a UN/LOCODE by
convention but not by enforcement. It is frequently stale, occasionally a
joke, and should never be treated as authoritative.

**`last_signal_seconds` is an age, not a timestamp.** The receiver reports
seconds since the last message. `last_seen` is the same thing as an absolute
UTC time, computed when you ask. A vessel is dropped from the window about
half an hour after its last message, so the age is bounded by that.

**`validated: 0` means the decoder has not cross-confirmed that vessel's
messages.** Those entries are published rather than filtered out — an
unvalidated position is a real thing about the data, and quietly dropping
those vessels would be a subtler lie than showing the flag.

**MMSIs beginning `111` are search-and-rescue aircraft**, not vessels. They
carry AIS so they show up on ships' plotters during a search.

## What this deliberately does not publish

The receiver's own position, and anything that would reveal it.

The underlying decoder output carries the station's latitude and longitude at
the top level, and a range and bearing on every single vessel. Any two of
those trilaterate the aerial to within a few metres — and the aerial is in
somebody's loft. So the backend publishes an explicit allowlist of vessel
fields rather than filtering a blocklist, meaning a future decoder field
cannot leak by default, and the range and bearing figures are dropped
entirely.

If you want distance from the station, you will have to guess where the
station is.

## About the boats

These are other people's vessels, and most of them are small private craft
whose name and MMSI together identify an owner. That data is broadcast
publicly by the vessels themselves on an open channel, which is why it already
appears on every commercial vessel tracker — this API is not making anything
public that is not already.

Even so: if you are the owner of a vessel that appears here and would rather
it did not, open an issue and it will be filtered out. No argument, no form.

## Rate limits

Five requests per second per key. The backend is a Raspberry Pi on a domestic
broadband connection; the limit exists to protect it rather than to ration
anything.

The API is briefly unavailable around 04:00 UTC while the gateway updates.

## Deploying it

Only relevant if you are running your own copy.

```bash
cp .env.example .env    # fill in credentials
make deploy             # publish or update the API on Gravitee
make verify             # prove it works, as REST and as MCP
```

`deploy.py` is idempotent by design: it creates the API once and updates it in
place afterwards, and never touches a plan that already exists. Delete and
reimport would be simpler, and would also cancel every subscription and revoke
every key that anyone had signed up for.

`make verify` is the part worth reading. An MCP endpoint returns HTTP 200 on a
JSON-RPC error, so it asserts the handshake body rather than the status code,
requires the tool list to be non-empty and to contain the expected tool, and
finishes by driving a real call and requiring actual vessels back. It also
asserts that `/mcp/` does *not* answer MCP, so that if a future version starts
matching the trailing slash it says so rather than quietly changing which
server answers.

## Licence

Code: MIT. Data: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) —
use it for whatever you like, credit the station.

# CTF Instancer (MVP)

One challenge, one button, one instance per browser session. Creating an
instance hands out four things nobody else has:

* a **container**,
* a **subnet** — its own internal /24, with no gateway,
* a **port** inside that subnet,
* a **key**.

Nothing is published to the host. The only way to an instance is the proxy: one
container, one port, and a key that says which instance you get.

```
                    :5000  the web UI, hands out keys
                    :1337  the proxy, the only way in
                      |
   player  ───────────┴──────────►  [ proxy ]  ── key? ──►  [ instancer ]
                                        │                    (docker.sock)
                     ctf-control 10.239.0.0/24  ─────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │ internal, no gateway    │ internal, no gateway    │
        10.240.0.0/24              10.240.1.0/24            10.240.2.0/24
        [ instance :30000 ]        [ instance :30001 ]      [ instance :30002 ]
```

```
challenge/            the challenge (Dockerfile + sources), built once at startup
instancer-core/       the instancer: app.py + templates/index.html
proxy-core/           the proxy: proxy.py (stdlib only)
Dockerfile.instancer  image for the instancer
Dockerfile.proxy      image for the proxy
docker-compose.yml    runs both, on the control network
tests/                unit tests (fake Docker, fake instances) + integration tests
```

## Running

```sh
docker compose up --build
```

Then open <http://localhost:5000>, press START, and you get an address and a key:

```
netcat   nc localhost 1337     then paste the key at the "key: " prompt
http     http://localhost:1337/<key>/
```

At startup the instancer builds `challenge/` into `ctf-challenge:latest` and logs
`challenge image built`. Instances are never rebuilt — START only runs a
container from that image. **After editing the challenge, set `FORCE_BUILD=1`**:
a stale image is reused silently, and one that predates the `CHAL_PORT` contract
below will listen on the wrong port and never answer.

To run it without Docker Compose (talks to your local Docker daemon, and needs
the proxy container already running):

```sh
pip install -r instancer-core/requirements.txt
CHALLENGE_DIR=./challenge PROXY_TOKEN=... python instancer-core/app.py
PROXY_BIND=127.0.0.1 PROXY_TOKEN=... python proxy-core/proxy.py
```

## Configuration

Everything is environment variables (defaults in brackets). The instancer:

| Variable | Default | Meaning |
| --- | --- | --- |
| `INSTANCE_PORT_MIN` | `30000` | first port an instance may be given |
| `INSTANCE_PORT_MAX` | `30100` | last port an instance may be given |
| `CHALLENGE_DIR` | `/challenge` | build context of the challenge |
| `CHALLENGE_IMAGE` | `ctf-challenge:latest` | tag built at startup |
| `FORCE_BUILD` | unset | rebuild the challenge image even if it exists |
| `PORT_ENV` | `CHAL_PORT` | the variable the instance's port is passed in |
| `MODE` | `http` | `http` shows a clickable link; `netcat` shows an `nc` command |
| `SUBNET_POOL` | `10.100.0.0/16` | pool the per-instance subnets are carved from |
| `SUBNET_PREFIX` | `24` | size of each instance's subnet (a /24 = 256 subnets from a /16) |
| `MEM_LIMIT` | `512m` | memory ceiling per instance (empty = no limit) |
| `PIDS_LIMIT` | `256` | process ceiling per instance (empty or `0` = no limit) |
| `DEFAULT_TTL` | `3600` | instance lifetime in seconds when `/create` gives no `ttl` |
| `MAX_TTL` | `86400` | ceiling a requested `ttl` is clamped to |
| `CLEANUP_INTERVAL` | `10` | how often the background reaper checks for expiry (seconds) |
| `LISTEN_PORT` | `5000` | port of the instancer web UI |
| `SECRET_KEY` | random | signs the session cookies that carry instance ownership |
| `PROXY_CONTAINER` | `ctf-proxy` | container the instancer attaches to every instance network |
| `PROXY_PORT` | `1337` | proxy port shown to players |
| `PROXY_HOST` | unset | proxy hostname shown to players (unset = the host serving the UI) |
| `PROXY_TOKEN` | unset | shared secret the proxy presents when resolving a key |

And the proxy:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROXY_BIND` | `0.0.0.0` | **the one address to answer on** — see below |
| `PROXY_PORT` | `1337` | the single port players connect to |
| `INSTANCER_URL` | `http://instancer:5000` | where keys are resolved |
| `PROXY_TOKEN` | unset | must match the instancer's |
| `MODE` | `http` | `http` reverse-proxies; `netcat` splices raw TCP |
| `CONNECT_TIMEOUT` | `5` | seconds to reach the instancer or an instance |
| `IDLE_TIMEOUT` | `300` | seconds a connection may sit silent |

`SECRET_KEY` decides who owns what: leave it unset and a fresh key is generated
at startup (logged as a warning), which means every session — and therefore
every claim on a running container — is dropped when the instancer restarts.
Set it (a `.env` file next to `docker-compose.yml` works) in anything but a
throwaway run. Do the same for `PROXY_TOKEN`: the shipped default is a
placeholder, and an unset one makes the instancer refuse *every* key lookup, so
nobody gets through the proxy at all.

How many instances can run at once is decided by the port range: one each, so
`30000-30100` allows 101. These ports are not host ports — each lives inside its
own instance's network namespace, so they collide with nothing on the machine.
When the range runs out, `POST /create` fails with `no free instance port` and
starts nothing.

## Challenge requirements

`challenge/` must contain a `Dockerfile` that builds a **self-contained** image
(everything baked in — no volume mounts needed at runtime) which

* listens on `0.0.0.0:$CHAL_PORT` when started with no arguments,
* needs no volumes, no extra capabilities, and no published port.

`CHAL_PORT` is the one contract: the instancer gives every instance a port of its
own and passes it in as that variable. Bake a sensible default into the image
(`ENV CHAL_PORT=...`) so it still runs by hand. A challenge that ignores the
variable listens on the wrong port, and the proxy will report `502` / `no
instance` for every player.

The bundled challenge is a pwn service: `xinetd` serves a small binary over raw
TCP. `xinetd` wants the port in a config file, so `entrypoint.sh` renders
`xinetd.template` with `$CHAL_PORT` before starting it — the same two lines work
for most servers that take a port on the command line. Its `docker-compose.yml`
is only for local development; the instancer just `docker build`s the
`Dockerfile`.

Set `MODE` to how players reach it (`http` for web challenges, `netcat` for
raw-TCP / pwn) — for both the instancer and the proxy. Nothing else is read: no
manifest, no name, no config file.

## Isolation

This is the part worth reading twice. An instance is a container running
attacker-supplied code, by design.

**The instance's network has no gateway.** It is created `internal` *and* with
`com.docker.network.bridge.gateway_mode_ipv4=isolated`, so Docker leaves the
bridge without an address. Inside an instance there is exactly one route:

```
Iface   Destination  Gateway
eth0    0001F00A     00000000     <- its own /24, and nothing else
```

No default route means no host, no `172.17.0.1`, no control network, no
internet, no other instance — not filtered, *unroutable*. (Docker < 28 rejects
the option; the instancer falls back to a plain internal network and logs a
warning. That still blocks routing, but leaves the bridge's gateway address —
i.e. the host — reachable from the instance. Upgrade.)

**The proxy is the one thing on that subnet, and it does not answer there.** The
instancer attaches the proxy container to each instance network, so the proxy is
multi-homed: it can dial the instance. The proxy binds `PROXY_BIND`, its address
on the control network, and *never* `0.0.0.0` — a wildcard socket would also
accept on every instance network it is later attached to, which would let a
pwned instance connect to the proxy and ask for somebody else's key. Keep
`PROXY_BIND` set to the proxy's control-network address; the proxy logs a
warning if it is a wildcard.

**Nothing is published.** Instances get no host port at all, so the proxy is not
just the intended path, it is the only one.

**Keys are the only credential.** 16 random bytes, stored as a container label,
compared in constant time, and only ever resolved for a *running* container.
Destroy an instance and its key is dead. Turning a key into an address is the
whole authority in the system, so `/internal/route/<key>` demands the shared
`PROXY_TOKEN` and answers an indistinguishable `404` for a bad token, an unknown
key, and a stopped instance alike.

**Blast radius.** Each instance gets `MEM_LIMIT` and `PIDS_LIMIT` so one player's
fork bomb is one player's problem. Capabilities are left alone on purpose: pwn
challenges routinely need setuid helpers, and silently breaking them would be
worse than the marginal hardening.

## API

A running instance is reported as

```json
{"running": true, "mode": "netcat", "key": "9ce0…fea9", "proxy_host": null,
 "proxy_port": 1337, "port": 30001, "expires_at": 1787020896, "remaining_time": 118}
```

`key` and `proxy_host`/`proxy_port` are what a player needs; `port` is the
instance's port on its own subnet, reachable only by the proxy. `proxy_host` is
`null` unless `PROXY_HOST` is set, meaning "the host serving this page".

| Route | Response |
| --- | --- |
| `GET /` | the web UI |
| `GET /status` | your instance, or `{"running": false, ...}` |
| `POST /create` | your instance; returns the existing one if you already have it, `500` + `{"running": false, "error": ...}` on failure |
| `POST /destroy` | `{"running": false, ...}`, also when you had nothing running |
| `GET /internal/route/<key>` | `{"host": ..., "port": ...}` for the proxy; `404` without a matching `X-Proxy-Token` |

`POST /create` accepts an optional JSON body `{"ttl": 3600}` — the instance
lifetime in seconds (defaults to `DEFAULT_TTL`, clamped to `MAX_TTL`). No body is
fine; the START button sends none.

Every player-facing route only ever talks about the caller's own instance.
`GET /` puts a random id in a Flask session cookie — minted there rather than in
`/create`, so two fast clicks on START cannot race into two identities. That id
names both the container (`ctf-instance-<id>`) and the network
(`ctf-network-<id>`). Somebody else's `/status` reports nothing and their
`/destroy` removes nothing. Users never send an image, port, subnet, command, or
anything else Docker acts on.

## How it works

**Create.** Under a `threading.Lock` (so concurrent clicks can't double-create),
the instancer picks a free port, a free `/24` out of `SUBNET_POOL`, and a fresh
key; creates `ctf-network-<id>` as an internal, gatewayless bridge; attaches the
proxy to it; then creates and starts `ctf-instance-<id>` on it with the port in
`$CHAL_PORT`. If anything fails — including the proxy being missing — the
half-built container and network are torn down before returning `500`, so the
port, subnet and key stay free.

**Connect.** The player sends the key to the proxy: as the first line in netcat
mode, or as the first path segment (`/<key>/…`) in http mode, where the answer
also carries a `ctf_key` cookie so the challenge's own absolute links keep
routing. The proxy asks the instancer where that key leads, opens a connection to
`<instance ip>:<instance port>`, and splices the two together. It keeps no
routing table of its own, so a destroyed instance is unreachable immediately. In
http mode each exchange gets its own upstream connection (`Connection: close`),
because keep-alive would let a second request ride in on the first request's key.

**TTL.** `expires_at = now + ttl` is stamped on the container as a label. A
background thread wakes every `CLEANUP_INTERVAL` seconds and destroys any
instance past its `expires_at` (container **and** network), and prunes any
`ctf-network-*` whose container is gone. Removing a network means detaching the
proxy first — Docker refuses to remove a network that still has an endpoint.

**Persistence.** Docker is the only state store — there is no database. All
metadata lives in labels (`ctf.owner`, `ctf.expires_at`, `ctf.subnet`,
`ctf.port`, `ctf.key`) on the container and network, so restarting the instancer
re-discovers running instances, their TTLs *and* their keys instead of
duplicating them. The proxy is stateless for the same reason: it re-reads every
key from the instancer. Reconnecting a *browser* to its instance across a restart
additionally needs a stable `SECRET_KEY`.

**Recycling.** Nothing is tracked in memory: `pick_port()` reads the ports of
live instances and `pick_subnet()` the subnets of live networks, so a destroyed
or reaped instance's port and /24 are free again the moment it is gone.

## Tests

```sh
pip install pytest
python -m pytest tests -rs
```

`tests/test_instancer.py` runs against a fake Docker daemon (no Docker needed);
two Flask test clients stand in for two users. `tests/test_proxy.py` runs the
real proxy over real sockets against fake instances — key handling, path/cookie
routing, hangups, and the parsing helpers. `tests/test_docker_integration.py`
builds the real challenge and proxy images and drives real containers and
networks: a key really carries a player through the proxy to the pwn service, a
destroyed instance's key really stops working, and an instance really cannot
reach the proxy, the control network, or a default route. It skips itself when
no Docker daemon is reachable.

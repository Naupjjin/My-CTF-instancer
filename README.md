# CTF Instancer (MVP)

One challenge, one button, one instance per browser session — each on its own
Docker network with a unique subnet and a TTL after which it is reaped.

```
challenge/            the challenge (Dockerfile + sources), built once at startup
instancer-core/       the whole instancer: app.py + templates/index.html
Dockerfile.instancer  image for the instancer itself
docker-compose.yml    runs the instancer
tests/                unit tests (fake Docker) + integration tests (real Docker)
```

## Running

```sh
docker compose up --build
```

Then open <http://localhost:5000>.

At startup the instancer builds `challenge/` into `ctf-challenge:latest` and logs
`challenge image built`. Instances are never rebuilt — `Start Instance` only runs
a container from that image.

The instancer uses `network_mode: host` and the host Docker socket, so the ports
it hands out are real host ports and its own bind checks see the same namespace.
(Host networking is a Linux feature; on Docker Desktop replace it with
`ports: ["5000:5000"]` and accept that the bind check only sees the container.)

To run it without Docker (talks to your local Docker daemon):

```sh
pip install -r instancer-core/requirements.txt
CHALLENGE_DIR=./challenge python instancer-core/app.py
```

## Configuration

All configuration is environment variables (defaults in brackets):

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT_MIN` | `30000` | first host port the instancer may use |
| `PORT_MAX` | `30100` | last host port the instancer may use |
| `CHALLENGE_DIR` | `/challenge` | build context of the challenge |
| `CHALLENGE_IMAGE` | `ctf-challenge:latest` | tag built at startup |
| `CONTAINER_PORT` | `8080` | port the challenge listens on inside the container |
| `MODE` | `http` | `http` shows a clickable `http://host:port`; `netcat` shows `nc host port` |
| `SUBNET_POOL` | `10.100.0.0/16` | pool the per-instance subnets are carved from |
| `SUBNET_PREFIX` | `24` | size of each instance's subnet (a /24 = 256 subnets from a /16) |
| `DEFAULT_TTL` | `3600` | instance lifetime in seconds when `/create` gives no `ttl` |
| `MAX_TTL` | `86400` | ceiling a requested `ttl` is clamped to |
| `CLEANUP_INTERVAL` | `10` | how often the background reaper checks for expiry (seconds) |
| `LISTEN_PORT` | `5000` | port of the instancer web UI |
| `SECRET_KEY` | random | signs the session cookies that carry instance ownership |

Change the port range in `docker-compose.yml`:

```yaml
    environment:
      PORT_MIN: 32000
      PORT_MAX: 32050
```

`SECRET_KEY` decides who owns what: leave it unset and a fresh key is generated
at startup (logged as a warning), which means every session — and therefore
every claim on a running container — is dropped when the instancer restarts.
Set it (a `.env` file next to `docker-compose.yml` works) in anything but a
throwaway run.

How many instances can run at once is decided by the port range: one port each,
so `30000-30100` allows 101. A port is only used when Docker reports no container publishing it **and** a
real `bind()` on the host succeeds, so ports taken by other processes or
containers are skipped. If every port in the range is taken, `POST /create`
fails with `no free host port` and starts nothing.

## Challenge requirements

`challenge/` must contain a `Dockerfile` that builds a **self-contained** image
(everything baked in — no volume mounts needed at runtime) which

* listens on `0.0.0.0:<CONTAINER_PORT>` when started with no arguments,
* needs no volumes, no extra capabilities, and no other published port.

Set `CONTAINER_PORT` to that port and `MODE` to how players reach it (`http` for
web challenges, `netcat` for raw-TCP / pwn). Nothing else is read: no manifest,
no name, no config file.

The bundled challenge is a pwn service — `xinetd` serves a small binary over TCP
on **41240**, so the shipped `docker-compose.yml` sets `CONTAINER_PORT: 41240`
and `MODE: netcat`. Its `docker-compose.yml` is only for local development; the
instancer just `docker build`s the `Dockerfile`, which bakes `share/` and the
`xinetd` config into the image.

## API

A running instance is reported as
`{"running": true, "port": 30000, "mode": "netcat", "expires_at": 1787020896, "remaining_time": 118}`
(`expires_at` is a Unix timestamp, `remaining_time` is seconds left). Every
response also carries `mode`.

| Route | Response |
| --- | --- |
| `GET /` | the web UI |
| `GET /status` | your instance, or `{"running": false, "mode": ...}` |
| `POST /create` | your instance; returns the existing one if you already have it, `500` + `{"running": false, "error": ...}` on failure |
| `POST /destroy` | `{"running": false, "mode": ...}`, also when you had nothing running |

`POST /create` accepts an optional JSON body `{"ttl": 3600}` — the instance
lifetime in seconds (defaults to `DEFAULT_TTL`, clamped to `MAX_TTL`). No body is
fine; the Start button sends the value from the TTL box.

Every route only ever talks about the caller's own instance. `GET /` puts a
random id in a Flask session cookie — minted there rather than in `/create`, so
two fast clicks on Start cannot race into two identities. That id names both the
container (`ctf-instance-<id>`) and the network (`ctf-network-<id>`). Somebody
else's `/status` reports nothing and their `/destroy` removes nothing; a request
with no session sees nothing either. Users never send an image, port, subnet,
command, or anything else Docker acts on.

## How it works

**Create.** Under a `threading.Lock` (so concurrent clicks can't double-create),
the instancer picks a free host port (checked against Docker's published ports
*and* a real `bind()`), picks a free `/24` out of `SUBNET_POOL` (checked against
every existing Docker network), creates `ctf-network-<id>` with that subnet,
then creates and starts `ctf-instance-<id>` attached to it and publishing the
host port. If anything fails, the half-built container and network are torn down
before returning `500`, so the port and subnet stay free.

**TTL.** `expires_at = now + ttl` is stamped on the container as a label. A
background thread wakes every `CLEANUP_INTERVAL` seconds and destroys any
instance past its `expires_at` (container **and** network), and prunes any
`ctf-network-*` whose container is gone.

**Persistence.** Docker is the only state store — there is no database. All
metadata lives in labels (`ctf.owner`, `ctf.expires_at`, `ctf.subnet`) on the
container and network, so restarting the instancer re-discovers running
instances (and their TTLs) instead of duplicating them. Reconnecting a *browser*
to its instance across a restart additionally needs a stable `SECRET_KEY` (an
unset one is regenerated, invalidating existing session cookies — hence the
startup warning).

**Subnet recycling.** Nothing tracks subnets in memory: `pick_subnet()` reads
the subnets of all live Docker networks each time, so a destroyed or reaped
instance's `/24` is free again the moment its network is removed.

## Tests

```sh
pip install pytest
python -m pytest tests -rs
```

`tests/test_instancer.py` runs against a fake Docker daemon (no Docker needed).
Two Flask test clients stand in for two users.
`tests/test_docker_integration.py` builds the real challenge image and drives
real containers and networks — create/adopt/destroy, distinct subnets for two
sessions, the TTL reaper, and connecting to the pwn service over TCP. It skips
itself when no Docker daemon is reachable, and the one connectivity check skips
(rather than fails) on hosts that block host↔docker-bridge traffic.

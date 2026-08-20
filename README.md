# SpawnZero

A CTF instancer. Many challenges, one service, one instance of each per browser
session — or per CTFd account, or per CTFd team, if you turn `CTFD_VERIFY` on.
Creating an instance hands out four things nobody else has:

* a **container**,
* a **subnet** — its own internal /24, with no gateway,
* a **port** inside that subnet,
* a **key**.

Nothing is published to the host. The only way to an instance is its challenge's
proxy: one container, one port, and a key that says which instance you get.

```
                    :5000  the web UI, hands out keys
                      |
   player  ───────────┴───────────────────────────────►  [ instancer ]
                                                           (docker.sock)
            :1337  special-love          :1338  cookie-jar        │
              │                            │                      │
        [ proxy ] ── key? ──┐         [ proxy ] ── key? ───────────┤
              │             └──────────────┼──────────────────────┘
              │       ctf-control 10.239.0.0/24
     ┌────────┴────────┐            ┌──────┴──────────┐
     │ internal,       │            │ internal,       │
     │ no gateway      │            │ no gateway      │
  10.240.0.0/24   10.240.1.0/24  10.241.0.0/24   10.241.1.0/24
  [ instance ]    [ instance ]   [ instance ]    [ instance ]
  └────── 10.240.0.0/16 ──────┘  └────── 10.241.0.0/16 ──────┘
           subnet_pool                    subnet_pool
```

```
challenges/           one directory per challenge: Dockerfile + config.yml
instancer-core/       the instancer: app.py + templates/
proxy-core/           the proxy: proxy.py (stdlib only) + its Dockerfile
Dockerfile.instancer  image for the instancer
docker-compose.yml    runs the instancer; the proxies are its own doing
tests/                unit tests (fake Docker, fake instances) + integration tests
```

## Running

```sh
docker compose up --build
```

Then open <http://localhost:5000>, pick a challenge, press START, and you get an
address and a key:

```
netcat   nc localhost 1337     then paste the key at the "key: " prompt
http     http://localhost:1338/<key>/
```

Two challenges ship with it: `special-love`, a pwn service over raw TCP, and
`cookie-jar`, a web challenge. They are also the worked examples of the two
modes.

At startup the instancer builds `proxy-core/` into `ctf-proxy:latest` and each
`challenges/<id>/` into `ctf-challenge-<id>:latest`, then raises one proxy
container per challenge. Instances are never rebuilt — START only runs a
container from an image that is already there. **After editing a challenge, set
`FORCE_BUILD=1`**: a stale image is reused silently, and one that predates the
`CHAL_PORT` contract below will listen on the wrong port and never answer.

## Adding a challenge

Two things, and a restart:

```sh
mkdir challenges/my-challenge          # the directory name is the id
$EDITOR challenges/my-challenge/Dockerfile
$EDITOR challenges/my-challenge/config.yml
docker compose restart instancer
```

Nothing else in the system is told that challenges exist — not
`docker-compose.yml`, not the proxy, not the page. `examples/nc-chal/` is a
directory to copy.

The id has to be lowercase letters, digits and dashes: it goes in URLs and in the
name of every container and network the challenge is made of. A challenge with no
`Dockerfile`, no `config.yml`, or no `proxy_port` is skipped with a line in the
log, and the others come up regardless — one broken challenge is not an outage.

## Cleaning up

```sh
docker compose down
```

takes the instances *and the proxies* with it. Compose does not know about any of
them — the instancer creates them at runtime over the Docker socket — so the
instancer destroys them itself when it is asked to stop, along with the instance
networks. Instances go first and proxies last, so each proxy is still there to be
detached from the networks it is on, and `stop_grace_period: 60s` gives a full
house time to come down.

A *crash* is the other case, and there it deliberately does not happen: instances
survive, and a restarted instancer re-adopts them — and their proxies — from
their labels instead of leaving players stranded. `REAP_ON_SHUTDOWN=0` extends
that to deliberate stops too — worth setting mid-event if you want `docker
compose restart` to be a non-event for players.

If any of it ever outlives its instancer (it was killed, or the daemon restarted
under it), everything is labelled, so clearing it is exact:

```sh
docker rm -f $(docker ps -aq --filter label=ctf.chal) 2>/dev/null
docker network rm $(docker network ls -q --filter label=ctf.chal) 2>/dev/null
```

Take the stack down first: a network with a proxy still attached refuses to go
with `has active endpoints`. Left alone, instances expire within their
challenge's `ttl` anyway — but only while an instancer is running to reap them.

## Configuration

There are two places to configure this, and which one a setting belongs in
follows from how many of the thing there are. A challenge has its own file,
because there are many challenges. The deployment has environment variables,
because there is one deployment — and that is the whole of it: the instancer has
no config file of its own.

### A challenge — `challenges/<id>/config.yml`

Everything about one challenge, next to its Dockerfile, because everything here
is per challenge by nature: two challenges are two proxies on two ports, and a
web app deserves a different ceiling than a pwn box.

```yaml
name: Special Love          # the heading; the id is used if it is missing
author: naup
type: pwn

mode: netcat                # netcat | http -- how players reach it
proxy_port: 1337            # the host port its proxy has to itself

ttl: 3600                   # instance lifetime, seconds

subnet_pool: 10.240.0.0/16  # where its instances live: one /24 each
subnet_prefix: 24
instance_ports: 30000-30100 # one port each, so: up to 101 at once
max_instances: 0            # ...or a flat cap, if you would rather say it outright

mem_limit: 512m             # blast radius of one instance
pids_limit: 256             # processes + threads at once; 0 for no limit
proxy_host:                 # optional: a hostname just for this challenge
```

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | the directory name | the heading on the challenge's page |
| `author` | empty | shown under the heading |
| `type` | empty | shown under the heading (`pwn`, `web`, …) |
| `mode` | `http` | `http` shows a clickable link; `netcat` shows an `nc` command |
| `proxy_port` | **required** | the host port this challenge's proxy answers on |
| `ttl` | `3600` | instance lifetime in seconds — the same for every instance of it |
| `subnet_pool` | `10.240.0.0/16` | the address space its instances are carved out of |
| `subnet_prefix` | `24` | size of each instance's subnet (a /24 out of a /16 = 256 instances) |
| `instance_ports` | `30000-30100` | the port range its instances draw from — and so how many can be up at once |
| `max_instances` | `0` | a flat ceiling on how many of it may be up at once; `0` leaves that to the range and the pool |
| `mem_limit` | `512m` | memory ceiling per instance (`''` = no limit) |
| `pids_limit` | `256` | tasks — processes *and* threads — one instance may have at once (`0` = no limit) |
| `proxy_host` | `PROXY_HOST` | proxy hostname shown to players (unset = the host serving the UI) |

`proxy_port` is the one setting that must not collide with another challenge's:
it is a real port on the host. Two challenges asking for the same one is caught
at startup, and the second is refused with a line naming the first.

The other three are worth understanding, because two of them look alike and are
not:

* **`subnet_pool` is real address space.** Give each challenge its own. Two
  pointed at the same pool is allowed — every pick is checked against every
  network on the daemon, so nothing is ever handed out twice — they just share
  the space and run out sooner.
* **`instance_ports` is not shared space at all.** An instance port exists only
  inside that instance's own network namespace, so it collides with nothing on
  the host and nothing in another challenge; two challenges can both hand out
  `30000` and did, in a test. What the range really decides is **how many
  instances of this challenge can be up at once** — one port each, so
  `30000-30100` is 101. Write it as a range, or as a single port for a cap of
  one.
* **`max_instances` is the ceiling you meant.** The other two cap concurrency as
  a side effect of address arithmetic; this one says the number. Set it when what
  you want to limit is players — or when the challenge is heavy enough that the
  machine, not the config, is the real limit. `0` means it never binds.

Whichever of the three is lowest is the real ceiling, and that is the number the
page prints as *up to N at once*: a /16 in /24s is 256, `30000-30100` is 101, so
those two shipped challenges run 101 each until a `max_instances` says otherwise.
A `max_instances` set above what the pools allow is not an error — it just never
binds, and startup says so rather than letting you believe you raised a limit you
did not.

When any of them runs out, `POST /api/<chal>/create` returns `503` and
`{"error": "no free instance right now, try again in a moment"}`, and starts
nothing. A player who already has an instance of that challenge still gets it
back — the cap turns away new instances, not the people holding one.

### The deployment — environment variables

Defaults in brackets. The instancer:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHALLENGES_DIR` | `/challenges` | where the challenge directories are |
| `CHALLENGE_IMAGE` | `ctf-challenge-{chal}:latest` | tag built for each challenge |
| `PROXY_DIR` | `/proxy-core` | build context of the proxy image |
| `PROXY_IMAGE` | `ctf-proxy:latest` | tag every proxy runs |
| `FORCE_BUILD` | unset | rebuild the images even if they exist |
| `PORT_ENV` | `CHAL_PORT` | the variable the instance's port is passed in |
| `CONTROL_NETWORK` | `ctf-control` | the network the instancer and the proxies share |
| `INSTANCER_URL` | `http://instancer:<LISTEN_PORT>` | where the proxies reach this process |
| `CLEANUP_INTERVAL` | `10` | how often the background reaper checks for expiry (seconds) |
| `REAP_ON_SHUTDOWN` | `1` | destroy every instance and proxy when the instancer is asked to stop; `0` leaves them for the next start to adopt |
| `LISTEN_PORT` | `5000` | port of the instancer web UI |
| `INSTANCER_NAME` | `SpawnZero` | what this deployment calls itself: titlebar, boot banner, first line of the log |
| `SECRET_KEY` | random | signs the session cookies that carry instance ownership |
| `PROXY_HOST` | unset | default proxy hostname shown to players (unset = the host serving the UI) |
| `PROXY_TOKEN` | unset | the shared secret every proxy's own token is derived from |
| `CTFD_VERIFY` | `n` | `y` makes a player a CTFd account instead of a browser session — see below |
| `CTFD_URL` | unset | the CTFd to ask whose token it is; required when the above is on |
| `CTFD_SCOPE` | `user` | `team` gives one instance per CTFd *team* instead of per account |
| `CTFD_TIMEOUT` | `5` | seconds to wait for CTFd to answer |

Only seven of these are written in `docker-compose.yml` — the two secrets, the UI
port, the name, `PROXY_HOST`, and the two CTFd settings. The rest have defaults
that are already right for the compose layout, and nothing about a *challenge* is
there at all: there is one environment and many challenges, so a per-challenge
value has nowhere to live except with the challenge.

And the proxy — all of it handed over by the instancer, which creates the
container:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROXY_BIND` | `0.0.0.0` | **the one address to answer on** — see below |
| `PROXY_PORT` | `1337` | the single port players connect to |
| `PROXY_CHAL` | unset | the challenge whose keys it may resolve, and no other |
| `INSTANCER_URL` | `http://instancer:5000` | where keys are resolved |
| `PROXY_TOKEN` | unset | this challenge's token; must match what the instancer derives |
| `MODE` | `http` | `http` reverse-proxies; `netcat` splices raw TCP |
| `CONNECT_TIMEOUT` | `5` | seconds to reach the instancer or an instance |
| `IDLE_TIMEOUT` | `300` | seconds a connection may sit silent |

`SECRET_KEY` decides who owns what: leave it unset and a fresh key is generated
at startup (logged as a warning), which means every session — and therefore
every claim on a running container — is dropped when the instancer restarts.
Set it (a `.env` file next to `docker-compose.yml` works) in anything but a
throwaway run. Do the same for `PROXY_TOKEN`: the shipped default is a
placeholder, and an unset one makes the instancer refuse *every* key lookup, so
nobody gets through any proxy at all.

### CTFd — one account (or one team), one instance

Off by default: a player is a browser session, and clearing cookies is a new
player. That is fine for a practice box and not fine for a scored event, so:

```yaml
    environment:
      CTFD_VERIFY: "y"
      CTFD_URL: "https://ctf.example.com"
```

or, in the `.env` next to `docker-compose.yml`:

```sh
CTFD_VERIFY=y
CTFD_URL=https://ctf.example.com
```

With it on, START asks for a CTFd **API token** first — the one CTFd hands out
under *Settings → Access Tokens*, `ctfd_` and 64 hex. The instancer spends it on
the one question CTFd can answer about it:

```
GET <CTFD_URL>/api/v1/users/me
Authorization: Token ctfd_…
```

A `200` is both "this token is real" and "this is whose", because CTFd answers
that route for the account the token belongs to and for nobody else. The account
id it comes back with is the player: hashed to sixteen hex, and used everywhere a
session id was used before, so the container is `ctf-instance-<chal>-<account>`
and **one token holds one instance of each challenge**. A second browser, a
cleared cookie or a shared token all land on the same instance and hand it back
rather than opening another. (One *per challenge*, not one in total — the same
account still gets one of every challenge at once, each on its own `ttl`.)

**Team CTFs want `CTFD_SCOPE=team`.** Left at `user`, four teammates verify four
tokens and take four instances of the same challenge, which is usually not what a
team event means by one. With it set, the owner is the `team_id` that came back
with the account, so the whole team shares one instance and one `ttl` per
challenge — whoever presses START first opens it, and the others land on it. An
account that has not joined a team has no identity to be: it is turned away with
"join a team in CTFd first" rather than quietly given one of its own.

The hash is plain SHA-256 of the account (or team) id, deliberately with nothing
secret and nothing per-process in it: an owner id that moved when the instancer restarted
would hand every player a second instance every time it came back up. It is a
name, never a credential — the key is still the only credential, and there is a
fresh one per instance.

The token itself is read once, checked, and dropped; what the session cookie
carries afterwards is the account, never the token. Verifying a second token in
the same browser is a second player sitting down, not a second instance: what the
first one had stays theirs and runs out its `ttl`.

Failures say only what a player can act on. A token of the wrong shape is refused
without troubling CTFd at all; one CTFd does not know gets `403` + "CTFd does not
know that token". A CTFd that cannot be reached — or answers something that is
not CTFd — is *not* a refusal: it gets `503` + "try again in a moment", and the
reason lands in the log, because an event whose CTFd blinks must not hand every
player a fresh identity. `CTFD_VERIFY=y` with no `CTFD_URL` fails closed, loudly,
at startup and on every attempt: with nowhere to ask, nothing starts.

Two things worth knowing before an event. The instancer reaches CTFd over the
control network, so CTFd has to be reachable *from the instancer container* — a
CTFd on the same host is `http://<host-ip>:8000`, not `localhost`. And turning
this on mid-event is safe: a session id minted while it was off is a cookie, not
an account, and does not become one because the setting changed underneath it —
those players are asked for a token like everybody else. Instances they already
had keep running under the old owner id and expire on schedule.

## Challenge requirements

`challenges/<id>/` must contain a `config.yml` (above) and a `Dockerfile` that
builds a **self-contained** image (everything baked in — no volume mounts needed
at runtime) which

* listens on `0.0.0.0:$CHAL_PORT` when started with no arguments,
* needs no volumes, no extra capabilities, and no published port.

`CHAL_PORT` is the one contract: the instancer gives every instance a port of its
own and passes it in as that variable. Bake a sensible default into the image
(`ENV CHAL_PORT=...`) so it still runs by hand. A challenge that ignores the
variable listens on the wrong port, and its proxy will report `502` / `no
instance` for every player.

`challenges/special-love` is a pwn service: `xinetd` serves a small binary over
raw TCP, and because `xinetd` wants the port in a config file, `entrypoint.sh`
renders `xinetd.template` with `$CHAL_PORT` before starting it — the same two
lines work for most servers that take a port on the command line.
`challenges/cookie-jar` is a web challenge in the standard library, reading
`CHAL_PORT` straight out of the environment.

Each has a `docker-compose.yml` for developing it on its own:

```sh
docker compose -f challenges/special-love/docker-compose.yml up --build
nc localhost 30000
```

It deliberately runs the image with **no volumes** and on a port that is **not**
the image's default, because that is how SpawnZero runs it. A challenge that
ignores `$CHAL_PORT`, or that only works with your source bind-mounted over it,
fails there instead of in front of players. SpawnZero itself never reads that
file — it just `docker build`s the `Dockerfile`.

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
internet, no other instance, and no instance of another challenge — not
filtered, *unroutable*. (Docker < 28 rejects the option; the instancer falls back
to a plain internal network and logs a warning. That still blocks routing, but
leaves the bridge's gateway address — i.e. the host — reachable from the
instance. Upgrade.)

**The one thing on that subnet is the challenge's own proxy, and it does not
answer there.** The instancer attaches a proxy container to each instance network
of its own challenge, so the proxy is multi-homed: it can dial the instance. The
proxy binds `PROXY_BIND`, the address the instancer picked for it on the control
network, and *never* `0.0.0.0` — a wildcard socket would also accept on every
instance network it is later attached to, which would let a pwned instance
connect to the proxy and ask for somebody else's key. The instancer picks that
address rather than letting the proxy guess which of its interfaces is the safe
one; the proxy logs a warning if it is a wildcard anyway.

**Nothing is published.** Instances get no host port at all, so a proxy is not
just the intended path, it is the only one.

**Keys are the only credential, and they only work at one door.** 16 random
bytes, stored as a container label, compared in constant time, and only ever
resolved for a *running* container of the challenge that is asking. Destroy an
instance and its key is dead. Turning a key into an address is the whole
authority in this system, so `/internal/route/<chal>/<key>` demands a token —
and each proxy has one of its own, `HMAC-SHA256(PROXY_TOKEN, <chal>)`, so a
token taken off one proxy opens that challenge and no other. It is derived
rather than stored, so it costs nothing to keep and survives a restart unchanged.
A bad token, an unknown challenge, an unknown key and a stopped instance all get
the same indistinguishable `404`.

**Blast radius.** Each instance gets its challenge's `mem_limit` and
`pids_limit` so one player's fork bomb is one player's problem: `pids_limit` is
the Linux pids cgroup, counting processes *and* threads, and the fork that goes
past it fails with `EAGAIN` inside that container and nowhere else. Leaving
either key out keeps the default — it takes an explicit `0` (or `''` for
`mem_limit`) to run an instance with no ceiling at all. Capabilities are
left alone on purpose: pwn challenges routinely need setuid helpers, and silently
breaking them would be worse than the marginal hardening.

## API

A running instance is reported as

```json
{"chal": "special-love", "name": "Special Love", "running": true,
 "mode": "netcat", "key": "9ce0…fea9", "proxy_host": null, "proxy_port": 1337,
 "expires_at": 1787020896, "remaining_time": 118}
```

That is the whole of it: which challenge, where to connect, the key, and how long
you have. `proxy_host` is `null` unless it is set, meaning "the host serving this
page". The instance's own port, address, subnet and container name are never in a
player-facing response — a player cannot route to any of it, so sending it would
only describe our machinery. The proxy gets them, from
`/internal/route/<chal>/<key>`, which is the one route that does.

Failures are the same story: the reason lands in the log, and the player gets
something they can act on — `503` + "no free instance right now" when the port
or subnet pool is full, `500` + "could not start your instance" for everything
else. Docker's own messages (image tags, container ids, port ranges) never leave
the log.

| Route | Response |
| --- | --- |
| `GET /` | the challenge list |
| `GET /c/<chal>` | one challenge's page; `404` for a challenge that is not served |
| `GET /api/challenges` | every challenge, and whether you have one running |
| `POST /api/verify` | `{"token": "ctfd_…"}` → `{"ctfd": true, "verified": true, "user": ...}`; `403` for a token CTFd does not know — or, in team scope, an account with no team — `503` when CTFd could not be asked. Only meaningful with `CTFD_VERIFY` on |
| `GET /api/<chal>/status` | your instance of it, or `{"running": false, ...}` |
| `POST /api/<chal>/create` | your instance; returns the existing one if you already have it, `403`/`503`/`500` + `{"running": false, "error": ...}` on failure |
| `POST /api/<chal>/destroy` | `{"running": false, ...}`, also when you had nothing running |
| `GET /internal/route/<chal>/<key>` | `{"host": ..., "port": ...}` for that challenge's proxy; `404` without its `X-Proxy-Token` |

`POST /api/<chal>/create` takes no parameters. Instance lifetime is the
challenge's `ttl` and only that: a request cannot ask for a longer one, so a body
is ignored, and the only way to change a lifetime is to edit the challenge's
config.yml and restart.

Every player-facing route only ever talks about the caller's own instance of the
challenge named in the path. `GET /` puts a random id in a Flask session cookie —
minted there rather than in `/create`, so two fast clicks on START cannot race
into two identities. That id and the challenge id together name both the
container (`ctf-instance-<chal>-<id>`) and the network
(`ctf-network-<chal>-<id>`), so one browser holds at most one instance of each
challenge. Somebody else's `/status` reports nothing and their `/destroy` removes
nothing. Users never send an image, port, subnet, command, or anything else
Docker acts on.

With `CTFD_VERIFY` on that id is not minted at all: it is the CTFd account behind
a verified token, so the sentence above holds with "account" in place of
"browser", and `/create` is `403` + "verify your CTFd token" until `POST
/api/verify` has said who is asking. The one thing a player *does* send in that
mode is the token, and it is checked against a shape before it is spent (`ctfd_`
and 64 hex), so nothing else reaches CTFd.

## How it works

**Startup.** The instancer reads `challenges/`, one directory at a time, and
holds each one that has a Dockerfile, a config.yml and a port nobody else claimed.
It builds the proxy image and one image per challenge (only if missing), removes
any proxy whose challenge is no longer there — before raising the others, because
a departed challenge's proxy is still holding its published port — and then gives
every challenge a proxy: a container on the control network, with one address of
its own to bind, the host port its config.yml asked for, and a token derived for
it alone. A proxy that is already running the config it should be is adopted
untouched, so restarting the instancer does not interrupt anyone mid-connection;
one running the *previous* config.yml is replaced. A challenge that will not
build, or cannot get a proxy, is dropped with a line in the log while the rest
come up.

**Create.** Under two locks (below), the instancer checks that challenge's
`max_instances`, picks a free port for it, a free `/24` out of its `subnet_pool`,
and a fresh key; creates
`ctf-network-<chal>-<id>` as an internal, gatewayless bridge; attaches *that
challenge's* proxy to it; then creates and starts `ctf-instance-<chal>-<id>` on
it with the port in `$CHAL_PORT`. If anything fails — including the proxy being
missing — the half-built container and network are torn down before returning
`500`, so the port, subnet and key stay free.

**Connect.** The player sends the key to the challenge's proxy: as the first line
in netcat mode, or as the first path segment (`/<key>/…`) in http mode, where the
answer also carries a `sz_key` cookie so the challenge's own absolute links keep
routing. The proxy asks the instancer where that key leads *for its own
challenge*, opens a connection to `<instance ip>:<instance port>`, and splices the
two together. It keeps no routing table of its own, so a destroyed instance is
unreachable immediately. In http mode each exchange gets its own upstream
connection (`Connection: close`), because keep-alive would let a second request
ride in on the first request's key.

**A crowd.** Everything that can be handed out twice is handed out under a lock,
and everything slow happens outside one. Two levels:

* `pool_lock` covers *counting* what is out and *choosing* a port and a subnet,
  and nothing else. Counting is in there with the choosing because they answer
  the same question: two players arriving at once must not both be handed the
  last slot under `max_instances`, any more than they may be handed the same
  port. The choice
  is written into `reserved_ports` / `reserved_subnets` before the lock is
  released, because Docker only becomes the record of what is taken once the
  container exists — a second or two later, and two players would be handed the
  same port in that gap. The reservation is dropped in a `finally`, whether the
  build worked or not.
* An **owner lock**, one per session per challenge (striped, so the set is fixed
  rather than growing with every session an event sees), covers everything that
  touches a single instance: create, destroy, the stale-container sweep in
  `/status`, and the reaper. Two requests about the same instance queue up instead
  of fighting.

That second lock is not decoration. `/status` *deletes* a container it finds not
running, and "not running yet" is exactly what a container looks like between
being created and being started — so an unlocked poll, or a double-clicked
START, would delete the instance being built. Same for the reaper: a create makes
the network a moment before the container, which is precisely what an orphan
network looks like. Both are covered by tests that fail if the locking is
removed.

Because the pool lock is never held while Docker works, creates overlap: twelve
sessions pressing START at once finish in a few seconds against a real daemon,
all twelve with their own port, subnet, key and network. When the pools do run
out, the extra players get `503` and nothing half-built is left behind — they are
turned away, never double-booked.

The reservations live in this process, so run one instancer process. Threads are
fine (the dev server is threaded, which is what all of the above is for); several
worker *processes* would each keep their own reservation set and could hand out
the same port twice.

**TTL.** `expires_at = now + ttl`, the challenge's own, is stamped on the
container as a label. A background thread wakes every `CLEANUP_INTERVAL` seconds
and destroys any instance past its `expires_at` (container **and** network), and
prunes any `ctf-network-*` whose container is gone. It works off labels rather
than off the loaded config, so instances of a challenge taken off disk mid-event
still expire on schedule instead of living forever. Removing a network means
detaching the proxy first — Docker refuses to remove a network that still has an
endpoint.

**Persistence.** Docker is the only state store — there is no database. All
metadata lives in labels (`ctf.chal`, `ctf.owner`, `ctf.expires_at`,
`ctf.subnet`, `ctf.port`, `ctf.key` on instances; `ctf.chal` and
`ctf.proxy_spec` on proxies) on the container and network, so restarting the
instancer re-discovers running instances, their TTLs *and* their keys instead of
duplicating them, and can tell an adoptable proxy from a stale one. The proxies
are stateless for the same reason: each re-reads every key from the instancer.
Reconnecting a *browser* to its instances across a restart additionally needs a
stable `SECRET_KEY`.

**Recycling.** Nothing is tracked in memory: `pick_port()` reads the ports of one
challenge's live instances and walks that challenge's own range, and
`pick_subnet()` walks that challenge's own pool while checking against the
subnets of *every* live network — so a destroyed or reaped instance's port and
/24 are free again the moment it is gone, and two challenges sharing a pool
still cannot be handed the same one.

## Tests

```sh
pip install pytest
python -m pytest tests -rs
```

`tests/test_instancer.py` runs against a fake Docker daemon (no Docker needed);
two Flask test clients stand in for two users, two challenges stand in for an
event, and a handful of tests pin down exactly what a player is and is not told.
A fake CTFd stands in for the real one where `CTFD_VERIFY` is concerned — a token
that verifies, one CTFd has never heard of, one that never leaves the instancer
because it is not a token at all, and a CTFd that cannot be reached (which is not
the same as a CTFd saying no) — along with the thing the whole feature exists
for: two browsers holding one token get one instance between them.
`tests/test_proxy.py` runs the real proxy over real sockets against fake
instances — key handling, path/cookie routing, hangups, and the parsing helpers.
`tests/test_docker_integration.py` builds the real proxy and both real challenge
images and drives real containers, networks and proxies: a key really carries a
player through a proxy to the pwn service and through another to the web app, a
key really does nothing at the other challenge's door, a destroyed instance's key
really stops working, and an instance really cannot reach a proxy, the control
network, a default route, or anybody else's instance. It skips itself when no
Docker daemon is reachable, or when the ports it wants are taken.

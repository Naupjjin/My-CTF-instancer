# CTF Instancer (MVP)

One challenge, one button, one instance per browser session.

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

`challenge/` must contain a `Dockerfile` that builds a container which

* listens on **`0.0.0.0:8080`** (`CONTAINER_PORT`) when started with no arguments,
* needs no volumes, no extra capabilities, and no other published port.

Nothing else is read: no manifest, no name, no config file. The bundled
`challenge/server.py` is a placeholder — replace it with a real challenge.

## API

| Route | Response |
| --- | --- |
| `GET /` | the web UI |
| `GET /status` | your instance: `{"running": false}` or `{"running": true, "port": 30000}` |
| `POST /create` | `{"running": true, "port": 30000}`; returns your existing instance if you have one, `500` + `{"running": false, "error": ...}` on failure |
| `POST /destroy` | `{"running": false}`, also when you had nothing running |

Every route only ever talks about the caller's own instance.

`GET /` puts a random id in a Flask session cookie — minted there rather than in
`/create`, so two fast clicks on Start cannot race into two identities. The id
names the container: `ctf-instance-<id>`. Somebody else's `/status` reports
nothing and their `/destroy` removes nothing, because the name they look up is
their own; a request with no session at all sees nothing either. Users never
send an image, a port, or anything else Docker acts on.

Docker is the only state store: restart the instancer and containers that are
still up are adopted by their sessions, never duplicated. `POST /create` is
serialised by a `threading.Lock`, so one session cannot end up with two
containers and two sessions cannot be handed the same port.

## Tests

```sh
pip install pytest requests
python -m pytest tests -rs
```

`tests/test_instancer.py` runs against a fake Docker daemon (no Docker needed).
Two Flask test clients stand in for two users.
`tests/test_docker_integration.py` builds the real challenge image and drives a
real container; it skips itself when no Docker daemon is reachable.

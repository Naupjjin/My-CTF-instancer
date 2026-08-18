"""Minimal CTF instancer.

One challenge, one instance per browser session. Each instance gets its own
bridge network (a unique /24 out of a pool) and a TTL after which a background
thread reaps it. All state lives in Docker itself — container/network names and
labels — so a restart of this process re-discovers everything instead of
duplicating it.
"""

import ipaddress
import logging
import os
import secrets
import socket
import threading
import time

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from flask import Flask, jsonify, render_template, request, session

PORT_MIN = int(os.environ.get("PORT_MIN", "30000"))
PORT_MAX = int(os.environ.get("PORT_MAX", "30100"))
CHALLENGE_DIR = os.environ.get("CHALLENGE_DIR", "/challenge")
CHALLENGE_IMAGE = os.environ.get("CHALLENGE_IMAGE", "ctf-challenge:latest")
FORCE_BUILD = os.environ.get("FORCE_BUILD", "").lower() in ("1", "true", "yes")
CONTAINER_PORT = int(os.environ.get("CONTAINER_PORT", "8080"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "5000"))
SECRET_KEY = os.environ.get("SECRET_KEY")
CONTAINER_PREFIX = "ctf-instance-"
NETWORK_PREFIX = "ctf-network-"

# One /24 per instance, carved out of this pool.
SUBNET_POOL = ipaddress.ip_network(os.environ.get("SUBNET_POOL", "10.100.0.0/16"))
SUBNET_PREFIX = int(os.environ.get("SUBNET_PREFIX", "24"))

# TTL: how long an instance lives, and how often the reaper looks.
DEFAULT_TTL = int(os.environ.get("DEFAULT_TTL", "3600"))
MAX_TTL = int(os.environ.get("MAX_TTL", "86400"))
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "10"))

# Metadata we stash on Docker objects so a restart can recover the state.
LABEL_OWNER = "ctf.owner"
LABEL_EXPIRES = "ctf.expires_at"
LABEL_SUBNET = "ctf.subnet"

# How players reach the instance decides how we show the address:
#   http   -> a clickable http://host:port link  (web challenges)
#   netcat -> an nc host port command            (pwn / raw-tcp challenges)
MODE = os.environ.get("MODE", "http").lower()
if MODE not in ("http", "netcat"):
    logging.getLogger("instancer").warning("unknown MODE %r, falling back to http", MODE)
    MODE = "http"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("instancer")

app = Flask(__name__)
app.secret_key = SECRET_KEY or secrets.token_hex(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
lock = threading.Lock()

_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def owner_id():
    """Id of the current browser session, minted on first use."""
    if "owner" not in session:
        session.permanent = True
        session["owner"] = secrets.token_hex(8)
    return session["owner"]


def container_name(owner):
    return CONTAINER_PREFIX + owner


def network_name(owner):
    return NETWORK_PREFIX + owner


# --- instance lookup ----------------------------------------------------------

def get_instance(owner):
    """Return this session's running container, or None.

    A leftover stopped container (and its network) is removed so the name,
    port and subnet are free again.
    """
    try:
        container = client().containers.get(container_name(owner))
    except NotFound:
        return None
    if container.status != "running":
        log.info("removing stale container of %s (status=%s)", owner, container.status)
        remove_instance(owner)
        return None
    return container


def instance_port(container):
    bindings = container.attrs["NetworkSettings"]["Ports"].get("%d/tcp" % CONTAINER_PORT)
    if not bindings:
        return None
    return int(bindings[0]["HostPort"])


def instance_expires_at(container):
    raw = (container.labels or {}).get(LABEL_EXPIRES)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def list_instances():
    """Every instance container, running or not."""
    return [c for c in client().containers.list(all=True)
            if c.name.startswith(CONTAINER_PREFIX)]


# --- host ports ---------------------------------------------------------------

def docker_used_ports():
    """Host ports published by any running container."""
    used = set()
    for container in client().containers.list():
        ports = container.attrs["NetworkSettings"]["Ports"] or {}
        for bindings in ports.values():
            for binding in bindings or []:
                used.add(int(binding["HostPort"]))
    return used


def port_is_free(port):
    """True if nothing on this host is bound to the port right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def pick_port():
    used = docker_used_ports()
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port not in used and port_is_free(port):
            return port
    raise RuntimeError("no free host port in %d-%d" % (PORT_MIN, PORT_MAX))


# --- subnets ------------------------------------------------------------------

def used_subnets():
    """Every subnet Docker already has a network for."""
    used = []
    for network in client().networks.list():
        ipam = network.attrs.get("IPAM") or {}
        for config in (ipam.get("Config") or []):
            subnet = config.get("Subnet")
            if subnet:
                try:
                    used.append(ipaddress.ip_network(subnet))
                except ValueError:
                    pass
    return used


def pick_subnet():
    used = used_subnets()
    for subnet in SUBNET_POOL.subnets(new_prefix=SUBNET_PREFIX):
        if not any(subnet.overlaps(u) for u in used):
            return subnet
    raise RuntimeError("no free /%d subnet in %s" % (SUBNET_PREFIX, SUBNET_POOL))


# --- create / destroy ---------------------------------------------------------

def remove_network(owner):
    try:
        client().networks.get(network_name(owner)).remove()
    except NotFound:
        pass
    except APIError as exc:
        log.error("could not remove network for %s: %s", owner, exc)


def remove_instance(owner):
    """Remove this owner's container and its network. Safe if either is gone.

    Returns True if a container was actually removed.
    """
    removed = False
    try:
        client().containers.get(container_name(owner)).remove(force=True)
        removed = True
    except NotFound:
        pass
    remove_network(owner)
    return removed


def image_exists():
    try:
        client().images.get(CHALLENGE_IMAGE)
        return True
    except ImageNotFound:
        return False


def build_image():
    # Building on every startup would block the web server for minutes each boot
    # (the SDK's builder re-runs the challenge's apt install). Build only when the
    # image is missing; set FORCE_BUILD=1 to rebuild after changing the challenge.
    if image_exists() and not FORCE_BUILD:
        log.info("challenge image %s already present, skipping build "
                 "(set FORCE_BUILD=1 to rebuild)", CHALLENGE_IMAGE)
        return
    log.info("building challenge image %s from %s", CHALLENGE_IMAGE, CHALLENGE_DIR)
    client().images.build(path=CHALLENGE_DIR, tag=CHALLENGE_IMAGE, rm=True)
    log.info("challenge image built: %s", CHALLENGE_IMAGE)


def requested_ttl():
    body = request.get_json(silent=True) or {}
    try:
        ttl = int(body.get("ttl", DEFAULT_TTL))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    if ttl <= 0:
        ttl = DEFAULT_TTL
    return min(ttl, MAX_TTL)


def instance_json(container):
    expires_at = instance_expires_at(container)
    remaining = max(0, expires_at - int(time.time())) if expires_at is not None else None
    return jsonify(
        running=True,
        port=instance_port(container),
        mode=MODE,
        expires_at=expires_at,
        remaining_time=remaining,
    )


# --- routes -------------------------------------------------------------------

@app.get("/")
def index():
    # Mint the session here, so two fast clicks on Start cannot race each other
    # into two identities (and therefore two containers).
    owner_id()
    return render_template("index.html", mode=MODE, default_ttl=DEFAULT_TTL)


@app.get("/status")
def status():
    owner = session.get("owner")
    container = get_instance(owner) if owner else None
    if container is None:
        return jsonify(running=False, mode=MODE)
    return instance_json(container)


@app.post("/create")
def create():
    owner = owner_id()
    ttl = requested_ttl()
    with lock:
        container = get_instance(owner)
        if container is not None:
            return instance_json(container)

        # Clear any orphan network left behind by a previous life of this owner.
        remove_network(owner)

        container = None
        try:
            port = pick_port()
            subnet = pick_subnet()
            expires_at = int(time.time()) + ttl
            client().networks.create(
                network_name(owner),
                driver="bridge",
                ipam=docker.types.IPAMConfig(
                    pool_configs=[docker.types.IPAMPool(subnet=str(subnet))]),
                labels={LABEL_OWNER: owner, LABEL_SUBNET: str(subnet)},
                check_duplicate=True,
            )
            container = client().containers.create(
                CHALLENGE_IMAGE,
                name=container_name(owner),
                ports={"%d/tcp" % CONTAINER_PORT: port},
                network=network_name(owner),
                labels={
                    LABEL_OWNER: owner,
                    LABEL_EXPIRES: str(expires_at),
                    LABEL_SUBNET: str(subnet),
                },
            )
            container.start()
            container.reload()
        except Exception as exc:
            log.error("create failed for %s: %s", owner, exc)
            try:
                remove_instance(owner)
            except (DockerException, OSError) as cleanup_exc:
                log.error("cleanup after failed create: %s", cleanup_exc)
            return jsonify(running=False, error=str(exc), mode=MODE), 500

        log.info("instance created for %s: host %d -> container %d, subnet %s, ttl %ds",
                 owner, port, CONTAINER_PORT, subnet, ttl)
        return instance_json(container)


@app.post("/destroy")
def destroy():
    owner = session.get("owner")
    if owner is None:
        return jsonify(running=False, mode=MODE)
    with lock:
        try:
            removed = remove_instance(owner)
        except Exception as exc:
            log.error("destroy failed for %s: %s", owner, exc)
            return jsonify(running=True, error=str(exc), mode=MODE), 500
    if removed:
        log.info("instance destroyed for %s", owner)
    return jsonify(running=False, mode=MODE)


# --- TTL reaper ---------------------------------------------------------------

def reap_expired():
    """Destroy expired instances and drop networks whose container is gone."""
    now = int(time.time())
    with lock:
        for container in list_instances():
            owner = (container.labels or {}).get(LABEL_OWNER) \
                or container.name[len(CONTAINER_PREFIX):]
            expires_at = instance_expires_at(container)
            if expires_at is not None and now >= expires_at:
                log.info("instance for %s expired, destroying", owner)
                remove_instance(owner)
        prune_orphan_networks()


def prune_orphan_networks():
    for network in client().networks.list():
        if not network.name.startswith(NETWORK_PREFIX):
            continue
        owner = network.name[len(NETWORK_PREFIX):]
        try:
            client().containers.get(container_name(owner))
        except NotFound:
            log.info("removing orphan network %s", network.name)
            try:
                network.remove()
            except APIError as exc:
                log.error("could not remove orphan network %s: %s", network.name, exc)


def cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            reap_expired()
        except Exception as exc:  # never let the reaper thread die
            log.error("cleanup error: %s", exc)


def start_cleanup_thread():
    thread = threading.Thread(target=cleanup_loop, name="ttl-reaper", daemon=True)
    thread.start()
    return thread


# --- startup ------------------------------------------------------------------

def startup():
    if not SECRET_KEY:
        log.warning("SECRET_KEY not set: sessions, and with them instance ownership, "
                    "are lost when the instancer restarts")
    build_image()
    for container in list_instances():
        owner = (container.labels or {}).get(LABEL_OWNER, "?")
        log.info("adopted instance of %s (%s), expires_at=%s",
                 owner, container.status, instance_expires_at(container))
    log.info("server started on port %d (mode=%s, container port %d, host ports %d-%d, "
             "subnet pool %s /%d, default ttl %ds)",
             LISTEN_PORT, MODE, CONTAINER_PORT, PORT_MIN, PORT_MAX,
             SUBNET_POOL, SUBNET_PREFIX, DEFAULT_TTL)


if __name__ == "__main__":
    startup()
    start_cleanup_thread()
    app.run(host="0.0.0.0", port=LISTEN_PORT)

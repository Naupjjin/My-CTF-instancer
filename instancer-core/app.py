"""SpawnZero: a minimal CTF instancer.

One challenge, one instance per browser session. Creating an instance hands out
four things that belong to nobody else:

  * a container,
  * a /24 bridge network -- internal, and without a gateway address, so the
    instance can route to nothing at all except the proxy dialling in,
  * a port inside that network,
  * a key.

Nothing is published to the host: the only way to an instance is the proxy
(proxy-core/proxy.py), which trades a key for an address. All state lives in
Docker itself -- container/network names and labels -- so a restart of this
process re-discovers everything instead of duplicating it.
"""

import ipaddress
import logging
import os
import secrets
import signal
import sys
import threading
import time

import docker
import yaml
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from flask import Flask, jsonify, render_template, request, session

# The port an instance listens on, inside its own network. Never published, so
# the range only has to be big enough for the instances running at once -- it
# cannot collide with anything on the host.
INSTANCE_PORT_MIN = int(os.environ.get("INSTANCE_PORT_MIN", "30000"))
INSTANCE_PORT_MAX = int(os.environ.get("INSTANCE_PORT_MAX", "30100"))

CHALLENGE_DIR = os.environ.get("CHALLENGE_DIR", "/challenge")
CHALLENGE_IMAGE = os.environ.get("CHALLENGE_IMAGE", "spawnzero-challenge:latest")
FORCE_BUILD = os.environ.get("FORCE_BUILD", "").lower() in ("1", "true", "yes")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "5000"))
SECRET_KEY = os.environ.get("SECRET_KEY")
CONTAINER_PREFIX = "spawnzero-instance-"
NETWORK_PREFIX = "spawnzero-network-"

# The proxy: one container, one published port, attached to every instance
# network by us. PROXY_HOST is only needed when players reach the proxy under a
# different name than the instancer -- empty means "same host as this page".
PROXY_CONTAINER = os.environ.get("PROXY_CONTAINER", "spawnzero-proxy")
PROXY_HOST = os.environ.get("PROXY_HOST", "")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "1337"))
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")

# Keys are what a player sends to the proxy, so they are the only credential in
# the system: long enough not to be guessed, short enough to paste.
KEY_BYTES = int(os.environ.get("KEY_BYTES", "16"))

# The environment variable that tells the challenge which port to listen on.
PORT_ENV = os.environ.get("PORT_ENV", "CHAL_PORT")

# The one thing that is not an environment variable: the names on the page.
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.yml")

# Blast radius of one instance. Empty or 0 leaves the limit to Docker.
MEM_LIMIT = os.environ.get("MEM_LIMIT", "512m")
PIDS_LIMIT = int(os.environ.get("PIDS_LIMIT", "256") or 0)

# One /24 per instance, carved out of this pool.
SUBNET_POOL = ipaddress.ip_network(os.environ.get("SUBNET_POOL", "10.100.0.0/16"))
SUBNET_PREFIX = int(os.environ.get("SUBNET_PREFIX", "24"))

# Docker >= 28 can leave the bridge without a gateway address. That is what
# keeps an instance off the host: with no gateway it has a route to its own /24
# and to nothing else -- not the host, not the control network, not another
# instance. Older daemons reject the option; we fall back to a plain internal
# network, which still blocks routing but leaves the gateway address reachable.
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
NETWORK_OPTIONS = {GATEWAY_MODE_OPTION: "isolated"}

# Instances outlive this process by design -- a crash, or a restart mid-event,
# must not take every player's instance with it (they are re-adopted from their
# labels on the way back up). A clean shutdown is the other case: `docker
# compose down` means "take it all down", and compose knows nothing about
# containers we created, so we have to. Set 0 to keep instances across a
# deliberate stop as well.
REAP_ON_SHUTDOWN = os.environ.get("REAP_ON_SHUTDOWN", "1").lower() not in ("0", "false", "no")

# How long a shutdown waits for an in-flight create before tearing down anyway.
# Docker's own grace period is short; being late is worse than being rude.
SHUTDOWN_LOCK_WAIT = 5

# TTL: how long an instance lives, and how often the reaper looks.
DEFAULT_TTL = int(os.environ.get("DEFAULT_TTL", "3600"))
MAX_TTL = int(os.environ.get("MAX_TTL", "86400"))
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "10"))

# Metadata we stash on Docker objects so a restart can recover the state.
LABEL_OWNER = "spawnzero.owner"
LABEL_EXPIRES = "spawnzero.expires_at"
LABEL_SUBNET = "spawnzero.subnet"
LABEL_PORT = "spawnzero.port"
LABEL_KEY = "spawnzero.key"

# What a player is told when something breaks. Docker's own messages name
# images, ports, subnets and container ids -- all of it ours to read in the log,
# none of it theirs to see in a browser.
ERROR_BUSY = "no free instance right now, try again in a moment"
ERROR_CREATE = "could not start your instance, try again in a moment"
ERROR_DESTROY = "could not stop your instance, try again in a moment"

# How players reach the instance decides how we show the address:
#   http   -> a clickable http://proxy:port/<key>/ link  (web challenges)
#   netcat -> an nc proxy port command, then the key     (pwn / raw-tcp)
MODE = os.environ.get("MODE", "http").lower()
if MODE not in ("http", "netcat"):
    logging.getLogger("instancer").warning("unknown MODE %r, falling back to http", MODE)
    MODE = "http"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("instancer")


# --- names --------------------------------------------------------------------

DEFAULT_CONFIG = {
    "chal_name": "challenge",
    "author": "",
    "type": "",
    "instancer_name": "SpawnZero",
}


def load_config(path=None):
    """Read config.yml. Names only -- nothing here changes how anything runs.

    A missing or broken file is not fatal: an instancer that will not start
    because someone fat-fingered a display name would be a worse trade than one
    that starts with a placeholder on the page and a complaint in the log.
    """
    path = path or CONFIG_FILE
    try:
        with open(path) as handle:
            loaded = yaml.safe_load(handle) or {}
    except OSError as exc:
        log.warning("no config file at %s (%s), falling back to defaults", path, exc)
        return dict(DEFAULT_CONFIG)
    except yaml.YAMLError as exc:
        log.error("config file %s is not valid YAML (%s), falling back to defaults",
                  path, exc)
        return dict(DEFAULT_CONFIG)
    if not isinstance(loaded, dict):
        log.error("config file %s is not a mapping, falling back to defaults", path)
        return dict(DEFAULT_CONFIG)
    unknown = set(loaded) - set(DEFAULT_CONFIG)
    if unknown:
        log.warning("ignoring unknown key(s) in %s: %s", path, ", ".join(sorted(unknown)))
    config = dict(DEFAULT_CONFIG)
    config.update({key: str(loaded[key]) for key in DEFAULT_CONFIG
                   if loaded.get(key) is not None})
    return config


CONFIG = load_config()

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
    port, subnet and key are free again.
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


def label(container, name):
    return (container.labels or {}).get(name)


def label_int(container, name):
    try:
        return int(label(container, name))
    except (TypeError, ValueError):
        return None


def instance_owner(container):
    return label(container, LABEL_OWNER) or container.name[len(CONTAINER_PREFIX):]


def instance_port(container):
    return label_int(container, LABEL_PORT)


def instance_expires_at(container):
    return label_int(container, LABEL_EXPIRES)


def instance_address(container):
    """The instance's address on its own network -- what the proxy dials."""
    networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    endpoint = networks.get(network_name(instance_owner(container))) or {}
    return endpoint.get("IPAddress") or None


def list_instances():
    """Every instance container, running or not."""
    return [c for c in client().containers.list(all=True)
            if c.name.startswith(CONTAINER_PREFIX)]


def find_by_key(key):
    """The running instance a key belongs to, or None.

    Compared in constant time and against running containers only, so a key
    stops working the moment its instance does.
    """
    if not key or not key.isascii():
        return None
    for container in list_instances():
        if container.status != "running":
            continue
        if secrets.compare_digest(label(container, LABEL_KEY) or "", key):
            return container
    return None


# --- ports --------------------------------------------------------------------

def used_ports():
    """Ports already handed to an instance.

    Only instances can hold one: the port lives inside the instance's own
    network namespace, so nothing else on the host can be in the way.
    """
    return {port for port in (instance_port(c) for c in list_instances())
            if port is not None}


def pick_port():
    used = used_ports()
    for port in range(INSTANCE_PORT_MIN, INSTANCE_PORT_MAX + 1):
        if port not in used:
            return port
    raise RuntimeError("no free instance port in %d-%d"
                       % (INSTANCE_PORT_MIN, INSTANCE_PORT_MAX))


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


# --- keys ---------------------------------------------------------------------

def pick_key():
    return secrets.token_hex(KEY_BYTES)


# --- create / destroy ---------------------------------------------------------

def create_network(owner, subnet):
    """The instance's own network: internal, and ideally without a gateway."""
    kwargs = dict(
        driver="bridge",
        internal=True,
        ipam=docker.types.IPAMConfig(
            pool_configs=[docker.types.IPAMPool(subnet=str(subnet))]),
        labels={LABEL_OWNER: owner, LABEL_SUBNET: str(subnet)},
        check_duplicate=True,
    )
    try:
        return client().networks.create(network_name(owner),
                                        options=NETWORK_OPTIONS, **kwargs)
    except APIError as exc:
        log.warning("daemon rejected %s=isolated (%s); the instance network keeps "
                    "a reachable gateway address -- upgrade to Docker 28+ to close it",
                    GATEWAY_MODE_OPTION, exc)
        return client().networks.create(network_name(owner), **kwargs)


def attach_proxy(owner):
    """Give the proxy an interface on this instance's network.

    The proxy answers only on its control-network address, so this is a one-way
    door: the proxy can dial the instance, the instance finds nothing listening.
    """
    client().networks.get(network_name(owner)).connect(PROXY_CONTAINER)


def detach_proxy(network):
    """Best effort: a half-built instance may never have got the proxy attached.

    A detach that really was needed and really failed is not swallowed -- the
    removal right after it fails loudly with "has active endpoints".
    """
    try:
        network.disconnect(PROXY_CONTAINER, force=True)
    except (NotFound, APIError) as exc:
        log.debug("proxy not detached from %s: %s", network.name, exc)


def remove_network(owner):
    """Remove this owner's network. Docker refuses while the proxy is attached."""
    try:
        network = client().networks.get(network_name(owner))
    except NotFound:
        return
    detach_proxy(network)
    try:
        network.remove()
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


def create_container(owner, port, subnet, key, expires_at):
    limits = {}
    if MEM_LIMIT:
        limits["mem_limit"] = MEM_LIMIT
    if PIDS_LIMIT:
        limits["pids_limit"] = PIDS_LIMIT
    return client().containers.create(
        CHALLENGE_IMAGE,
        name=container_name(owner),
        # No ports=: publishing one would be a way in that skips the proxy.
        network=network_name(owner),
        environment={PORT_ENV: str(port)},
        labels={
            LABEL_OWNER: owner,
            LABEL_EXPIRES: str(expires_at),
            LABEL_SUBNET: str(subnet),
            LABEL_PORT: str(port),
            LABEL_KEY: key,
        },
        **limits,
    )


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
    """What the player's page gets: how to connect, and how long they have.

    Deliberately not in here: the instance's port, its address, its subnet, its
    container name. A player cannot route to any of it, so telling them only
    describes our machinery.
    """
    expires_at = instance_expires_at(container)
    remaining = max(0, expires_at - int(time.time())) if expires_at is not None else None
    return jsonify(
        running=True,
        mode=MODE,
        key=label(container, LABEL_KEY),
        proxy_host=PROXY_HOST or None,
        proxy_port=PROXY_PORT,
        expires_at=expires_at,
        remaining_time=remaining,
    )


def idle_json():
    return jsonify(running=False, mode=MODE,
                   proxy_host=PROXY_HOST or None, proxy_port=PROXY_PORT)


# --- routes -------------------------------------------------------------------

@app.get("/")
def index():
    # Mint the session here, so two fast clicks on Start cannot race each other
    # into two identities (and therefore two containers).
    owner_id()
    return render_template("index.html", mode=MODE, default_ttl=DEFAULT_TTL,
                           proxy_host=PROXY_HOST, proxy_port=PROXY_PORT, **CONFIG)


@app.get("/status")
def status():
    owner = session.get("owner")
    container = get_instance(owner) if owner else None
    if container is None:
        return idle_json()
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

        try:
            port = pick_port()
            subnet = pick_subnet()
            key = pick_key()
            expires_at = int(time.time()) + ttl
            create_network(owner, subnet)
            attach_proxy(owner)
            container = create_container(owner, port, subnet, key, expires_at)
            container.start()
            container.reload()
        except Exception as exc:
            log.error("create failed for %s: %s", owner, exc)
            try:
                remove_instance(owner)
            except (DockerException, OSError) as cleanup_exc:
                log.error("cleanup after failed create: %s", cleanup_exc)
            # pick_port/pick_subnet raise RuntimeError when the pools are full --
            # the one failure a player can actually do something about (wait).
            if isinstance(exc, RuntimeError):
                return jsonify(running=False, mode=MODE, error=ERROR_BUSY), 503
            return jsonify(running=False, mode=MODE, error=ERROR_CREATE), 500

        log.info("instance created for %s: %s port %d, subnet %s, ttl %ds",
                 owner, instance_address(container) or "?", port, subnet, ttl)
        return instance_json(container)


@app.post("/destroy")
def destroy():
    owner = session.get("owner")
    if owner is None:
        return idle_json()
    with lock:
        try:
            removed = remove_instance(owner)
        except Exception as exc:
            log.error("destroy failed for %s: %s", owner, exc)
            return jsonify(running=True, mode=MODE, error=ERROR_DESTROY), 500
    if removed:
        log.info("instance destroyed for %s", owner)
    return idle_json()


@app.get("/internal/route/<key>")
def route(key):
    """Key -> instance address. The proxy is the only caller.

    Turning a key into an address is the whole authority in this system, so the
    call carries a shared token; without it -- or with a key nobody owns -- the
    answer is the same 404, and the proxy has nowhere to send the player.
    """
    presented = request.headers.get("X-Proxy-Token", "")
    if not PROXY_TOKEN or not secrets.compare_digest(presented, PROXY_TOKEN):
        return jsonify(error="not found"), 404
    container = find_by_key(key)
    host = instance_address(container) if container is not None else None
    if host is None:
        return jsonify(error="not found"), 404
    return jsonify(host=host, port=instance_port(container))


# --- TTL reaper ---------------------------------------------------------------

def reap_expired():
    """Destroy expired instances and drop networks whose container is gone."""
    now = int(time.time())
    with lock:
        for container in list_instances():
            owner = instance_owner(container)
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
            detach_proxy(network)
            try:
                network.remove()
            except APIError as exc:
                log.error("could not remove orphan network %s: %s", network.name, exc)


def destroy_all():
    """Tear down every instance there is. The end of the line for a shutdown."""
    held = lock.acquire(timeout=SHUTDOWN_LOCK_WAIT)
    try:
        for container in list_instances():
            owner = instance_owner(container)
            log.info("shutting down: destroying instance of %s", owner)
            remove_instance(owner)
        prune_orphan_networks()
    finally:
        if held:
            lock.release()


def handle_shutdown(signum, frame):
    """SIGTERM/SIGINT: what `docker compose down` and Ctrl-C arrive as."""
    log.info("signal %d received, shutting down", signum)
    if REAP_ON_SHUTDOWN:
        try:
            destroy_all()
        except Exception as exc:   # a failed cleanup must not block the exit
            log.error("shutdown cleanup failed: %s", exc)
    sys.exit(0)


def install_shutdown_handler():
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, handle_shutdown)


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
    if not PROXY_TOKEN:
        log.warning("PROXY_TOKEN not set: every key lookup is refused, so no player "
                    "can get through the proxy")
    build_image()
    for container in list_instances():
        log.info("adopted instance of %s (%s), expires_at=%s", instance_owner(container),
                 container.status, instance_expires_at(container))
    log.info("%s serving %r by %s (%s)", CONFIG["instancer_name"], CONFIG["chal_name"],
             CONFIG["author"] or "nobody in particular", CONFIG["type"] or "no type")
    log.info("server started on port %d (mode=%s, proxy %s:%d via %s, instance ports "
             "%d-%d, subnet pool %s /%d, default ttl %ds, reap on shutdown: %s)",
             LISTEN_PORT, MODE, PROXY_HOST or "<this host>", PROXY_PORT, PROXY_CONTAINER,
             INSTANCE_PORT_MIN, INSTANCE_PORT_MAX, SUBNET_POOL, SUBNET_PREFIX, DEFAULT_TTL,
             "yes" if REAP_ON_SHUTDOWN else "no")


if __name__ == "__main__":
    startup()
    install_shutdown_handler()
    start_cleanup_thread()
    app.run(host="0.0.0.0", port=LISTEN_PORT)

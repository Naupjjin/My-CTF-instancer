"""A CTF instancer: many challenges, one service.

A challenge is a directory under CHALLENGES_DIR with a config.yml next to its
Dockerfile, and that is the whole of what the instancer knows about it. Each one
gets:

  * an image, built once,
  * a proxy container of its own -- one published port, and the only door in,
  * and, per player, an instance.

An instance is four things that belong to nobody else:

  * a container,
  * a /24 bridge network -- internal, and without a gateway address, so the
    instance can route to nothing at all except the proxy dialling in,
  * a port inside that network,
  * a key.

Nothing is published to the host: the only way to an instance is its challenge's
proxy (proxy-core/proxy.py), which trades a key for an address. All state lives
in Docker itself -- container/network names and labels -- so a restart of this
process re-discovers everything instead of duplicating it.
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time

import docker
import yaml
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from flask import Flask, jsonify, render_template, request, session

# Where the challenges are, one directory each. The directory name is the
# challenge id: it is in the URL, and in the name of everything the challenge is
# made of, so it is held to a shape.
CHALLENGES_DIR = os.environ.get("CHALLENGES_DIR", "/challenges")
CHALLENGE_CONFIG = "config.yml"
CHALLENGE_IMAGE = os.environ.get("CHALLENGE_IMAGE", "ctf-challenge-{chal}:latest")
CHALLENGE_ID = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?\Z")
FORCE_BUILD = os.environ.get("FORCE_BUILD", "").lower() in ("1", "true", "yes")

# The proxy image is ours too, and built the same way -- the instancer creates
# the proxies, so nothing outside this process needs to know how many there are.
PROXY_DIR = os.environ.get("PROXY_DIR", "/proxy-core")
PROXY_IMAGE = os.environ.get("PROXY_IMAGE", "ctf-proxy:latest")

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "5000"))
SECRET_KEY = os.environ.get("SECRET_KEY")

# One instancer, many challenges, so a name has to say which. Every object
# carries the challenge it belongs to and the session that owns it.
CONTAINER_PREFIX = "ctf-instance-"
NETWORK_PREFIX = "ctf-network-"
PROXY_PREFIX = "ctf-proxy-"
CONTAINER_NAME = CONTAINER_PREFIX + "{chal}-{owner}"
NETWORK_NAME = NETWORK_PREFIX + "{chal}-{owner}"
PROXY_NAME = PROXY_PREFIX + "{chal}"

# The network the instancer and every proxy share. Instances are never on it;
# each proxy is given one address here and binds only that.
CONTROL_NETWORK = os.environ.get("CONTROL_NETWORK", "ctf-control")
INSTANCER_URL = os.environ.get("INSTANCER_URL", "http://instancer:%d" % LISTEN_PORT)

# Leave the low addresses of the control network to whatever compose brings up.
PROXY_ADDRESS_OFFSET = 10

# Where players reach the proxies, when that is not the host serving this page.
# A challenge may override it; most never do.
PROXY_HOST = os.environ.get("PROXY_HOST", "")

# The one secret shared with the proxies. Each proxy is handed a token of its
# own, derived from this, so a token taken off one proxy opens nothing but that
# challenge -- and none of them has to be stored anywhere.
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")

# Keys are what a player sends to the proxy, so they are the only credential in
# the system: long enough not to be guessed, short enough to paste.
KEY_BYTES = 16

# The environment variable that tells a challenge which port to listen on.
PORT_ENV = os.environ.get("PORT_ENV", "CHAL_PORT")

# The instancer's own name, the one thing that is written rather than configured.
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.yml")

# What a challenge gets when its config.yml does not say. Where an instance
# lives is the challenge's business now -- its own pool and its own port range,
# written next to its Dockerfile -- so these are only the shape of a sensible
# answer, not knobs: there is nowhere in the environment for a per-challenge one.
DEFAULT_TTL = 3600
DEFAULT_MEM_LIMIT = "512m"
DEFAULT_PIDS_LIMIT = 256
DEFAULT_SUBNET_POOL = "10.240.0.0/16"
DEFAULT_SUBNET_PREFIX = 24
DEFAULT_INSTANCE_PORTS = "30000-30100"
DEFAULT_MODE = "http"
MODES = ("http", "netcat")

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
# compose down` means "take it all down", and compose knows nothing about the
# containers we created -- instances or proxies -- so we have to. Set 0 to keep
# them across a deliberate stop as well.
REAP_ON_SHUTDOWN = os.environ.get("REAP_ON_SHUTDOWN", "1").lower() not in ("0", "false", "no")

# How long a shutdown waits for an in-flight create before tearing down anyway.
# Docker's own grace period is short; being late is worse than being rude.
SHUTDOWN_LOCK_WAIT = 5

# How often the reaper looks.
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "10"))

# Metadata we stash on Docker objects so a restart can recover the state.
LABEL_CHAL = "ctf.chal"
LABEL_OWNER = "ctf.owner"
LABEL_EXPIRES = "ctf.expires_at"
LABEL_SUBNET = "ctf.subnet"
LABEL_PORT = "ctf.port"
LABEL_KEY = "ctf.key"
LABEL_SPEC = "ctf.proxy_spec"

# What a player is told when something breaks. Docker's own messages name
# images, ports, subnets and container ids -- all of it ours to read in the log,
# none of it theirs to see in a browser.
ERROR_BUSY = "no free instance right now, try again in a moment"
ERROR_CREATE = "could not start your instance, try again in a moment"
ERROR_DESTROY = "could not stop your instance, try again in a moment"
ERROR_SHUTDOWN = "the instancer is going down, try again shortly"
ERROR_NO_CHAL = "no such challenge"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("instancer")


# --- names --------------------------------------------------------------------

DEFAULT_CONFIG = {"instancer_name": "SpawnZero"}


def read_yaml(path):
    """Load one config file. Returns a mapping, or None if it is not usable.

    A missing or broken file is never fatal here: an instancer that will not
    start because someone fat-fingered a display name would be a worse trade
    than one that starts with a placeholder and a complaint in the log.
    """
    try:
        with open(path) as handle:
            loaded = yaml.safe_load(handle) or {}
    except OSError as exc:
        log.warning("no config file at %s (%s)", path, exc)
        return None
    except yaml.YAMLError as exc:
        log.error("config file %s is not valid YAML (%s)", path, exc)
        return None
    if not isinstance(loaded, dict):
        log.error("config file %s is not a mapping", path)
        return None
    return loaded


def load_config(path=None):
    """Read the instancer's own config.yml. Names only -- nothing here changes
    how anything runs; that is what each challenge's config.yml is for."""
    path = path or CONFIG_FILE
    loaded = read_yaml(path)
    if loaded is None:
        log.warning("falling back to default names")
        return dict(DEFAULT_CONFIG)
    unknown = set(loaded) - set(DEFAULT_CONFIG)
    if unknown:
        log.warning("ignoring unknown key(s) in %s: %s", path, ", ".join(sorted(unknown)))
    config = dict(DEFAULT_CONFIG)
    config.update({key: str(loaded[key]) for key in DEFAULT_CONFIG
                   if loaded.get(key) is not None})
    return config


CONFIG = load_config()


# --- challenges ---------------------------------------------------------------

class Challenge:
    """One challenge: its directory, its image, its proxy, and what an instance
    of it is allowed to be.

    Everything here is per challenge by nature -- two challenges are two proxies
    on two ports, and a web app deserves a different ceiling than a pwn box --
    which is why it is written next to the Dockerfile rather than passed in from
    the environment, where there is only ever one of anything.
    """

    def __init__(self, cid, directory, config):
        self.id = cid
        self.dir = directory
        self.name = config["name"] or cid
        self.author = config["author"]
        self.type = config["type"]
        self.mode = config["mode"]
        self.proxy_port = config["proxy_port"]
        self.proxy_host = config["proxy_host"] or PROXY_HOST
        self.ttl = config["ttl"]
        self.mem_limit = config["mem_limit"]
        self.pids_limit = config["pids_limit"]
        self.subnet_pool = config["subnet_pool"]
        self.subnet_prefix = config["subnet_prefix"]
        self.port_min, self.port_max = config["instance_ports"]
        self.max_instances = config["max_instances"]
        self.image = CHALLENGE_IMAGE.format(chal=cid)
        self.proxy = PROXY_NAME.format(chal=cid)
        self.sigil = sigil(cid)

    @property
    def capacity(self):
        """How many instances of it can be up at once, whichever ceiling is
        lowest: the port range (one port each), the subnet pool (one /24 each),
        or a max_instances that says so outright."""
        ceilings = [self.port_max - self.port_min + 1,
                    2 ** (self.subnet_prefix - self.subnet_pool.prefixlen)]
        if self.max_instances:
            ceilings.append(self.max_instances)
        return min(ceilings)

    def __repr__(self):
        return "<Challenge %s on :%d (%s)>" % (self.id, self.proxy_port, self.mode)


def sigil(cid):
    """A 5x5 pixel mark for a challenge, in the shape of its id.

    Mirrored down the middle, like the badges stamped on a cartridge label:
    the same id always draws the same mark, and no two ids draw the same one
    often enough to matter. Purely something to look at -- but the id is what it
    looks at, which is the thing that is actually load-bearing here.
    """
    bits = [byte & 1 for byte in hashlib.sha256(cid.encode()).digest()[:15]]
    rows = []
    for row in range(5):
        half = bits[row * 3:row * 3 + 3]
        rows.append(half + half[1::-1])
    return rows


# What a challenge's config.yml may say, and what it means when it does not.
# `proxy_port` has no sensible default: a challenge nobody can reach is not one.
DEFAULT_CHALLENGE = {
    "name": "",
    "author": "",
    "type": "",
    "mode": DEFAULT_MODE,
    "proxy_port": 0,
    "proxy_host": "",
    "ttl": DEFAULT_TTL,
    "mem_limit": DEFAULT_MEM_LIMIT,
    "pids_limit": DEFAULT_PIDS_LIMIT,
    "subnet_pool": DEFAULT_SUBNET_POOL,
    "subnet_prefix": DEFAULT_SUBNET_PREFIX,
    "instance_ports": DEFAULT_INSTANCE_PORTS,
    "max_instances": 0,
}
CHALLENGE_INTS = ("proxy_port", "ttl", "pids_limit", "subnet_prefix", "max_instances")


def parse_ports(text):
    """`30000-30100`, or a single port. Returns (first, last), or None."""
    first, _, last = str(text).partition("-")
    try:
        low, high = int(first), int(last or first)
    except ValueError:
        return None
    return (low, high) if 1 <= low <= high <= 65535 else None


def load_challenge(cid, directory):
    """Read one challenge directory. Returns a Challenge, or None with a reason
    in the log -- one unusable challenge must not take the others down."""
    if not CHALLENGE_ID.match(cid):
        log.error("ignoring %s: a challenge id must be lowercase letters, digits "
                  "and dashes -- it goes in URLs and container names", cid)
        return None
    if not os.path.isfile(os.path.join(directory, "Dockerfile")):
        log.error("ignoring %s: no Dockerfile in %s", cid, directory)
        return None
    loaded = read_yaml(os.path.join(directory, CHALLENGE_CONFIG))
    if loaded is None:
        log.error("ignoring %s: no usable %s", cid, CHALLENGE_CONFIG)
        return None
    unknown = set(loaded) - set(DEFAULT_CHALLENGE)
    if unknown:
        log.warning("ignoring unknown key(s) in %s/%s: %s", cid, CHALLENGE_CONFIG,
                    ", ".join(sorted(unknown)))

    config = dict(DEFAULT_CHALLENGE)
    for key, fallback in DEFAULT_CHALLENGE.items():
        value = loaded.get(key)
        if value is None:
            continue
        if key in CHALLENGE_INTS:
            try:
                config[key] = int(value)
            except (TypeError, ValueError):
                log.warning("%s: %s=%r is not a number, using %r", cid, key, value, fallback)
        else:
            config[key] = str(value)

    if config["mode"] not in MODES:
        log.warning("%s: unknown mode %r, falling back to %s", cid, config["mode"],
                    DEFAULT_MODE)
        config["mode"] = DEFAULT_MODE
    if not 1 <= config["proxy_port"] <= 65535:
        log.error("ignoring %s: proxy_port %r is not a port players could reach",
                  cid, config["proxy_port"])
        return None
    if config["ttl"] <= 0:
        log.warning("%s: ttl %r is not a lifetime, using %d", cid, config["ttl"],
                    DEFAULT_TTL)
        config["ttl"] = DEFAULT_TTL

    ports = parse_ports(config["instance_ports"])
    if ports is None:
        log.warning("%s: instance_ports %r is not a port range, using %s", cid,
                    config["instance_ports"], DEFAULT_INSTANCE_PORTS)
        ports = parse_ports(DEFAULT_INSTANCE_PORTS)
    config["instance_ports"] = ports

    try:
        pool = ipaddress.ip_network(config["subnet_pool"])
    except ValueError as exc:
        log.warning("%s: subnet_pool %r is not a network (%s), using %s", cid,
                    config["subnet_pool"], exc, DEFAULT_SUBNET_POOL)
        pool = ipaddress.ip_network(DEFAULT_SUBNET_POOL)
    if not pool.prefixlen <= config["subnet_prefix"] <= pool.max_prefixlen:
        log.warning("%s: subnet_prefix /%d does not divide %s, using /%d", cid,
                    config["subnet_prefix"], pool, DEFAULT_SUBNET_PREFIX)
        config["subnet_prefix"] = DEFAULT_SUBNET_PREFIX
    config["subnet_pool"] = pool

    if config["max_instances"] < 0:
        log.warning("%s: max_instances %r is not a number of instances, using 0 "
                    "(no cap of its own)", cid, config["max_instances"])
        config["max_instances"] = 0
    challenge = Challenge(cid, directory, config)
    if challenge.max_instances and challenge.max_instances > challenge.capacity:
        # Not an error -- it just never gets to be the ceiling, and saying so
        # beats letting someone believe they raised a limit they did not.
        log.warning("%s: max_instances %d is above what its port range and subnet "
                    "pool allow (%d), so %d is the real limit", cid,
                    challenge.max_instances, challenge.capacity, challenge.capacity)
    return challenge


def load_challenges(path=None):
    """Every challenge under CHALLENGES_DIR, by id, in the order they are shown.

    Adding a challenge is a directory and a restart; nothing else in the system
    is told that challenges exist. Two of them wanting the same proxy port is
    the one clash worth refusing outright -- Docker would refuse the second
    container anyway, and later, with less to say about why.
    """
    path = path or CHALLENGES_DIR
    try:
        entries = sorted(os.listdir(path))
    except OSError as exc:
        log.error("cannot read %s (%s): serving no challenges", path, exc)
        return {}
    challenges, ports = {}, {}
    for cid in entries:
        directory = os.path.join(path, cid)
        if not os.path.isdir(directory):
            continue
        challenge = load_challenge(cid, directory)
        if challenge is None:
            continue
        clash = ports.get(challenge.proxy_port)
        if clash is not None:
            log.error("ignoring %s: proxy_port %d is already %s's",
                      cid, challenge.proxy_port, clash)
            continue
        ports[challenge.proxy_port] = cid
        challenges[cid] = challenge
    return challenges


CHALLENGES = load_challenges()


def challenge(chal):
    """The Challenge with this id, or None. Players hand us the id."""
    return CHALLENGES.get(chal)


app = Flask(__name__)
app.secret_key = SECRET_KEY or secrets.token_hex(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Two levels of locking, because the two things worth protecting cost very
# different amounts to hold.
#
#   pool_lock    guards the choice of a port and a subnet, and nothing else. It
#                is never held while Docker builds anything, so a hundred players
#                arriving at once queue on the pick (milliseconds) instead of on
#                each other's containers (a second or more, each).
#
#   owner locks  one per session per challenge: everything that touches *one*
#                instance goes through it -- create, destroy, the stale-container
#                sweep, the reaper. Two requests about the same instance queue up,
#                so a double-clicked START cannot build two, and a status poll
#                cannot delete a container that is still being started.
#
# The owner locks are a fixed set, picked by challenge and session id rather than
# a dict that would grow for every session an event ever sees. Two unrelated
# players may share one and briefly wait on each other; that is cheaper than the
# bookkeeping.
pool_lock = threading.Lock()
OWNER_STRIPES = 64
owner_locks = [threading.Lock() for _ in range(OWNER_STRIPES)]

# Claimed by a create that is still building. Docker is the record of what is in
# use, but only once the container exists -- a second or two later. Until then
# the claim lives here, or two players would be handed the same port. Ports are
# claimed per challenge, subnets across all of them, for the same reason each is
# picked that way.
reserved_ports = set()
reserved_subnets = set()

# Set once a shutdown starts, so a create racing the teardown cannot leave an
# instance behind after destroy_all() has already walked past it.
shutting_down = threading.Event()


def owner_lock(chal, owner):
    """The lock for one instance. Same challenge and session, same lock."""
    return owner_locks[hash((chal, owner)) % OWNER_STRIPES]

_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def owner_id():
    """Id of the current browser session, minted on first use. One session owns
    at most one instance of each challenge."""
    if "owner" not in session:
        session.permanent = True
        session["owner"] = secrets.token_hex(8)
    return session["owner"]


def container_name(chal, owner):
    return CONTAINER_NAME.format(chal=chal, owner=owner)


def network_name(chal, owner):
    return NETWORK_NAME.format(chal=chal, owner=owner)


def proxy_name(chal):
    return PROXY_NAME.format(chal=chal)


def name_parts(name, prefix):
    """(challenge, owner) back out of a name we made.

    Owner ids are hex and a challenge id may contain dashes, so the last dash is
    the seam. Only a fallback: both halves are on the object as labels.
    """
    chal, _, owner = name[len(prefix):].rpartition("-")
    return chal, owner


# --- instance lookup ----------------------------------------------------------

def get_instance(chal, owner):
    """Return this session's running container for a challenge, or None.

    A leftover stopped container (and its network) is removed so the name,
    port, subnet and key are free again.
    """
    try:
        container = client().containers.get(container_name(chal, owner))
    except NotFound:
        return None
    if container.status != "running":
        log.info("removing stale %s container of %s (status=%s)", chal, owner,
                 container.status)
        remove_instance(chal, owner)
        return None
    return container


def label(container, name):
    return (container.labels or {}).get(name)


def label_int(container, name):
    try:
        return int(label(container, name))
    except (TypeError, ValueError):
        return None


def instance_chal(container):
    return label(container, LABEL_CHAL) or name_parts(container.name, CONTAINER_PREFIX)[0]


def instance_owner(container):
    return label(container, LABEL_OWNER) or name_parts(container.name, CONTAINER_PREFIX)[1]


def instance_port(container):
    return label_int(container, LABEL_PORT)


def instance_expires_at(container):
    return label_int(container, LABEL_EXPIRES)


def instance_address(container):
    """The instance's address on its own network -- what the proxy dials."""
    networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    endpoint = networks.get(network_name(instance_chal(container),
                                         instance_owner(container))) or {}
    return endpoint.get("IPAddress") or None


def list_instances(chal=None):
    """Every instance container, running or not -- of one challenge, or of all."""
    prefix = CONTAINER_PREFIX + (chal + "-" if chal else "")
    return [c for c in client().containers.list(all=True) if c.name.startswith(prefix)]


def find_by_key(chal, key):
    """The running instance of this challenge a key belongs to, or None.

    Compared in constant time and against running containers only, so a key
    stops working the moment its instance does. Scoped to the challenge whose
    proxy is asking, so one key is one door and not a set of them.
    """
    if not key or not key.isascii():
        return None
    for container in list_instances(chal):
        if container.status != "running":
            continue
        if secrets.compare_digest(label(container, LABEL_KEY) or "", key):
            return container
    return None


# --- ports --------------------------------------------------------------------

def used_ports(chal, reserved=()):
    """Ports of this challenge that are spoken for: handed to an instance, or
    claimed by a create that is still building one.

    Per challenge, because the port lives inside the instance's own network
    namespace: two challenges may hand out the same number all day. One entry
    per instance, so the size of this set is also how many are out.
    """
    live = {port for port in (instance_port(c) for c in list_instances(chal))
            if port is not None}
    return live | {port for c, port in reserved if c == chal}


def pick_port(chal, taken):
    """A port free in this challenge's own range."""
    for port in range(chal.port_min, chal.port_max + 1):
        if port not in taken:
            return port
    raise RuntimeError("no free instance port in %d-%d for %s"
                       % (chal.port_min, chal.port_max, chal.id))


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


def pick_subnet(chal, reserved=()):
    """A subnet free in this challenge's own pool.

    Checked against every network on the daemon, not just this pool, so two
    challenges pointed at overlapping pools share the space instead of colliding
    in it -- they just run out sooner.
    """
    used = used_subnets() + list(reserved)
    for subnet in chal.subnet_pool.subnets(new_prefix=chal.subnet_prefix):
        if not any(subnet.overlaps(u) for u in used):
            return subnet
    raise RuntimeError("no free /%d subnet in %s for %s"
                       % (chal.subnet_prefix, chal.subnet_pool, chal.id))


# --- keys ---------------------------------------------------------------------

def pick_key():
    return secrets.token_hex(KEY_BYTES)


# --- reservations -------------------------------------------------------------

def reserve(chal):
    """Take a port and a subnet nobody else can be given.

    Both come out of this challenge's own config: its port range and its subnet
    pool. Its max_instances is checked here too, and for the same reason the
    picking is here -- under one lock, counting what Docker holds *and* what the
    creates still building have claimed, so two players arriving at once cannot
    both be handed the last slot. Held only long enough to read and write the
    claim; the slow part -- building the network and the container -- happens
    with the lock released, so everyone else can be picking meanwhile.
    """
    with pool_lock:
        taken = used_ports(chal.id, reserved_ports)
        if chal.max_instances and len(taken) >= chal.max_instances:
            raise RuntimeError("%s is full: %d of %d instances already out"
                               % (chal.id, len(taken), chal.max_instances))
        port = pick_port(chal, taken)
        subnet = pick_subnet(chal, reserved_subnets)
        reserved_ports.add((chal.id, port))
        reserved_subnets.add(subnet)
        return port, subnet


def release(chal, port, subnet):
    """Drop the claim -- either Docker holds the record now, or nothing does."""
    with pool_lock:
        reserved_ports.discard((chal, port))
        reserved_subnets.discard(subnet)


# --- create / destroy ---------------------------------------------------------

def create_network(chal, owner, subnet):
    """The instance's own network: internal, and ideally without a gateway."""
    kwargs = dict(
        driver="bridge",
        internal=True,
        ipam=docker.types.IPAMConfig(
            pool_configs=[docker.types.IPAMPool(subnet=str(subnet))]),
        labels={LABEL_CHAL: chal, LABEL_OWNER: owner, LABEL_SUBNET: str(subnet)},
        check_duplicate=True,
    )
    try:
        return client().networks.create(network_name(chal, owner),
                                        options=NETWORK_OPTIONS, **kwargs)
    except APIError as exc:
        log.warning("daemon rejected %s=isolated (%s); the instance network keeps "
                    "a reachable gateway address -- upgrade to Docker 28+ to close it",
                    GATEWAY_MODE_OPTION, exc)
        return client().networks.create(network_name(chal, owner), **kwargs)


def attach_proxy(chal, owner):
    """Give this challenge's proxy an interface on this instance's network.

    A proxy answers only on its control-network address, so this is a one-way
    door: the proxy can dial the instance, the instance finds nothing listening.
    And it is *this* challenge's proxy: no proxy is ever on a network belonging
    to a challenge that is not its own.
    """
    client().networks.get(network_name(chal, owner)).connect(proxy_name(chal))


def detach_proxy(network, chal):
    """Best effort: a half-built instance may never have got the proxy attached.

    A detach that really was needed and really failed is not swallowed -- the
    removal right after it fails loudly with "has active endpoints".
    """
    try:
        network.disconnect(proxy_name(chal), force=True)
    except (NotFound, APIError) as exc:
        log.debug("proxy not detached from %s: %s", network.name, exc)


def remove_network(chal, owner):
    """Remove this instance's network. Docker refuses while the proxy is on it."""
    try:
        network = client().networks.get(network_name(chal, owner))
    except NotFound:
        return
    detach_proxy(network, chal)
    try:
        network.remove()
    except APIError as exc:
        log.error("could not remove network for %s/%s: %s", chal, owner, exc)


def remove_instance(chal, owner):
    """Remove this instance's container and network. Safe if either is gone.

    Returns True if a container was actually removed.
    """
    removed = False
    try:
        client().containers.get(container_name(chal, owner)).remove(force=True)
        removed = True
    except NotFound:
        pass
    remove_network(chal, owner)
    return removed


def create_container(chal, owner, port, subnet, key, expires_at):
    limits = {}
    if chal.mem_limit:
        limits["mem_limit"] = chal.mem_limit
    if chal.pids_limit:
        limits["pids_limit"] = chal.pids_limit
    return client().containers.create(
        chal.image,
        name=container_name(chal.id, owner),
        # No ports=: publishing one would be a way in that skips the proxy.
        network=network_name(chal.id, owner),
        environment={PORT_ENV: str(port)},
        labels={
            LABEL_CHAL: chal.id,
            LABEL_OWNER: owner,
            LABEL_EXPIRES: str(expires_at),
            LABEL_SUBNET: str(subnet),
            LABEL_PORT: str(port),
            LABEL_KEY: key,
        },
        **limits,
    )


# --- images -------------------------------------------------------------------

def image_exists(tag):
    try:
        client().images.get(tag)
        return True
    except ImageNotFound:
        return False


def build_image(tag, path, what):
    # Building on every startup would block the web server for minutes each boot
    # (the SDK's builder re-runs every challenge's apt install). Build only when
    # the image is missing; set FORCE_BUILD=1 to rebuild after changing one.
    if image_exists(tag) and not FORCE_BUILD:
        log.info("%s image %s already present, skipping build "
                 "(set FORCE_BUILD=1 to rebuild)", what, tag)
        return
    log.info("building %s image %s from %s", what, tag, path)
    client().images.build(path=path, tag=tag, rm=True)
    log.info("%s image built: %s", what, tag)


# --- proxies ------------------------------------------------------------------

def proxy_token(chal):
    """The token this challenge's proxy presents when it resolves a key.

    Derived from the shared secret rather than stored, so it costs nothing to
    keep, survives a restart unchanged, and is not the same token as the one on
    the proxy next to it -- one taken off a proxy opens that challenge and no
    other.
    """
    return hmac.new(PROXY_TOKEN.encode(), chal.encode(), hashlib.sha256).hexdigest()


def proxy_spec(chal):
    """What a proxy container was built to be. Stamped on it, so a restart can
    tell "already running" from "running the previous config.yml"."""
    return "%s|%d|%s" % (chal.mode, chal.proxy_port, chal.image)


def control_network():
    return client().networks.get(CONTROL_NETWORK)


def pick_proxy_address(network):
    """A free address on the control network, for one proxy to bind."""
    network.reload()
    config = ((network.attrs.get("IPAM") or {}).get("Config") or [{}])[0]
    subnet = ipaddress.ip_network(config["Subnet"])
    taken = set()
    if config.get("Gateway"):
        taken.add(ipaddress.ip_address(config["Gateway"].split("/")[0]))
    for info in (network.attrs.get("Containers") or {}).values():
        if info.get("IPv4Address"):
            taken.add(ipaddress.ip_interface(info["IPv4Address"]).ip)
    for index, host in enumerate(subnet.hosts()):
        if index >= PROXY_ADDRESS_OFFSET and host not in taken:
            return str(host)
    raise RuntimeError("no free address on %s" % CONTROL_NETWORK)


def list_proxies():
    return [c for c in client().containers.list(all=True)
            if c.name.startswith(PROXY_PREFIX)]


def create_proxy(chal):
    """This challenge's proxy: one container, one published port, one address.

    The address is the point. A proxy binds only its control-network address, so
    the instance networks it is attached to later have nothing to connect back
    to -- which is why the instancer picks the address and hands it over, rather
    than letting the proxy guess which of its interfaces is the safe one.
    """
    network = control_network()
    address = pick_proxy_address(network)
    container = client().containers.create(
        PROXY_IMAGE,
        name=chal.proxy,
        hostname=chal.proxy,
        environment={
            "PROXY_BIND": address,
            "PROXY_PORT": str(chal.proxy_port),
            "PROXY_TOKEN": proxy_token(chal.id),
            "PROXY_CHAL": chal.id,
            "INSTANCER_URL": INSTANCER_URL,
            "MODE": chal.mode,
        },
        ports={"%d/tcp" % chal.proxy_port: chal.proxy_port},
        network=CONTROL_NETWORK,
        networking_config={
            CONTROL_NETWORK: client().api.create_endpoint_config(ipv4_address=address)},
        labels={LABEL_CHAL: chal.id, LABEL_SPEC: proxy_spec(chal)},
        restart_policy={"Name": "unless-stopped"},
    )
    container.start()
    log.info("proxy for %s up on %s:%d, bound %s", chal.id, chal.mode,
             chal.proxy_port, address)
    return container


def remove_proxy(chal):
    """Take a proxy away. Its instances' networks must be gone first -- Docker
    refuses to remove a container that still has endpoints in use."""
    try:
        client().containers.get(proxy_name(chal)).remove(force=True)
    except NotFound:
        return False
    return True


def ensure_proxy(chal):
    """The proxy this challenge needs, running the config.yml it has now.

    Adopted if it is already what it should be -- players mid-connection are not
    interrupted by a restart of the instancer. Replaced if the challenge's mode,
    port or image changed underneath it, because a proxy is only ever as right
    as the config it was created with.
    """
    try:
        container = client().containers.get(chal.proxy)
    except NotFound:
        return create_proxy(chal)
    if container.status == "running" and label(container, LABEL_SPEC) == proxy_spec(chal):
        log.info("adopted proxy for %s (%s:%d)", chal.id, chal.mode, chal.proxy_port)
        return container
    log.info("replacing proxy for %s (status=%s, spec=%s, wanted %s)", chal.id,
             container.status, label(container, LABEL_SPEC), proxy_spec(chal))
    container.remove(force=True)
    return create_proxy(chal)


def remove_stale_proxies():
    """Proxies of challenges that are no longer there.

    Done before the others are brought up: a removed challenge's proxy is still
    holding its published port, and the next challenge to want that port would
    otherwise fail for a reason that has nothing to do with it.
    """
    for container in list_proxies():
        chal = label(container, LABEL_CHAL) or container.name[len(PROXY_PREFIX):]
        if chal in CHALLENGES:
            continue
        log.info("removing proxy of unknown challenge %s", chal)
        for instance in list_instances(chal):
            remove_instance(chal, instance_owner(instance))
        container.remove(force=True)


# --- what a player is told ----------------------------------------------------

def instance_json(chal, container):
    """What the player's page gets: how to connect, and how long they have.

    Deliberately not in here: the instance's port, its address, its subnet, its
    container name. A player cannot route to any of it, so telling them only
    describes our machinery.
    """
    expires_at = instance_expires_at(container)
    remaining = max(0, expires_at - int(time.time())) if expires_at is not None else None
    return jsonify(
        chal=chal.id,
        name=chal.name,
        running=True,
        mode=chal.mode,
        key=label(container, LABEL_KEY),
        proxy_host=chal.proxy_host or None,
        proxy_port=chal.proxy_port,
        expires_at=expires_at,
        remaining_time=remaining,
    )


def idle_json(chal):
    return jsonify(chal=chal.id, name=chal.name, running=False, mode=chal.mode,
                   proxy_host=chal.proxy_host or None, proxy_port=chal.proxy_port)


def challenge_json(chal, owner):
    """One line of the index: what the challenge is, and whether you have one.

    Under the owner's lock for the same reason /status is: get_instance() sweeps
    away a container that is not running, and "not running yet" is what a
    container looks like while its own create is still building it. The list
    polls, so an index left open in one tab would otherwise delete the instance
    the next tab is starting.
    """
    container = None
    if owner:
        with owner_lock(chal.id, owner):
            container = get_instance(chal.id, owner)
    expires_at = instance_expires_at(container) if container is not None else None
    return {
        "chal": chal.id,
        "name": chal.name,
        "author": chal.author,
        "type": chal.type,
        "mode": chal.mode,
        "ttl": chal.ttl,
        "running": container is not None,
        "remaining_time": (max(0, expires_at - int(time.time()))
                           if expires_at is not None else None),
    }


def no_such_challenge():
    return jsonify(running=False, error=ERROR_NO_CHAL), 404


# --- routes -------------------------------------------------------------------

@app.get("/")
def index():
    # Mint the session here, so two fast clicks on Start cannot race each other
    # into two identities (and therefore two containers).
    owner_id()
    return render_template("index.html", challenges=list(CHALLENGES.values()), **CONFIG)


@app.get("/c/<chal>")
def challenge_page(chal):
    found = challenge(chal)
    if found is None:
        return render_template("missing.html", chal=chal, **CONFIG), 404
    owner_id()
    return render_template("challenge.html", chal=found, **CONFIG)


@app.get("/api/challenges")
def api_challenges():
    owner = session.get("owner")
    return jsonify(challenges=[challenge_json(c, owner) for c in CHALLENGES.values()])


@app.get("/api/<chal>/status")
def status(chal):
    found = challenge(chal)
    if found is None:
        return no_such_challenge()
    owner = session.get("owner")
    if owner is None:
        return idle_json(found)
    # Under the owner's lock: get_instance() sweeps away a container that is not
    # running, and "not running yet" is what a container looks like for the
    # moment between being created and being started. Polling while a create is
    # in flight would otherwise delete the instance being built.
    with owner_lock(chal, owner):
        container = get_instance(chal, owner)
        if container is None:
            return idle_json(found)
        return instance_json(found, container)


@app.post("/api/<chal>/create")
def create(chal):
    found = challenge(chal)
    if found is None:
        return no_such_challenge()
    owner = owner_id()
    if shutting_down.is_set():
        return jsonify(running=False, mode=found.mode, error=ERROR_SHUTDOWN), 503
    with owner_lock(chal, owner):
        container = get_instance(chal, owner)
        if container is not None:
            return instance_json(found, container)

        # Clear any orphan network left behind by a previous life of this owner.
        remove_network(chal, owner)

        port = subnet = None
        try:
            port, subnet = reserve(found)
            key = pick_key()
            expires_at = int(time.time()) + found.ttl
            create_network(chal, owner, subnet)
            attach_proxy(chal, owner)
            container = create_container(found, owner, port, subnet, key, expires_at)
            container.start()
            container.reload()
        except Exception as exc:
            log.error("create failed for %s/%s: %s", chal, owner, exc)
            try:
                remove_instance(chal, owner)
            except (DockerException, OSError) as cleanup_exc:
                log.error("cleanup after failed create: %s", cleanup_exc)
            # reserve() raises RuntimeError when the pools are full -- the one
            # failure a player can actually do something about (wait).
            if isinstance(exc, RuntimeError):
                return jsonify(running=False, mode=found.mode, error=ERROR_BUSY), 503
            return jsonify(running=False, mode=found.mode, error=ERROR_CREATE), 500
        finally:
            # Either the container exists and Docker records the claim, or
            # nothing does; either way ours is no longer needed.
            release(chal, port, subnet)

        log.info("instance created for %s/%s: %s port %d, subnet %s, ttl %ds",
                 chal, owner, instance_address(container) or "?", port, subnet,
                 found.ttl)
        return instance_json(found, container)


@app.post("/api/<chal>/destroy")
def destroy(chal):
    found = challenge(chal)
    if found is None:
        return no_such_challenge()
    owner = session.get("owner")
    if owner is None:
        return idle_json(found)
    with owner_lock(chal, owner):
        try:
            removed = remove_instance(chal, owner)
        except Exception as exc:
            log.error("destroy failed for %s/%s: %s", chal, owner, exc)
            return jsonify(running=True, mode=found.mode, error=ERROR_DESTROY), 500
    if removed:
        log.info("instance destroyed for %s/%s", chal, owner)
    return idle_json(found)


@app.get("/internal/route/<chal>/<key>")
def route(chal, key):
    """Key -> instance address. A challenge's own proxy is the only caller.

    Turning a key into an address is the whole authority in this system, so the
    call carries the token that belongs to this challenge and no other; without
    it -- or with a key nobody owns -- the answer is the same 404, and the proxy
    has nowhere to send the player.
    """
    presented = request.headers.get("X-Proxy-Token", "")
    if not PROXY_TOKEN or not presented.isascii() or chal not in CHALLENGES:
        return jsonify(error="not found"), 404
    if not secrets.compare_digest(presented, proxy_token(chal)):
        return jsonify(error="not found"), 404
    container = find_by_key(chal, key)
    host = instance_address(container) if container is not None else None
    if host is None:
        return jsonify(error="not found"), 404
    return jsonify(host=host, port=instance_port(container))


# --- TTL reaper ---------------------------------------------------------------

def reap_expired():
    """Destroy expired instances and drop networks whose container is gone.

    Works off labels, not off the loaded config, so the instances of a challenge
    that was taken off disk still expire on schedule instead of living forever.
    """
    for container in list_instances():
        chal, owner = instance_chal(container), instance_owner(container)
        if instance_expires_at(container) is None:
            continue
        with owner_lock(chal, owner):
            # Read it again under the lock: between the listing and here, this
            # owner may have destroyed and rebuilt, and the new one is not ours
            # to take.
            if not expired(chal, owner):
                continue
            log.info("instance for %s/%s expired, destroying", chal, owner)
            remove_instance(chal, owner)
    prune_orphan_networks()


def expired(chal, owner):
    try:
        container = client().containers.get(container_name(chal, owner))
    except NotFound:
        return False
    expires_at = instance_expires_at(container)
    return expires_at is not None and int(time.time()) >= expires_at


def prune_orphan_networks():
    for network in client().networks.list():
        if not network.name.startswith(NETWORK_PREFIX):
            continue
        chal, owner = name_parts(network.name, NETWORK_PREFIX)
        # A create makes the network first and the container a moment later.
        # Without the owner's lock this is exactly the window in which an
        # instance being built looks like an orphan.
        with owner_lock(chal, owner):
            try:
                client().containers.get(container_name(chal, owner))
                continue
            except NotFound:
                pass
            log.info("removing orphan network %s", network.name)
            detach_proxy(network, chal)
            try:
                network.remove()
            except APIError as exc:
                log.error("could not remove orphan network %s: %s", network.name, exc)


def destroy_all():
    """Tear down every instance there is, then every proxy.

    Each instance still goes through its owner's lock, but only briefly:
    Docker's grace period is short, and being late is worse than being rude to a
    create that is already doomed. Callers who mean "we are going away" raise
    `shutting_down` first, so a create arriving now is turned away instead of
    building something this walk has already passed. The proxies go last, and
    only once the networks they are attached to are gone.
    """
    for container in list_instances():
        chal, owner = instance_chal(container), instance_owner(container)
        held = owner_lock(chal, owner).acquire(timeout=SHUTDOWN_LOCK_WAIT)
        try:
            log.info("shutting down: destroying instance of %s/%s", chal, owner)
            remove_instance(chal, owner)
        finally:
            if held:
                owner_lock(chal, owner).release()
    prune_orphan_networks()
    for container in list_proxies():
        log.info("shutting down: removing %s", container.name)
        try:
            container.remove(force=True)
        except APIError as exc:
            log.error("could not remove %s: %s", container.name, exc)


def handle_shutdown(signum, frame):
    """SIGTERM/SIGINT: what `docker compose down` and Ctrl-C arrive as."""
    log.info("signal %d received, shutting down", signum)
    shutting_down.set()
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
                    "can get through any proxy")
    if not CHALLENGES:
        log.error("no usable challenge in %s: there is nothing to serve", CHALLENGES_DIR)
    try:
        control_network()
    except NotFound:
        log.error("no control network %r: the proxies would have nowhere to answer "
                  "and no way back to this process -- is this running under "
                  "docker-compose.yml?", CONTROL_NETWORK)
        raise
    build_image(PROXY_IMAGE, PROXY_DIR, "proxy")
    remove_stale_proxies()
    for chal in list(CHALLENGES.values()):
        try:
            build_image(chal.image, chal.dir, chal.id)
            ensure_proxy(chal)
        except (DockerException, OSError, RuntimeError) as exc:
            # One challenge that will not build or will not get a proxy is not a
            # reason for the others to stay down. Drop it and say so.
            log.error("dropping challenge %s: %s", chal.id, exc)
            del CHALLENGES[chal.id]
    for container in list_instances():
        log.info("adopted instance of %s/%s (%s), expires_at=%s",
                 instance_chal(container), instance_owner(container),
                 container.status, instance_expires_at(container))
    log.info("%s serving %d challenge(s)", CONFIG["instancer_name"], len(CHALLENGES))
    for chal in CHALLENGES.values():
        log.info("  %s on :%d (%s), ttl %ds, up to %d at once from %s in /%d",
                 chal.id, chal.proxy_port, chal.mode, chal.ttl, chal.capacity,
                 chal.subnet_pool, chal.subnet_prefix)
    log.info("server started on port %d (control network %s, reap on shutdown: %s)",
             LISTEN_PORT, CONTROL_NETWORK, "yes" if REAP_ON_SHUTDOWN else "no")


if __name__ == "__main__":
    startup()
    install_shutdown_handler()
    start_cleanup_thread()
    app.run(host="0.0.0.0", port=LISTEN_PORT)

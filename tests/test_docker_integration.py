"""Integration tests: the real Docker daemon, the real proxy, the real challenge.

The instancer runs in-process (a Flask server in a thread, so the proxy
container can call it back); everything else is what production runs.
Skipped automatically when no Docker daemon is reachable.
"""

import ipaddress
import socket
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

import app as instancer

REPO = Path(__file__).resolve().parents[1]
IMAGE = "spawnzero-challenge:test"
PROXY_IMAGE = "spawnzero-proxy:test"
PREFIX = "spawnzero-instance-test-"
NET_PREFIX = "spawnzero-network-test-"
SECRET = "integration-test-secret"
TOKEN = "integration-test-token"

# The proxy's own turf: one network shared with the instancer, one fixed address
# it binds, one published port players arrive on.
CONTROL_NET = "spawnzero-control-test"
CONTROL_SUBNET = "10.239.9.0/24"
PROXY_NAME = "spawnzero-proxy-test"
PROXY_IP = "10.239.9.10"
PROXY_PORT = 1337          # inside the proxy container; Docker picks the host one

PORT_MIN = 30000
PORT_MAX = 30005
SUBNET_POOL = "10.241.0.0/16"

docker = pytest.importorskip("docker")


# --- the world under test -----------------------------------------------------

@pytest.fixture(scope="module")
def real_client():
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.skip("no reachable Docker daemon: %s" % exc)
    remove_leftovers(client)
    client.images.build(path=str(REPO / "challenge"), tag=IMAGE, rm=True)
    client.images.build(path=str(REPO), dockerfile="Dockerfile.proxy",
                        tag=PROXY_IMAGE, rm=True)
    yield client
    remove_leftovers(client)
    client.images.remove(IMAGE, force=True)
    client.images.remove(PROXY_IMAGE, force=True)


@pytest.fixture(scope="module")
def world(real_client):
    """Configure the instancer, serve it, and put the proxy in front of it."""
    saved = {name: getattr(instancer, name) for name in CONFIG}
    for name, value in CONFIG.items():
        setattr(instancer, name, value)
    instancer.app.secret_key = SECRET

    control = real_client.networks.create(
        CONTROL_NET, driver="bridge",
        ipam=docker.types.IPAMConfig(
            pool_configs=[docker.types.IPAMPool(subnet=CONTROL_SUBNET)]))
    gateway = control.attrs["IPAM"]["Config"][0]["Gateway"]

    # The proxy asks the instancer where a key leads, so the instancer has to be
    # listening before the proxy starts -- it reaches us over the control gateway.
    server = make_server("0.0.0.0", 0, instancer.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.socket.getsockname()[1]
    proxy = start_proxy(real_client, "http://%s:%d" % (gateway, port))
    # Whatever host port Docker handed us is the one players would arrive on --
    # asking for a fixed one only picks a fight with whatever else is running.
    proxy.host_port = published_port(proxy)
    instancer.PROXY_PORT = proxy.host_port

    yield proxy
    server.shutdown()
    proxy.remove(force=True)
    control.remove()
    for name, value in saved.items():
        setattr(instancer, name, value)


CONFIG = {
    "CHALLENGE_IMAGE": IMAGE,
    "CONTAINER_PREFIX": PREFIX,
    "NETWORK_PREFIX": NET_PREFIX,
    "MODE": "netcat",
    "INSTANCE_PORT_MIN": PORT_MIN,
    "INSTANCE_PORT_MAX": PORT_MAX,
    "SUBNET_POOL": ipaddress.ip_network(SUBNET_POOL),
    "SUBNET_PREFIX": 24,
    "PROXY_CONTAINER": PROXY_NAME,
    "PROXY_TOKEN": TOKEN,
    "PROXY_PORT": 0,       # replaced once Docker has published the proxy
}


def start_proxy(client, instancer_url):
    """The proxy container, on the address it will bind and nothing else."""
    endpoint = client.api.create_endpoint_config(ipv4_address=PROXY_IP)
    container = client.api.create_container(
        PROXY_IMAGE, name=PROXY_NAME, ports=[PROXY_PORT],
        environment={
            "PROXY_BIND": PROXY_IP,
            "PROXY_PORT": str(PROXY_PORT),
            "PROXY_TOKEN": TOKEN,
            "INSTANCER_URL": instancer_url,
            "MODE": "netcat",
        },
        host_config=client.api.create_host_config(
            port_bindings={PROXY_PORT: ("127.0.0.1",)}),
        networking_config=client.api.create_networking_config(
            {CONTROL_NET: endpoint}))
    client.api.start(container)
    return client.containers.get(PROXY_NAME)


def published_port(container):
    container.reload()
    bindings = container.attrs["NetworkSettings"]["Ports"]["%d/tcp" % PROXY_PORT]
    return int(bindings[0]["HostPort"])


@pytest.fixture
def web(world):
    """A player's browser."""
    client = instancer.app.test_client()
    client.get("/")
    return client


@pytest.fixture(autouse=True)
def clean_slate(world):
    yield
    remove_instances(instancer.client())


def remove_leftovers(client):
    for name in (PROXY_NAME,):
        try:
            client.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass
    remove_instances(client)
    for network in client.networks.list():
        if network.name == CONTROL_NET:
            network.remove()


def remove_instances(client):
    for container in client.containers.list(all=True):
        if container.name.startswith(PREFIX):
            container.remove(force=True)
    for network in client.networks.list():
        if not network.name.startswith(NET_PREFIX):
            continue
        network.reload()
        for endpoint in (network.attrs.get("Containers") or {}):
            network.disconnect(endpoint, force=True)
        network.remove()


def port_of(container):
    """The instance's port -- a label, not something the player is handed."""
    return int(container.labels["spawnzero.port"])


def instances(client):
    return [c for c in client.containers.list() if c.name.startswith(PREFIX)]


def networks(client):
    # list() answers with a summary; only inspect knows who is attached.
    found = [n for n in client.networks.list() if n.name.startswith(NET_PREFIX)]
    for network in found:
        network.reload()
    return found


# --- talking to the proxy -----------------------------------------------------

def play(proxy, key, timeout=20):
    """Do what a player does: connect to the proxy, hand over the key, read."""
    deadline = time.time() + timeout
    while True:
        try:
            with socket.create_connection(("127.0.0.1", proxy.host_port), timeout=3) as sock:
                sock.settimeout(3)
                sock.recv(4096)                     # the key prompt
                sock.sendall(key.encode() + b"\n")
                data = b""
                while len(data) < 32:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    return data.decode(errors="replace")
        except OSError:
            pass
        if time.time() > deadline:
            raise AssertionError("nothing came back through the proxy")
        time.sleep(0.3)


def can_reach(container, host, port, shell="bash"):
    """Whether a container can open a TCP connection -- from inside it."""
    code, _ = container.exec_run(
        [shell, "-c", "timeout 2 %s -c 'exec 3<>/dev/tcp/%s/%d'" % (shell, host, port)])
    return code == 0


def dial_from(container, host, port, timeout=15):
    """Try to connect from inside a container, retrying while it boots."""
    deadline = time.time() + timeout
    while True:
        code, _ = container.exec_run(
            ["python", "-c",
             "import socket; socket.create_connection((%r, %d), 3)" % (host, port)])
        if code == 0 or time.time() > deadline:
            return code == 0
        time.sleep(0.3)


def default_routes(container):
    """The instance's default routes, read from inside it (destination 0.0.0.0)."""
    routes = container.exec_run(["cat", "/proc/net/route"]).output.decode()
    return [line for line in routes.splitlines()[1:] if line.split()[1] == "00000000"]


def address_on(container, network_name):
    container.reload()
    return container.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]


# --- lifecycle ----------------------------------------------------------------

def test_lifecycle(web, world):
    client = instancer.client()
    assert web.post("/destroy").get_json()["running"] is False

    created = web.post("/create", json={"ttl": 120}).get_json()
    assert created["running"] is True
    assert created["mode"] == "netcat"
    assert created["proxy_port"] == world.host_port
    assert 0 < created["remaining_time"] <= 120

    container = instances(client)[0]
    assert container.status == "running"
    owner = container.labels["spawnzero.owner"]
    assert container.labels["spawnzero.key"] == created["key"]
    assert PORT_MIN <= port_of(container) <= PORT_MAX

    # nothing of the instance is published: the proxy is the only way in
    assert not (container.attrs["NetworkSettings"]["Ports"] or {})

    net = networks(client)[0]
    assert net.name == instancer.NETWORK_PREFIX + owner
    assert net.attrs["Internal"] is True
    subnet = net.attrs["IPAM"]["Config"][0]["Subnet"]
    assert subnet.startswith("10.241.")
    assert container.labels["spawnzero.subnet"] == subnet

    # ...and the proxy is on that network, so it can dial in
    assert PROXY_NAME in {c["Name"] for c in net.attrs["Containers"].values()}

    # a second create does not start a second container/network
    assert web.post("/create").get_json()["key"] == created["key"]
    assert len(instances(client)) == 1
    assert len(networks(client)) == 1

    assert web.post("/destroy").get_json()["running"] is False
    assert instances(client) == []
    assert networks(client) == []          # the proxy was detached first


def test_the_key_gets_a_player_through_the_proxy(web, world):
    key = web.post("/create").get_json()["key"]
    assert "Special gifts" in play(world, key)


def test_another_key_gets_nobody_anywhere(web, world):
    web.post("/create").get_json()
    with socket.create_connection(("127.0.0.1", world.host_port), timeout=5) as sock:
        sock.settimeout(5)
        sock.recv(4096)
        sock.sendall(b"f" * 32 + b"\n")
        assert b"no instance" in sock.recv(4096)


def test_a_destroyed_instances_key_stops_working(web, world):
    key = web.post("/create").get_json()["key"]
    play(world, key)                         # it worked a moment ago
    web.post("/destroy")
    with socket.create_connection(("127.0.0.1", world.host_port), timeout=5) as sock:
        sock.settimeout(5)
        sock.recv(4096)
        sock.sendall(key.encode() + b"\n")
        assert b"no instance" in sock.recv(4096)


# --- isolation ----------------------------------------------------------------

def test_an_instance_can_reach_nothing_but_its_own_subnet(web, world):
    client = instancer.client()
    web.post("/create")
    instance = instances(client)[0]
    network = networks(client)[0].name

    # The proxy is on the instance's network -- and answers on none of it.
    proxy_here = address_on(world, network)
    assert not can_reach(instance, proxy_here, PROXY_PORT)

    # The control network, where the proxy and the instancer actually live.
    assert not can_reach(instance, PROXY_IP, PROXY_PORT)

    # And the host: an instance with no gateway has no route off its own /24.
    assert default_routes(instance) == []


def test_the_proxy_can_reach_the_instance(web, world):
    web.post("/create")
    client = instancer.client()
    instance = instances(client)[0]
    network = networks(client)[0].name
    assert dial_from(world, address_on(instance, network), port_of(instance))


def test_a_real_crowd_never_shares_a_resource(world, monkeypatch):
    """Twelve players pressing START at once, against the real daemon."""
    monkeypatch.setattr(instancer, "INSTANCE_PORT_MAX", PORT_MIN + 20)
    client = instancer.client()
    players = []
    for _ in range(12):
        browser = instancer.app.test_client()
        browser.get("/")
        players.append(browser)

    answers = []
    threads = [threading.Thread(target=lambda b=b: answers.append(b.post("/create").get_json()))
               for b in players]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(a["running"] for a in answers), answers
    running = instances(client)
    assert len(running) == 12
    assert len({port_of(c) for c in running}) == 12
    assert len({c.labels["spawnzero.subnet"] for c in running}) == 12
    assert len({a["key"] for a in answers}) == 12
    # and every one of them is really on a network of its own, with the proxy
    assert len(networks(client)) == 12


def test_two_sessions_are_strangers(web, world):
    client = instancer.client()
    other = instancer.app.test_client()
    other.get("/")

    mine = web.post("/create").get_json()
    theirs = other.post("/create").get_json()
    assert mine["key"] != theirs["key"]
    assert len(instances(client)) == 2
    assert len({port_of(c) for c in instances(client)}) == 2
    assert len({n.attrs["IPAM"]["Config"][0]["Subnet"] for n in networks(client)}) == 2

    # neither instance's network can carry a packet to the other
    a, b = instances(client)
    net_a = instancer.network_name(a.labels["spawnzero.owner"])
    assert not can_reach(b, address_on(a, net_a), port_of(a))


# --- reaping and failure ------------------------------------------------------

def test_reaper_destroys_expired_instance(web, world):
    client = instancer.client()
    web.post("/create", json={"ttl": 1})
    assert len(instances(client)) == 1
    time.sleep(2)
    instancer.reap_expired()
    assert instances(client) == []
    assert networks(client) == []


def test_shutdown_takes_the_instances_with_it(web, world):
    client = instancer.client()
    web.post("/create")
    assert len(instances(client)) == 1
    instancer.destroy_all()
    assert instances(client) == []
    assert networks(client) == []     # the proxy was detached from each of them


def test_create_fails_cleanly_on_bad_image(web, world, monkeypatch):
    client = instancer.client()
    monkeypatch.setattr(instancer, "CHALLENGE_IMAGE", "spawnzero-challenge:does-not-exist")
    response = web.post("/create")
    assert response.status_code == 500
    assert response.get_json()["running"] is False
    assert instances(client) == []
    assert networks(client) == []          # network rolled back, proxy detached

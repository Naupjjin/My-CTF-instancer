"""Integration tests: the real Docker daemon and the real challenge image.

Skipped automatically when no Docker daemon is reachable.
"""

import importlib.util
import socket
import time
from pathlib import Path

import pytest

import app as instancer

REPO = Path(__file__).resolve().parents[1]
IMAGE = "ctf-challenge:test"
PREFIX = "ctf-instance-test-"
NET_PREFIX = "ctf-network-test-"
SECRET = "integration-test-secret"
PORT_MIN = 30000
PORT_MAX = 30005

docker = pytest.importorskip("docker")


@pytest.fixture(scope="module")
def real_client():
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.skip("no reachable Docker daemon: %s" % exc)
    remove_leftovers(client)
    client.images.build(path=str(REPO / "challenge"), tag=IMAGE, rm=True)
    yield client
    remove_leftovers(client)
    client.images.remove(IMAGE, force=True)


@pytest.fixture(autouse=True)
def clean_slate(real_client):
    remove_leftovers(real_client)


def remove_leftovers(client):
    for container in client.containers.list(all=True):
        if container.name.startswith(PREFIX):
            container.remove(force=True)
    for network in client.networks.list():
        if network.name.startswith(NET_PREFIX):
            network.remove()


def instances(client):
    return [c for c in client.containers.list() if c.name.startswith(PREFIX)]


def networks(client):
    return [n for n in client.networks.list() if n.name.startswith(NET_PREFIX)]


def configure(module, monkeypatch):
    monkeypatch.setattr(module, "CHALLENGE_IMAGE", IMAGE)
    monkeypatch.setattr(module, "CONTAINER_PREFIX", PREFIX)
    monkeypatch.setattr(module, "NETWORK_PREFIX", NET_PREFIX)
    monkeypatch.setattr(module, "CONTAINER_PORT", 41240)
    monkeypatch.setattr(module, "MODE", "netcat")
    monkeypatch.setattr(module, "PORT_MIN", PORT_MIN)
    monkeypatch.setattr(module, "PORT_MAX", PORT_MAX)
    monkeypatch.setattr(module.app, "secret_key", SECRET)
    client = module.app.test_client()
    client.get("/")
    return client


def load_fresh_module():
    spec = importlib.util.spec_from_file_location("app_restarted", REPO / "instancer-core" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_over_tcp(port, timeout=15):
    """Connect to the pwn service and read its banner, waiting for it to boot."""
    deadline = time.time() + timeout
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(b"0 0\n")
                sock.settimeout(3)
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
            raise AssertionError("challenge never answered on port %d" % port)
        time.sleep(0.3)


def bridge_reachable(container):
    for network in container.attrs["NetworkSettings"]["Networks"].values():
        ip = network.get("IPAddress")
        if not ip:
            continue
        try:
            socket.create_connection((ip, 41240), timeout=3).close()
            return True
        except OSError:
            continue
    return False


def test_lifecycle(real_client, monkeypatch):
    web = configure(instancer, monkeypatch)

    assert web.post("/destroy").get_json() == {"running": False, "mode": "netcat"}

    created = web.post("/create", json={"ttl": 120}).get_json()
    assert created["running"] is True
    assert PORT_MIN <= created["port"] <= PORT_MAX
    assert created["mode"] == "netcat"
    assert 0 < created["remaining_time"] <= 120
    port = created["port"]

    # the container runs on its own network, mapped to the advertised host port
    container = instances(real_client)[0]
    assert container.status == "running"
    owner = container.labels["ctf.owner"]
    assert container.labels["ctf.expires_at"].isdigit()

    net = networks(real_client)[0]
    assert net.name == instancer.NETWORK_PREFIX + owner
    subnet = net.attrs["IPAM"]["Config"][0]["Subnet"]
    assert subnet.startswith("10.100.")
    assert container.labels["ctf.subnet"] == subnet

    bindings = container.attrs["NetworkSettings"]["Ports"]["41240/tcp"]
    assert str(port) in [b["HostPort"] for b in bindings]

    # a restarted process adopts the same container + reads its TTL from labels
    restarted = load_fresh_module()
    restarted_web = configure(restarted, monkeypatch)
    restarted_web.set_cookie("session", web.get_cookie("session").value)
    adopted = restarted_web.get("/status").get_json()
    assert adopted["port"] == port
    assert adopted["expires_at"] == created["expires_at"]
    assert len(instances(real_client)) == 1

    # a second create does not start a second container/network
    assert web.post("/create").get_json()["port"] == port
    assert len(instances(real_client)) == 1
    assert len(networks(real_client)) == 1

    # destroy removes both container and network
    assert web.post("/destroy").get_json() == {"running": False, "mode": "netcat"}
    assert instances(real_client) == []
    assert networks(real_client) == []
    assert web.post("/destroy").get_json() == {"running": False, "mode": "netcat"}


def test_two_sessions_get_two_networks(real_client, monkeypatch):
    a = configure(instancer, monkeypatch)
    b = instancer.app.test_client()
    b.get("/")

    pa = a.post("/create").get_json()["port"]
    pb = b.post("/create").get_json()["port"]
    try:
        assert pa != pb
        assert len(instances(real_client)) == 2
        assert len(networks(real_client)) == 2
        subnets = {n.attrs["IPAM"]["Config"][0]["Subnet"] for n in networks(real_client)}
        assert len(subnets) == 2
    finally:
        a.post("/destroy")
        b.post("/destroy")
    assert networks(real_client) == []


def test_reaper_destroys_expired_instance(real_client, monkeypatch):
    web = configure(instancer, monkeypatch)
    web.post("/create", json={"ttl": 1})
    assert len(instances(real_client)) == 1
    time.sleep(2)
    instancer.reap_expired()
    assert instances(real_client) == []
    assert networks(real_client) == []


def test_challenge_answers_over_netcat(real_client, monkeypatch):
    """The published host port must actually serve the pwn binary.

    Some hosts block host<->docker-bridge traffic; there the port mapping is all
    the instancer can be held responsible for, so we skip loudly instead of
    reporting a false pass.
    """
    web = configure(instancer, monkeypatch)
    port = web.post("/create").get_json()["port"]
    try:
        try:
            banner = fetch_over_tcp(port, timeout=10)
        except AssertionError:
            if bridge_reachable(instances(real_client)[0]):
                raise
            pytest.skip("host cannot reach docker container networking (port mapping verified instead)")
        assert "Special gifts" in banner
    finally:
        web.post("/destroy")


def test_create_fails_cleanly_on_bad_image(real_client, monkeypatch):
    web = configure(instancer, monkeypatch)
    monkeypatch.setattr(instancer, "CHALLENGE_IMAGE", "ctf-challenge:does-not-exist")
    response = web.post("/create")
    assert response.status_code == 500
    assert response.get_json()["running"] is False
    assert instances(real_client) == []
    assert networks(real_client) == []   # network rolled back

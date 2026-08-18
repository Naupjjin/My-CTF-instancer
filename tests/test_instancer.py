"""Unit tests: the instancer logic against a fake Docker daemon."""

import ipaddress
import socket
import threading
import time

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

import app as instancer


# --- fake Docker daemon -------------------------------------------------------

class FakeContainer:
    def __init__(self, daemon, name, ports, labels, network):
        self.daemon = daemon
        self.name = name
        self.ports = ports
        self.labels = dict(labels or {})
        self.network = network
        self.status = "created"
        self.attrs = {"NetworkSettings": {"Ports": {}}}

    def start(self):
        if self.daemon.fail_start:
            raise APIError("start failed")
        self.status = "running"
        self.attrs = {
            "NetworkSettings": {
                "Ports": {
                    key: [{"HostIp": "0.0.0.0", "HostPort": str(value)}]
                    for key, value in self.ports.items()
                }
            }
        }

    def reload(self):
        pass

    def remove(self, force=False):
        self.daemon.containers.pop(self.name, None)


class FakeContainerCollection:
    def __init__(self, daemon):
        self.daemon = daemon

    def get(self, name):
        if name not in self.daemon.containers:
            raise NotFound(name)
        return self.daemon.containers[name]

    def create(self, image, name=None, ports=None, network=None, labels=None, **kwargs):
        self.daemon.create_calls += 1
        time.sleep(self.daemon.create_delay)
        if name in self.daemon.containers:
            raise APIError("name conflict")
        if network is not None and network not in self.daemon.networks:
            raise APIError("no such network: %s" % network)
        container = FakeContainer(self.daemon, name, ports or {}, labels, network)
        self.daemon.containers[name] = container
        return container

    def list(self, all=False, filters=None):
        containers = list(self.daemon.containers.values())
        if not all:
            containers = [c for c in containers if c.status == "running"]
        return containers


class FakeNetwork:
    def __init__(self, daemon, name, subnet, labels):
        self.daemon = daemon
        self.name = name
        self.labels = dict(labels or {})
        self.attrs = {"IPAM": {"Config": [{"Subnet": subnet}] if subnet else []}}

    def remove(self):
        self.daemon.networks.pop(self.name, None)


class FakeNetworkCollection:
    def __init__(self, daemon):
        self.daemon = daemon

    def get(self, name):
        if name not in self.daemon.networks:
            raise NotFound(name)
        return self.daemon.networks[name]

    def create(self, name, driver=None, ipam=None, labels=None, check_duplicate=None):
        self.daemon.network_calls += 1
        if check_duplicate and name in self.daemon.networks:
            raise APIError("network %s already exists" % name)
        subnet = None
        if ipam:
            configs = ipam.get("Config") or []
            if configs:
                subnet = configs[0].get("Subnet")
        network = FakeNetwork(self.daemon, name, subnet, labels)
        self.daemon.networks[name] = network
        return network

    def list(self, names=None):
        return list(self.daemon.networks.values())


class FakeImageCollection:
    def __init__(self, daemon):
        self.daemon = daemon

    def get(self, name):
        if name not in self.daemon.images:
            raise ImageNotFound(name)
        return object()

    def build(self, path=None, tag=None, rm=False):
        self.daemon.builds.append((path, tag))
        self.daemon.images.add(tag)
        return object(), []


class FakeDaemon:
    def __init__(self):
        self.containers_collection = FakeContainerCollection(self)
        self.networks_collection = FakeNetworkCollection(self)
        self.images_collection = FakeImageCollection(self)
        self.containers = {}
        self.networks = {}
        self.images = set()
        self.builds = []
        self.create_calls = 0
        self.network_calls = 0
        self.create_delay = 0
        self.fail_start = False

    def add_network(self, name, subnet, labels=None):
        self.networks[name] = FakeNetwork(self, name, subnet, labels or {})


class FakeClient:
    def __init__(self, daemon):
        self.containers = daemon.containers_collection
        self.networks = daemon.networks_collection
        self.images = daemon.images_collection


@pytest.fixture
def daemon(monkeypatch):
    fake = FakeDaemon()
    monkeypatch.setattr(instancer, "_client", FakeClient(fake))
    monkeypatch.setattr(instancer, "PORT_MIN", 31000)
    monkeypatch.setattr(instancer, "PORT_MAX", 31010)
    monkeypatch.setattr(instancer, "SUBNET_POOL", ipaddress.ip_network("10.100.0.0/16"))
    monkeypatch.setattr(instancer, "SUBNET_PREFIX", 24)
    monkeypatch.setattr(instancer, "DEFAULT_TTL", 3600)
    monkeypatch.setattr(instancer, "MAX_TTL", 86400)
    monkeypatch.setattr(instancer, "MODE", "http")
    monkeypatch.setattr(instancer.app, "secret_key", "test-secret")
    return fake


@pytest.fixture
def web(daemon):
    """One user: a Flask test client keeps its session cookie between calls."""
    client = instancer.app.test_client()
    client.get("/")  # like a browser, pick up the session cookie
    return client


@pytest.fixture
def other(daemon):
    client = instancer.app.test_client()
    client.get("/")
    return client


def only_container(daemon):
    assert len(daemon.containers) == 1
    return next(iter(daemon.containers.values()))


def only_network(daemon):
    assert len(daemon.networks) == 1
    return next(iter(daemon.networks.values()))


# --- UI + status --------------------------------------------------------------

def test_index_serves_ui(web):
    body = web.get("/").get_data(as_text=True)
    assert "CTF INSTANCER" in body
    assert "START" in body
    assert "STOP" in body


def test_status_without_instance(web):
    assert web.get("/status").get_json() == {"running": False, "mode": "http"}


def test_session_comes_from_the_page_not_from_polling(daemon):
    client = instancer.app.test_client()
    client.get("/status")
    assert client.get_cookie("session") is None
    client.get("/")
    assert client.get_cookie("session") is not None


# --- create / status / destroy ------------------------------------------------

def test_create_starts_container_and_network(web, daemon):
    data = web.post("/create").get_json()
    assert data["running"] is True
    assert instancer.PORT_MIN <= data["port"] <= instancer.PORT_MAX
    assert data["mode"] == "http"
    assert data["expires_at"] is not None
    assert 0 < data["remaining_time"] <= 3600
    assert daemon.create_calls == 1
    assert daemon.network_calls == 1

    container = only_container(daemon)
    assert container.status == "running"
    assert container.network == instancer.network_name(container.labels["ctf.owner"])
    only_network(daemon)


def test_status_reports_running_instance(web, daemon):
    created = web.post("/create").get_json()
    status = web.get("/status").get_json()
    assert status["running"] is True
    assert status["port"] == created["port"]
    assert status["mode"] == "http"
    assert status["expires_at"] == created["expires_at"]
    assert status["remaining_time"] <= created["remaining_time"]


def test_create_is_idempotent_within_a_session(web, daemon):
    first = web.post("/create").get_json()
    second = web.post("/create").get_json()
    assert first["port"] == second["port"]
    assert daemon.create_calls == 1
    assert daemon.network_calls == 1
    assert len(daemon.containers) == 1
    assert len(daemon.networks) == 1


def test_destroy_removes_container_and_network(web, daemon):
    web.post("/create")
    assert web.post("/destroy").get_json() == {"running": False, "mode": "http"}
    assert daemon.containers == {}
    assert daemon.networks == {}
    assert web.get("/status").get_json() == {"running": False, "mode": "http"}


def test_destroy_without_container(web):
    assert web.post("/destroy").status_code == 200
    assert web.post("/destroy").get_json() == {"running": False, "mode": "http"}


def test_failed_create_cleans_up_container_and_network(web, daemon):
    daemon.fail_start = True
    response = web.post("/create")
    assert response.status_code == 500
    assert response.get_json()["running"] is False
    assert daemon.containers == {}
    assert daemon.networks == {}          # network rolled back too
    assert web.get("/status").get_json() == {"running": False, "mode": "http"}

    daemon.fail_start = False
    retry = web.post("/create").get_json()
    assert retry["running"] is True
    assert retry["port"] == instancer.PORT_MIN   # port not wrongly marked used


def test_concurrent_create_in_one_session(daemon):
    daemon.create_delay = 0.2
    web = instancer.app.test_client()
    web.get("/")
    results = []
    run_together([lambda: results.append(web.post("/create").get_json())] * 2)
    assert daemon.create_calls == 1
    assert len(daemon.containers) == 1
    assert len(daemon.networks) == 1
    assert results[0]["port"] == results[1]["port"]


def run_together(workers):
    threads = [threading.Thread(target=worker) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# --- restart / persistence ----------------------------------------------------

def test_existing_container_is_adopted_across_restart(web, daemon):
    created = web.post("/create").get_json()
    # a fresh Flask client (simulating a restarted process) with the same cookie
    restarted = instancer.app.test_client()
    restarted.set_cookie("session", web.get_cookie("session").value)

    status = restarted.get("/status").get_json()
    assert status["port"] == created["port"]
    assert status["expires_at"] == created["expires_at"]
    assert restarted.post("/create").get_json()["port"] == created["port"]
    assert daemon.create_calls == 1   # not recreated


def test_stale_container_and_network_are_cleaned_up(web, daemon):
    web.post("/create")
    only_container(daemon).status = "exited"
    assert web.get("/status").get_json() == {"running": False, "mode": "http"}
    assert daemon.containers == {}
    assert daemon.networks == {}


def test_labels_carry_metadata(web, daemon):
    web.post("/create")
    container = only_container(daemon)
    owner = container.labels["ctf.owner"]
    assert container.labels["ctf.expires_at"].isdigit()
    assert ipaddress.ip_network(container.labels["ctf.subnet"]).prefixlen == 24
    network = only_network(daemon)
    assert network.labels["ctf.owner"] == owner
    assert network.labels["ctf.subnet"] == container.labels["ctf.subnet"]


# --- ports --------------------------------------------------------------------

def test_ports_outside_range_are_never_used(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "PORT_MIN", 31020)
    monkeypatch.setattr(instancer, "PORT_MAX", 31021)
    assert web.post("/create").get_json()["port"] in (31020, 31021)


def test_create_fails_when_port_range_exhausted(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "PORT_MIN", 31030)
    monkeypatch.setattr(instancer, "PORT_MAX", 31030)
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 31030))
    blocker.listen(1)
    try:
        response = web.post("/create")
        assert response.status_code == 500
        assert "no free host port" in response.get_json()["error"]
        assert daemon.containers == {}
        assert daemon.networks == {}
    finally:
        blocker.close()


def test_pick_port_skips_port_used_by_another_process(daemon):
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", instancer.PORT_MIN))
    blocker.listen(1)
    try:
        assert instancer.pick_port() == instancer.PORT_MIN + 1
    finally:
        blocker.close()


def test_pick_port_skips_port_used_by_another_container(daemon):
    other = FakeContainer(daemon, "someone-else", {"9999/tcp": instancer.PORT_MIN}, {}, None)
    daemon.containers["someone-else"] = other
    other.start()
    assert instancer.pick_port() == instancer.PORT_MIN + 1


# --- subnets ------------------------------------------------------------------

def test_two_sessions_get_distinct_subnets(web, other, daemon):
    web.post("/create")
    other.post("/create")
    subnets = {c.labels["ctf.subnet"] for c in daemon.containers.values()}
    assert len(subnets) == 2
    assert len(daemon.networks) == 2


def test_pick_subnet_skips_already_used_subnet(daemon):
    daemon.add_network("someone", "10.100.0.0/24")
    assert instancer.pick_subnet() == ipaddress.ip_network("10.100.1.0/24")


def test_pick_subnet_ignores_networks_outside_the_pool(daemon):
    daemon.add_network("bridge", "172.17.0.0/16")
    assert instancer.pick_subnet() == ipaddress.ip_network("10.100.0.0/24")


def test_pick_subnet_exhaustion_raises(daemon, monkeypatch):
    monkeypatch.setattr(instancer, "SUBNET_POOL", ipaddress.ip_network("10.100.0.0/24"))
    daemon.add_network("taken", "10.100.0.0/24")
    with pytest.raises(RuntimeError, match="no free"):
        instancer.pick_subnet()


def test_subnets_come_from_the_pool(web, daemon):
    subnet = ipaddress.ip_network(web.post("/create").get_json() and
                                  only_container(daemon).labels["ctf.subnet"])
    assert subnet.subnet_of(instancer.SUBNET_POOL)
    assert subnet.prefixlen == 24


# --- TTL ----------------------------------------------------------------------

def test_ttl_from_request_body(web, daemon):
    before = int(time.time())
    data = web.post("/create", json={"ttl": 100}).get_json()
    assert before + 100 <= data["expires_at"] <= int(time.time()) + 100
    assert 0 < data["remaining_time"] <= 100


def test_ttl_defaults_without_body(web, daemon):
    data = web.post("/create").get_json()
    assert data["remaining_time"] > 3000  # ~3600 default


def test_ttl_invalid_falls_back_to_default(web, daemon):
    data = web.post("/create", json={"ttl": "not-a-number"}).get_json()
    assert data["remaining_time"] > 3000


def test_ttl_non_positive_falls_back_to_default(web, daemon):
    data = web.post("/create", json={"ttl": -5}).get_json()
    assert data["remaining_time"] > 3000


def test_ttl_capped_at_max(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "MAX_TTL", 500)
    data = web.post("/create", json={"ttl": 999999}).get_json()
    assert data["remaining_time"] <= 500


# --- reaper -------------------------------------------------------------------

def test_reaper_destroys_expired_instance(web, daemon):
    web.post("/create")
    container = only_container(daemon)
    container.labels["ctf.expires_at"] = str(int(time.time()) - 1)   # already expired
    instancer.reap_expired()
    assert daemon.containers == {}
    assert daemon.networks == {}


def test_reaper_keeps_live_instance(web, daemon):
    web.post("/create")
    instancer.reap_expired()
    assert len(daemon.containers) == 1
    assert len(daemon.networks) == 1


def test_reaper_only_kills_the_expired_one(web, other, daemon):
    web.post("/create", json={"ttl": 100})
    other.post("/create", json={"ttl": 100})
    # expire exactly one of them
    victim = next(c for c in daemon.containers.values())
    victim.labels["ctf.expires_at"] = str(int(time.time()) - 1)
    instancer.reap_expired()
    assert len(daemon.containers) == 1
    assert victim.name not in daemon.containers
    assert len(daemon.networks) == 1


def test_reaper_prunes_orphan_network(daemon):
    daemon.add_network(instancer.network_name("ghost"), "10.100.9.0/24")
    instancer.reap_expired()
    assert daemon.networks == {}


def test_reaper_keeps_network_with_container(web, daemon):
    web.post("/create")
    instancer.prune_orphan_networks()
    assert len(daemon.networks) == 1


# --- mode ---------------------------------------------------------------------

def test_netcat_mode_is_reported(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "MODE", "netcat")
    assert web.post("/create").get_json()["mode"] == "netcat"
    assert web.get("/status").get_json()["mode"] == "netcat"
    assert web.post("/destroy").get_json()["mode"] == "netcat"


def test_index_exposes_mode_to_the_page(daemon, monkeypatch):
    monkeypatch.setattr(instancer, "MODE", "netcat")
    body = instancer.app.test_client().get("/").get_data(as_text=True)
    assert '"netcat"' in body


def test_unknown_mode_falls_back_to_http(monkeypatch):
    import importlib
    monkeypatch.setenv("MODE", "carrier-pigeon")
    reloaded = importlib.reload(instancer)
    assert reloaded.MODE == "http"


# --- multi-session isolation --------------------------------------------------

def test_a_session_only_sees_its_own_instance(web, other, daemon):
    web.post("/create")
    assert other.get("/status").get_json() == {"running": False, "mode": "http"}


def test_a_session_cannot_destroy_another(web, other, daemon):
    web.post("/create")
    assert other.post("/destroy").get_json() == {"running": False, "mode": "http"}
    assert web.get("/status").get_json()["running"] is True
    assert len(daemon.containers) == 1


def test_startup_builds_image_when_missing(daemon):
    instancer.startup()
    assert daemon.builds == [(instancer.CHALLENGE_DIR, instancer.CHALLENGE_IMAGE)]


def test_startup_skips_build_when_image_present(daemon):
    daemon.images.add(instancer.CHALLENGE_IMAGE)   # already built
    instancer.startup()
    assert daemon.builds == []                     # no rebuild -> fast startup


def test_force_build_rebuilds_even_when_present(daemon, monkeypatch):
    daemon.images.add(instancer.CHALLENGE_IMAGE)
    monkeypatch.setattr(instancer, "FORCE_BUILD", True)
    instancer.startup()
    assert daemon.builds == [(instancer.CHALLENGE_DIR, instancer.CHALLENGE_IMAGE)]

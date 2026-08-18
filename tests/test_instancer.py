"""Unit tests: the instancer logic against a fake Docker daemon."""

import ipaddress
import re
import threading
import time

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

import app as instancer

TOKEN = "test-proxy-token"


# --- fake Docker daemon -------------------------------------------------------

class FakeContainer:
    def __init__(self, daemon, name, environment, labels, network):
        self.daemon = daemon
        self.name = name
        self.environment = dict(environment or {})
        self.labels = dict(labels or {})
        self.network = network
        self.status = "created"
        self.attrs = {"NetworkSettings": {"Networks": {}}}
        if network is not None:
            daemon.networks[network].connect(name)

    @property
    def ip(self):
        return self.daemon.networks[self.network].endpoints[self.name]

    def start(self):
        if self.daemon.fail_start:
            raise APIError("start failed")
        self.status = "running"
        self.attrs = {"NetworkSettings": {"Networks": {self.network: {"IPAddress": self.ip}}}}

    def reload(self):
        pass

    def remove(self, force=False):
        # Removing a container releases its endpoint, like the real daemon.
        if self.network in self.daemon.networks:
            self.daemon.networks[self.network].endpoints.pop(self.name, None)
        self.daemon.containers.pop(self.name, None)


class FakeContainerCollection:
    def __init__(self, daemon):
        self.daemon = daemon

    def get(self, name):
        if name not in self.daemon.containers:
            raise NotFound(name)
        return self.daemon.containers[name]

    def create(self, image, name=None, network=None, environment=None, labels=None, **kwargs):
        self.daemon.create_calls += 1
        self.daemon.create_kwargs = kwargs
        time.sleep(self.daemon.create_delay)
        if name in self.daemon.containers:
            raise APIError("name conflict")
        if network is not None and network not in self.daemon.networks:
            raise APIError("no such network: %s" % network)
        container = FakeContainer(self.daemon, name, environment, labels, network)
        self.daemon.containers[name] = container
        return container

    def list(self, all=False, filters=None):
        containers = list(self.daemon.containers.values())
        if not all:
            containers = [c for c in containers if c.status == "running"]
        return containers


class FakeNetwork:
    def __init__(self, daemon, name, subnet, labels, internal=False, options=None):
        self.daemon = daemon
        self.name = name
        self.labels = dict(labels or {})
        self.internal = internal
        self.options = dict(options or {})
        self.subnet = subnet
        self.endpoints = {}
        self.attrs = {"IPAM": {"Config": [{"Subnet": subnet}] if subnet else []}}

    def connect(self, container):
        # Real Docker hands out addresses from the subnet; the proxy gets one too.
        hosts = ipaddress.ip_network(self.subnet).hosts() if self.subnet else iter(())
        taken = set(self.endpoints.values())
        self.endpoints[container] = next(str(h) for h in hosts if str(h) not in taken)

    def disconnect(self, container, force=False):
        if container not in self.endpoints:
            raise APIError("%s is not connected to %s" % (container, self.name))
        del self.endpoints[container]

    def remove(self):
        if self.endpoints:
            raise APIError("network %s has active endpoints" % self.name)
        self.daemon.networks.pop(self.name, None)


class FakeNetworkCollection:
    def __init__(self, daemon):
        self.daemon = daemon

    def get(self, name):
        if name not in self.daemon.networks:
            raise NotFound(name)
        return self.daemon.networks[name]

    def create(self, name, driver=None, ipam=None, labels=None, check_duplicate=None,
               internal=False, options=None):
        self.daemon.network_calls += 1
        if options and self.daemon.reject_options:
            raise APIError("unknown network option")   # a pre-28 daemon
        if check_duplicate and name in self.daemon.networks:
            raise APIError("network %s already exists" % name)
        subnet = None
        if ipam:
            configs = ipam.get("Config") or []
            if configs:
                subnet = configs[0].get("Subnet")
        network = FakeNetwork(self.daemon, name, subnet, labels, internal, options)
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
        self.create_kwargs = {}
        self.network_calls = 0
        self.create_delay = 0
        self.fail_start = False
        self.reject_options = False

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
    monkeypatch.setattr(instancer, "INSTANCE_PORT_MIN", 31000)
    monkeypatch.setattr(instancer, "INSTANCE_PORT_MAX", 31010)
    monkeypatch.setattr(instancer, "SUBNET_POOL", ipaddress.ip_network("10.100.0.0/16"))
    monkeypatch.setattr(instancer, "SUBNET_PREFIX", 24)
    monkeypatch.setattr(instancer, "DEFAULT_TTL", 3600)
    monkeypatch.setattr(instancer, "MAX_TTL", 86400)
    monkeypatch.setattr(instancer, "MODE", "http")
    monkeypatch.setattr(instancer, "PROXY_TOKEN", TOKEN)
    monkeypatch.setattr(instancer, "PROXY_PORT", 1337)
    monkeypatch.setattr(instancer, "PROXY_HOST", "")
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


def port_of(container):
    """The instance's port -- a label now, no longer anything the player sees."""
    return int(container.labels["spawnzero.port"])


def idle(mode="http"):
    return {"running": False, "mode": mode, "proxy_host": None, "proxy_port": 1337}


def lookup(web, key, token=TOKEN):
    headers = {"X-Proxy-Token": token} if token is not None else {}
    return web.get("/internal/route/%s" % key, headers=headers)


# --- UI + status --------------------------------------------------------------

def test_index_serves_ui(web):
    body = web.get("/").get_data(as_text=True)
    assert "SPAWNZERO" in body
    assert "START" in body
    assert "STOP" in body


def test_status_without_instance(web):
    assert web.get("/status").get_json() == idle()


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
    assert data["mode"] == "http"
    assert data["proxy_port"] == 1337
    assert data["expires_at"] is not None
    assert 0 < data["remaining_time"] <= 3600
    assert daemon.create_calls == 1
    assert daemon.network_calls == 1

    container = only_container(daemon)
    assert container.status == "running"
    assert instancer.INSTANCE_PORT_MIN <= port_of(container) <= instancer.INSTANCE_PORT_MAX
    assert container.network == instancer.network_name(container.labels["spawnzero.owner"])
    only_network(daemon)


def test_the_instance_is_told_which_port_to_listen_on(web, daemon):
    web.post("/create")
    container = only_container(daemon)
    assert container.environment == {"CHAL_PORT": str(port_of(container))}


def test_nothing_is_published_to_the_host(web, daemon):
    web.post("/create")
    assert "ports" not in daemon.create_kwargs


def test_status_reports_running_instance(web, daemon):
    created = web.post("/create").get_json()
    status = web.get("/status").get_json()
    assert status["running"] is True
    assert status["key"] == created["key"]
    assert status["expires_at"] == created["expires_at"]
    assert status["remaining_time"] <= created["remaining_time"]


def test_create_is_idempotent_within_a_session(web, daemon):
    first = web.post("/create").get_json()
    second = web.post("/create").get_json()
    assert first["key"] == second["key"]
    assert daemon.create_calls == 1
    assert daemon.network_calls == 1
    assert len(daemon.containers) == 1
    assert len(daemon.networks) == 1


def test_destroy_removes_container_and_network(web, daemon):
    web.post("/create")
    assert web.post("/destroy").get_json() == idle()
    assert daemon.containers == {}
    assert daemon.networks == {}
    assert web.get("/status").get_json() == idle()


def test_destroy_without_container(web):
    assert web.post("/destroy").status_code == 200
    assert web.post("/destroy").get_json() == idle()


def test_failed_create_cleans_up_container_and_network(web, daemon):
    daemon.fail_start = True
    response = web.post("/create")
    assert response.status_code == 500
    assert response.get_json()["running"] is False
    assert daemon.containers == {}
    assert daemon.networks == {}          # network rolled back too

    daemon.fail_start = False
    assert web.post("/create").get_json()["running"] is True
    # the port of the rolled-back attempt was never marked used
    assert port_of(only_container(daemon)) == instancer.INSTANCE_PORT_MIN


def test_concurrent_create_in_one_session(daemon):
    daemon.create_delay = 0.2
    web = instancer.app.test_client()
    web.get("/")
    results = []
    run_together([lambda: results.append(web.post("/create").get_json())] * 2)
    assert daemon.create_calls == 1
    assert len(daemon.containers) == 1
    assert len(daemon.networks) == 1
    assert results[0]["key"] == results[1]["key"]


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
    assert status["key"] == created["key"]
    assert status["expires_at"] == created["expires_at"]
    assert restarted.post("/create").get_json()["key"] == created["key"]
    assert daemon.create_calls == 1   # not recreated


def test_stale_container_and_network_are_cleaned_up(web, daemon):
    web.post("/create")
    only_container(daemon).status = "exited"
    assert web.get("/status").get_json() == idle()
    assert daemon.containers == {}
    assert daemon.networks == {}


def test_labels_carry_metadata(web, daemon):
    created = web.post("/create").get_json()
    container = only_container(daemon)
    owner = container.labels["spawnzero.owner"]
    assert container.labels["spawnzero.expires_at"].isdigit()
    assert instancer.INSTANCE_PORT_MIN <= port_of(container) <= instancer.INSTANCE_PORT_MAX
    assert container.labels["spawnzero.key"] == created["key"]
    assert ipaddress.ip_network(container.labels["spawnzero.subnet"]).prefixlen == 24
    network = only_network(daemon)
    assert network.labels["spawnzero.owner"] == owner
    assert network.labels["spawnzero.subnet"] == container.labels["spawnzero.subnet"]


def test_a_player_is_told_only_what_they_can_use(web, daemon):
    # How to connect, and how long they have. Nothing about our machinery.
    assert set(web.post("/create").get_json()) == {
        "running", "mode", "key", "proxy_host", "proxy_port",
        "expires_at", "remaining_time"}


def test_a_failure_does_not_hand_the_player_our_logs(web, daemon):
    daemon.fail_start = True
    answer = web.post("/create").get_json()
    assert answer["error"] == instancer.ERROR_CREATE
    assert "start failed" not in repr(answer)     # Docker's words stay in the log


# --- network isolation --------------------------------------------------------

def test_instance_network_is_internal_and_gatewayless(web, daemon):
    web.post("/create")
    network = only_network(daemon)
    assert network.internal is True
    assert network.options[instancer.GATEWAY_MODE_OPTION] == "isolated"


def test_old_daemon_falls_back_to_a_plain_internal_network(web, daemon, caplog):
    daemon.reject_options = True
    assert web.post("/create").get_json()["running"] is True
    network = only_network(daemon)
    assert network.internal is True
    assert network.options == {}
    assert "upgrade to Docker 28+" in caplog.text


def test_proxy_is_attached_to_the_instance_network(web, daemon):
    web.post("/create")
    assert instancer.PROXY_CONTAINER in only_network(daemon).endpoints


def test_destroy_detaches_the_proxy_before_removing_the_network(web, daemon):
    web.post("/create")
    web.post("/destroy")
    assert daemon.networks == {}       # a still-attached proxy would have blocked this


def test_create_rolls_back_when_the_proxy_is_missing(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "attach_proxy",
                        lambda owner: (_ for _ in ()).throw(APIError("no such container")))
    assert web.post("/create").status_code == 500
    assert daemon.containers == {}
    assert daemon.networks == {}


# --- keys ---------------------------------------------------------------------

def test_key_is_unguessable_and_unique_per_session(web, other, daemon):
    mine = web.post("/create").get_json()["key"]
    theirs = other.post("/create").get_json()["key"]
    assert re.fullmatch(r"[0-9a-f]{32}", mine)
    assert mine != theirs


def test_route_resolves_a_key_to_its_instance(web, daemon):
    # The proxy is the one caller that does get the address and the port.
    created = web.post("/create").get_json()
    container = only_container(daemon)
    assert lookup(web, created["key"]).get_json() == {
        "host": container.ip, "port": port_of(container)}


def test_route_needs_the_proxy_token(web, daemon):
    key = web.post("/create").get_json()["key"]
    assert lookup(web, key, token=None).status_code == 404
    assert lookup(web, key, token="wrong").status_code == 404


def test_route_refuses_everything_without_a_configured_token(web, daemon, monkeypatch):
    key = web.post("/create").get_json()["key"]
    monkeypatch.setattr(instancer, "PROXY_TOKEN", "")
    assert lookup(web, key, token="").status_code == 404


def test_route_of_an_unknown_key_is_a_404(web, daemon):
    web.post("/create")
    assert lookup(web, "deadbeef" * 4).status_code == 404


def test_key_stops_working_once_the_instance_is_gone(web, daemon):
    key = web.post("/create").get_json()["key"]
    web.post("/destroy")
    assert lookup(web, key).status_code == 404


def test_key_of_a_stopped_instance_does_not_route(web, daemon):
    key = web.post("/create").get_json()["key"]
    only_container(daemon).status = "exited"
    assert lookup(web, key).status_code == 404


# --- ports --------------------------------------------------------------------

def test_ports_outside_range_are_never_used(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "INSTANCE_PORT_MIN", 31020)
    monkeypatch.setattr(instancer, "INSTANCE_PORT_MAX", 31021)
    web.post("/create")
    assert port_of(only_container(daemon)) in (31020, 31021)


def test_two_sessions_get_distinct_ports(web, other, daemon):
    web.post("/create")
    other.post("/create")
    assert len({port_of(c) for c in daemon.containers.values()}) == 2


def test_a_destroyed_instance_gives_its_port_back(web, daemon):
    web.post("/create")
    port = port_of(only_container(daemon))
    web.post("/destroy")
    web.post("/create")
    assert port_of(only_container(daemon)) == port


def test_create_fails_when_port_range_exhausted(web, other, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "INSTANCE_PORT_MIN", 31030)
    monkeypatch.setattr(instancer, "INSTANCE_PORT_MAX", 31030)
    web.post("/create")
    response = other.post("/create")
    assert response.status_code == 503
    assert response.get_json()["error"] == instancer.ERROR_BUSY
    assert len(daemon.containers) == 1
    assert len(daemon.networks) == 1


# --- subnets ------------------------------------------------------------------

def test_two_sessions_get_distinct_subnets(web, other, daemon):
    web.post("/create")
    other.post("/create")
    subnets = {c.labels["spawnzero.subnet"] for c in daemon.containers.values()}
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
    web.post("/create")
    subnet = ipaddress.ip_network(only_container(daemon).labels["spawnzero.subnet"])
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
    container.labels["spawnzero.expires_at"] = str(int(time.time()) - 1)   # already expired
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
    victim.labels["spawnzero.expires_at"] = str(int(time.time()) - 1)
    instancer.reap_expired()
    assert len(daemon.containers) == 1
    assert victim.name not in daemon.containers
    assert len(daemon.networks) == 1


def test_reaper_prunes_orphan_network(daemon):
    daemon.add_network(instancer.network_name("ghost"), "10.100.9.0/24")
    instancer.reap_expired()
    assert daemon.networks == {}


def test_reaper_prunes_an_orphan_network_the_proxy_is_still_on(daemon):
    daemon.add_network(instancer.network_name("ghost"), "10.100.9.0/24")
    daemon.networks[instancer.network_name("ghost")].connect(instancer.PROXY_CONTAINER)
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
    assert web.post("/destroy").get_json() == idle("netcat")


def test_index_exposes_mode_and_proxy_to_the_page(daemon, monkeypatch):
    monkeypatch.setattr(instancer, "MODE", "netcat")
    body = instancer.app.test_client().get("/").get_data(as_text=True)
    assert '"netcat"' in body
    assert "const PROXY_PORT = 1337" in body


def test_unknown_mode_falls_back_to_http(monkeypatch):
    import importlib
    monkeypatch.setenv("MODE", "carrier-pigeon")
    reloaded = importlib.reload(instancer)
    assert reloaded.MODE == "http"


# --- multi-session isolation --------------------------------------------------

def test_a_session_only_sees_its_own_instance(web, other, daemon):
    web.post("/create")
    assert other.get("/status").get_json() == idle()


def test_a_session_cannot_destroy_another(web, other, daemon):
    web.post("/create")
    assert other.post("/destroy").get_json() == idle()
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

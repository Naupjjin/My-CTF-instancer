"""Unit tests: the instancer logic against a fake Docker daemon."""

import io
import ipaddress
import json
import logging
import re
import signal
import threading
import time
import urllib.error
import urllib.request

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

import app as instancer

TOKEN = "test-proxy-token"
CONTROL = "ctf-control-test"
CONTROL_SUBNET = "10.239.0.0/24"

PWN = "special-love"
WEB = "cookie-jar"


# --- fake Docker daemon -------------------------------------------------------

class FakeContainer:
    def __init__(self, daemon, name, image, environment, labels, network, address, kwargs):
        self.daemon = daemon
        self.name = name
        self.image = image
        self.environment = dict(environment or {})
        self.labels = dict(labels or {})
        self.network = network
        self.kwargs = dict(kwargs or {})
        self.status = "created"
        self.attrs = {"NetworkSettings": {"Networks": {}}}

    @property
    def ip(self):
        return self.daemon.networks[self.network].endpoints[self.name]

    def start(self):
        # A real container exists, and is not running, for as long as this takes.
        self.daemon.starting.set()
        time.sleep(self.daemon.start_delay)
        if self.daemon.fail_start and self.name.startswith(instancer.CONTAINER_PREFIX):
            raise APIError("start failed")
        self.status = "running"
        self.attrs = {"NetworkSettings": {"Networks": {self.network: {"IPAddress": self.ip}}}}

    def reload(self):
        pass

    def remove(self, force=False):
        # Removing a container releases its endpoint, like the real daemon.
        for network in self.daemon.networks.values():
            network.endpoints.pop(self.name, None)
        self.daemon.containers.pop(self.name, None)


class FakeContainerCollection:
    def __init__(self, daemon):
        self.daemon = daemon

    def get(self, name):
        if name not in self.daemon.containers:
            raise NotFound(name)
        return self.daemon.containers[name]

    def create(self, image, name=None, network=None, environment=None, labels=None,
               networking_config=None, **kwargs):
        if name.startswith(instancer.CONTAINER_PREFIX):
            self.daemon.create_calls += 1
            time.sleep(self.daemon.create_delay)
        if name in self.daemon.containers:
            raise APIError("name conflict")
        if network is not None and network not in self.daemon.networks:
            raise APIError("no such network: %s" % network)
        address = None
        if networking_config:
            endpoint = networking_config.get(network) or {}
            address = (endpoint.get("IPAMConfig") or {}).get("IPv4Address")
        container = FakeContainer(self.daemon, name, image, environment, labels,
                                  network, address, kwargs)
        self.daemon.containers[name] = container
        if network is not None:
            self.daemon.networks[network].connect(name, address)
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

    @property
    def network(self):
        return ipaddress.ip_network(self.subnet)

    @property
    def gateway(self):
        return str(next(self.network.hosts())) if self.subnet else None

    @property
    def attrs(self):
        config = {}
        if self.subnet:
            config = {"Subnet": self.subnet, "Gateway": self.gateway}
        return {
            "IPAM": {"Config": [config] if config else []},
            "Internal": self.internal,
            "Containers": {
                name: {"IPv4Address": "%s/%d" % (ip, self.network.prefixlen)}
                for name, ip in self.endpoints.items()},
        }

    def reload(self):
        pass

    def connect(self, container, ipv4_address=None):
        # Real Docker hands out addresses from the subnet, keeping .1 for the
        # gateway; the proxy asks for a specific one and gets it. And there has
        # to be something to attach: a missing proxy is a NotFound, not a no-op.
        if container not in self.daemon.containers:
            raise NotFound(container)
        if ipv4_address is not None:
            self.endpoints[container] = ipv4_address
            return
        hosts = self.network.hosts() if self.subnet else iter(())
        taken = set(self.endpoints.values()) | {self.gateway}
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


class FakeAPI:
    """The low-level client, for the one thing the high-level one cannot say:
    which address a container should be given on a network."""

    @staticmethod
    def create_endpoint_config(ipv4_address=None):
        return {"IPAMConfig": {"IPv4Address": ipv4_address}}


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
        self.start_delay = 0
        self.starting = threading.Event()   # raised while a container is starting
        self.fail_start = False
        self.reject_options = False

    def add_network(self, name, subnet, labels=None):
        self.networks[name] = FakeNetwork(self, name, subnet, labels or {})


class FakeClient:
    def __init__(self, daemon):
        self.containers = daemon.containers_collection
        self.networks = daemon.networks_collection
        self.images = daemon.images_collection
        self.api = FakeAPI()


# --- fake CTFd ----------------------------------------------------------------

# What CTFd hands out: `ctfd_` and 64 hex. Two of them, so a test can watch two
# accounts stay apart.
GOOD = "ctfd_" + "a" * 64
OTHER = "ctfd_" + "b" * 64


class FakeCTFd:
    """A CTFd that answers one question -- whose token is this -- and nothing else."""

    def __init__(self):
        self.accounts = {}
        self.asked = []           # every request that actually left the instancer
        self.down = False         # CTFd unreachable, rather than CTFd saying no
        self.answer = None        # a 200 that is not CTFd's shape

    def add(self, token, account_id, name):
        self.accounts[token] = {"id": account_id, "name": name}
        return token

    def urlopen(self, request, timeout=None):
        self.asked.append(request)
        if self.down:
            raise urllib.error.URLError("connection refused")
        if self.answer is not None:
            return io.BytesIO(json.dumps(self.answer).encode())
        presented = request.headers.get("Authorization", "")
        token = presented[len("Token "):] if presented.startswith("Token ") else ""
        account = self.accounts.get(token)
        if account is None:
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
        return io.BytesIO(json.dumps({"success": True, "data": account}).encode())


# --- challenges ---------------------------------------------------------------

# A pool each, the way two real challenges would have one.
POOLS = {PWN: "10.100.0.0/16", WEB: "10.101.0.0/16"}
PORTS = "31000-31010"


def make_challenge(cid, mode="http", port=1337, ttl=3600, pool=None, ports=PORTS,
                   cap=0, **extra):
    config = dict(instancer.DEFAULT_CHALLENGE, mode=mode, proxy_port=port, ttl=ttl,
                  name=cid.replace("-", " ").title(), author="naup",
                  type="pwn" if mode == "netcat" else "web",
                  subnet_pool=ipaddress.ip_network(pool or POOLS.get(cid, "10.102.0.0/16")),
                  subnet_prefix=24, instance_ports=instancer.parse_ports(ports),
                  max_instances=cap, **extra)
    return instancer.Challenge(cid, "/challenges/" + cid, config)


@pytest.fixture
def daemon(monkeypatch):
    fake = FakeDaemon()
    # Module state that outlives a request, and so would outlive a test.
    instancer.shutting_down.clear()
    instancer.reserved_ports.clear()
    instancer.reserved_subnets.clear()
    fake.add_network(CONTROL, CONTROL_SUBNET)
    monkeypatch.setattr(instancer, "_client", FakeClient(fake))
    monkeypatch.setattr(instancer, "CONTROL_NETWORK", CONTROL)
    monkeypatch.setattr(instancer, "PROXY_TOKEN", TOKEN)
    monkeypatch.setattr(instancer, "PROXY_HOST", "")
    monkeypatch.setattr(instancer, "CHALLENGES", {
        PWN: make_challenge(PWN, mode="netcat", port=1337),
        WEB: make_challenge(WEB, mode="http", port=1338),
    })
    monkeypatch.setattr(instancer.app, "secret_key", "test-secret")
    # What startup() leaves behind, and what every create assumes: each
    # challenge already has the proxy that is its only door.
    for challenge in instancer.CHALLENGES.values():
        instancer.create_proxy(challenge)
    return fake


def add_challenge(monkeypatch, chal):
    """A challenge that appears mid-test, proxy and all."""
    monkeypatch.setitem(instancer.CHALLENGES, chal.id, chal)
    instancer.create_proxy(chal)
    return chal


@pytest.fixture
def pwn(daemon):
    return instancer.CHALLENGES[PWN]


@pytest.fixture
def chal(daemon):
    """The challenge most tests use. Anything mode-specific says which it wants."""
    return instancer.CHALLENGES[WEB]


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


@pytest.fixture
def ctfd(monkeypatch):
    """The event checks tokens, and there is a CTFd to check them with."""
    fake = FakeCTFd()
    fake.add(GOOD, 7, "player-one")
    fake.add(OTHER, 9, "player-two")
    monkeypatch.setattr(instancer, "CTFD_VERIFY", True)
    monkeypatch.setattr(instancer, "CTFD_URL", "http://ctfd.test")
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


# --- talking to the api -------------------------------------------------------

def create(client, chal=WEB, **kwargs):
    return client.post("/api/%s/create" % chal, **kwargs)


def status(client, chal=WEB):
    return client.get("/api/%s/status" % chal)


def destroy(client, chal=WEB):
    return client.post("/api/%s/destroy" % chal)


def verify(client, token=GOOD):
    return client.post("/api/verify", json={"token": token})


def player():
    """A browser that has loaded the page and said nothing yet."""
    client = instancer.app.test_client()
    client.get("/")
    return client


def lookup(client, key, chal=WEB, token=...):
    if token is ...:
        token = instancer.proxy_token(chal)
    headers = {"X-Proxy-Token": token} if token is not None else {}
    return client.get("/internal/route/%s/%s" % (chal, key), headers=headers)


def instances(daemon, chal=None):
    prefix = instancer.CONTAINER_PREFIX + (chal + "-" if chal else "")
    return [c for c in daemon.containers.values() if c.name.startswith(prefix)]


def proxies(daemon):
    return [c for c in daemon.containers.values()
            if c.name.startswith(instancer.PROXY_PREFIX)]


def only_container(daemon, chal=None):
    found = instances(daemon, chal)
    assert len(found) == 1
    return found[0]


def instance_networks(daemon):
    return [n for n in daemon.networks.values()
            if n.name.startswith(instancer.NETWORK_PREFIX)]


def only_network(daemon):
    found = instance_networks(daemon)
    assert len(found) == 1
    return found[0]


def port_of(container):
    """The instance's port -- a label now, no longer anything the player sees."""
    return int(container.labels["ctf.port"])


def idle(chal):
    return {"chal": chal.id, "name": chal.name, "running": False, "mode": chal.mode,
            "proxy_host": None, "proxy_port": chal.proxy_port}


def run_together(workers):
    threads = [threading.Thread(target=worker) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# --- UI + status --------------------------------------------------------------

def test_index_lists_every_challenge(web, daemon):
    body = web.get("/").get_data(as_text=True)
    assert "Special Love" in body
    assert "Cookie Jar" in body
    assert '/c/%s' % PWN in body


def test_index_of_an_empty_instancer_says_so(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "CHALLENGES", {})
    assert "nothing is being served" in web.get("/").get_data(as_text=True)


def test_challenge_page_serves_the_panel(web, chal):
    body = web.get("/c/%s" % chal.id).get_data(as_text=True)
    assert "START" in body
    assert "STOP" in body
    assert "COOKIE JAR" in body
    assert '"%s"' % chal.id in body


def test_challenge_page_of_an_unknown_challenge_is_a_404(web, daemon):
    response = web.get("/c/nope")
    assert response.status_code == 404
    assert "NO SUCH CHALLENGE" in response.get_data(as_text=True)


def test_api_of_an_unknown_challenge_is_a_404(web, daemon):
    for response in (create(web, "nope"), status(web, "nope"), destroy(web, "nope")):
        assert response.status_code == 404
        assert response.get_json()["error"] == instancer.ERROR_NO_CHAL


def test_status_without_instance(web, chal):
    assert status(web, chal.id).get_json() == idle(chal)


def test_session_comes_from_the_page_not_from_polling(daemon):
    client = instancer.app.test_client()
    status(client)
    assert client.get_cookie("session") is None
    client.get("/")
    assert client.get_cookie("session") is not None


def test_the_challenge_list_reports_what_you_have_running(web, daemon, pwn, chal):
    create(web, chal.id)
    listed = {c["chal"]: c for c in web.get("/api/challenges").get_json()["challenges"]}
    assert listed[chal.id]["running"] is True
    assert 0 < listed[chal.id]["remaining_time"] <= chal.ttl
    assert listed[pwn.id]["running"] is False
    assert listed[pwn.id]["type"] == "pwn"


# --- the instancer's own name -------------------------------------------------

def test_the_page_shows_the_configured_name(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "INSTANCER_NAME", "Zero")
    assert "ZERO" in web.get("/").get_data(as_text=True)


def test_the_name_reaches_every_page(web, daemon, chal, monkeypatch):
    monkeypatch.setattr(instancer, "INSTANCER_NAME", "Zero")
    for path in ("/", "/c/%s" % chal.id, "/c/nope"):
        assert "ZERO" in web.get(path).get_data(as_text=True), path


# --- who a player is (CTFd) ---------------------------------------------------

def test_without_ctfd_a_browser_is_a_player(web, chal, daemon):
    assert create(web, chal.id).get_json()["running"] is True


def test_with_ctfd_a_browser_is_nobody_until_it_says_who(ctfd, web, chal, daemon):
    response = create(web, chal.id)
    assert response.status_code == 403
    assert response.get_json()["error"] == instancer.ERROR_NO_TOKEN
    assert instances(daemon) == []


def test_a_verified_token_opens_the_door(ctfd, web, chal, daemon):
    assert verify(web).get_json() == {"ctfd": True, "verified": True,
                                      "user": "player-one"}
    assert create(web, chal.id).get_json()["running"] is True
    asked, = ctfd.asked
    assert asked.full_url == "http://ctfd.test/api/v1/users/me"
    assert asked.headers["Authorization"] == "Token " + GOOD


def test_an_instance_is_named_after_the_account_not_the_session(ctfd, web, chal, daemon):
    verify(web)
    create(web, chal.id)
    assert only_container(daemon).name.endswith("-" + instancer.ctfd_owner(7))


def test_an_account_id_names_an_owner_the_same_way_every_time():
    # Nothing secret goes into it and nothing per-process: an owner that moved
    # when the instancer restarted would be a second instance for every player.
    assert instancer.ctfd_owner(7) == instancer.ctfd_owner(7)
    assert instancer.ctfd_owner(7) != instancer.ctfd_owner(9)
    assert re.fullmatch(r"[0-9a-f]{16}", instancer.ctfd_owner(7))


def test_a_token_that_is_not_one_is_never_asked_about(ctfd, web, daemon):
    for bad in ("", "hunter2", "ctfd_" + "z" * 64, "ctfd_" + "a" * 63, GOOD + "a"):
        response = web.post("/api/verify", json={"token": bad})
        assert response.status_code == 403, bad
        assert response.get_json()["error"] == instancer.ERROR_BAD_TOKEN
    assert ctfd.asked == []


def test_verify_without_a_token_is_not_a_crash(ctfd, web, daemon):
    for body in ({}, {"token": None}, {"token": 7}, {"token": ["a"]}):
        assert web.post("/api/verify", json=body).status_code == 403, body
    assert web.post("/api/verify", data="not json",
                    content_type="text/plain").status_code == 403
    assert ctfd.asked == []


def test_a_token_ctfd_does_not_know_gets_nothing(ctfd, web, chal, daemon):
    response = verify(web, "ctfd_" + "c" * 64)
    assert response.status_code == 403
    assert response.get_json() == {"ctfd": True, "verified": False,
                                   "error": instancer.ERROR_BAD_TOKEN}
    assert create(web, chal.id).status_code == 403


def test_a_ctfd_that_cannot_be_reached_is_not_a_no(ctfd, web, chal, daemon):
    ctfd.down = True
    response = verify(web)
    assert response.status_code == 503
    assert response.get_json()["error"] == instancer.ERROR_CTFD
    assert create(web, chal.id).status_code == 403
    # The token was fine all along, and says so the moment CTFd is back.
    ctfd.down = False
    assert verify(web).get_json()["verified"] is True


def test_a_ctfd_that_answers_nonsense_is_our_failure_not_the_token_s(ctfd, web, daemon):
    ctfd.answer = {"nothing": "like CTFd"}
    response = verify(web)
    assert response.status_code == 503
    assert response.get_json()["error"] == instancer.ERROR_CTFD


def test_with_nowhere_to_check_a_token_nothing_starts(ctfd, web, chal, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "CTFD_URL", "")
    assert verify(web).status_code == 503
    assert create(web, chal.id).status_code == 403
    assert ctfd.asked == []


def test_one_account_holds_one_instance_of_a_challenge(ctfd, web, other, chal, daemon):
    verify(web)
    verify(other)                       # the same token, in a second browser
    first = create(web, chal.id).get_json()
    second = create(other, chal.id).get_json()
    assert second["key"] == first["key"]         # the same instance, handed back
    assert len(instances(daemon)) == 1


def test_a_fresh_cookie_does_not_buy_a_second_instance(ctfd, web, chal, daemon):
    verify(web)
    create(web, chal.id)
    fresh = player()                    # cleared cookies, same account
    verify(fresh)
    assert status(fresh, chal.id).get_json()["running"] is True
    assert len(instances(daemon)) == 1


def test_one_account_still_gets_one_of_each_challenge(ctfd, web, pwn, chal, daemon):
    verify(web)
    assert create(web, pwn.id).get_json()["running"] is True
    assert create(web, chal.id).get_json()["running"] is True
    assert len(instances(daemon)) == 2


def test_two_accounts_are_two_players(ctfd, web, other, chal, daemon):
    verify(web)
    verify(other, OTHER)
    create(web, chal.id)
    create(other, chal.id)
    assert len(instances(daemon)) == 2
    assert status(web, chal.id).get_json()["key"] != status(other, chal.id).get_json()["key"]


def test_a_second_token_is_a_second_player_not_a_second_instance(ctfd, web, chal, daemon):
    verify(web)
    create(web, chal.id)
    verify(web, OTHER)                  # somebody else sits down at this browser
    assert status(web, chal.id).get_json() == idle(chal)   # and has nothing
    assert len(instances(daemon)) == 1                     # and took nothing away


def test_a_cookie_from_before_the_gate_is_not_an_identity(web, chal, daemon, monkeypatch):
    # This browser was here while CTFD_VERIFY was off, and kept the session id
    # it was given. Turning the check on does not turn that into an account.
    monkeypatch.setattr(instancer, "CTFD_VERIFY", True)
    assert create(web, chal.id).status_code == 403
    assert status(web, chal.id).get_json() == idle(chal)
    assert web.get("/api/challenges").get_json()["challenges"][0]["running"] is False


def test_the_token_itself_is_not_kept(ctfd, web, daemon):
    verify(web)
    with web.session_transaction() as stored:
        assert GOOD not in stored.values()
        assert stored["ctfd"] == "player-one"


def test_the_page_asks_for_a_token_and_then_says_whose_it_is(ctfd, web, chal, daemon):
    body = web.get("/c/%s" % chal.id).get_data(as_text=True)
    assert "IDENTITY" in body and "VERIFY" in body
    assert "CTFd tokens" in web.get("/").get_data(as_text=True)
    verify(web)
    assert "verified as player-one" in web.get("/c/%s" % chal.id).get_data(as_text=True)
    assert "verified as" in web.get("/").get_data(as_text=True)


def test_without_ctfd_no_page_asks_for_anything(web, chal, daemon):
    for path in ("/", "/c/%s" % chal.id):
        assert "IDENTITY" not in web.get(path).get_data(as_text=True), path


def test_verify_is_a_no_op_when_the_event_does_not_check_tokens(web, daemon):
    assert verify(web).get_json() == {"ctfd": False, "verified": True, "user": None}


# --- challenges (each challenge's own config.yml) -----------------------------

CHAL_YAML = ("name: Special Love\nauthor: naup\ntype: pwn\n"
             "mode: netcat\nproxy_port: 1337\nttl: 600\n"
             "mem_limit: 256m\npids_limit: 64\n")


def write_challenge(root, cid, config=CHAL_YAML, dockerfile="FROM scratch\n"):
    directory = root / cid
    directory.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (directory / "config.yml").write_text(config)
    if dockerfile is not None:
        (directory / "Dockerfile").write_text(dockerfile)
    return directory


def test_a_challenge_is_a_directory_and_its_config(tmp_path):
    write_challenge(tmp_path, PWN)
    found = instancer.load_challenges(str(tmp_path))
    assert list(found) == [PWN]
    chal = found[PWN]
    assert (chal.name, chal.author, chal.type) == ("Special Love", "naup", "pwn")
    assert (chal.mode, chal.proxy_port, chal.ttl) == ("netcat", 1337, 600)
    assert (chal.mem_limit, chal.pids_limit) == ("256m", 64)
    assert chal.image == instancer.CHALLENGE_IMAGE.format(chal=PWN)
    assert chal.proxy == instancer.PROXY_PREFIX + PWN


def test_a_challenge_config_fills_in_what_it_leaves_out(tmp_path):
    write_challenge(tmp_path, "bare", "proxy_port: 9000\n")
    chal = instancer.load_challenges(str(tmp_path))["bare"]
    assert chal.name == "bare"                       # the id, for want of a name
    assert chal.mode == instancer.DEFAULT_MODE
    assert chal.ttl == instancer.DEFAULT_TTL
    assert chal.mem_limit == instancer.DEFAULT_MEM_LIMIT
    assert chal.subnet_pool == ipaddress.ip_network(instancer.DEFAULT_SUBNET_POOL)
    assert chal.subnet_prefix == instancer.DEFAULT_SUBNET_PREFIX
    assert (chal.port_min, chal.port_max) == instancer.parse_ports(
        instancer.DEFAULT_INSTANCE_PORTS)


def test_a_challenge_says_where_its_instances_live(tmp_path):
    write_challenge(tmp_path, "own", "proxy_port: 9000\nsubnet_pool: 10.9.0.0/16\n"
                                     "subnet_prefix: 26\ninstance_ports: 40000-40009\n")
    chal = instancer.load_challenges(str(tmp_path))["own"]
    assert chal.subnet_pool == ipaddress.ip_network("10.9.0.0/16")
    assert chal.subnet_prefix == 26
    assert (chal.port_min, chal.port_max) == (40000, 40009)
    assert chal.capacity == 10          # one instance per port


def test_a_single_port_is_a_range_of_one(tmp_path):
    write_challenge(tmp_path, "solo", "proxy_port: 9000\ninstance_ports: 40000\n")
    chal = instancer.load_challenges(str(tmp_path))["solo"]
    assert (chal.port_min, chal.port_max) == (40000, 40000)
    assert chal.capacity == 1


def test_a_port_range_that_is_not_one_falls_back(tmp_path, caplog):
    write_challenge(tmp_path, "wrong", "proxy_port: 9000\ninstance_ports: 40100-40000\n")
    chal = instancer.load_challenges(str(tmp_path))["wrong"]
    assert (chal.port_min, chal.port_max) == instancer.parse_ports(
        instancer.DEFAULT_INSTANCE_PORTS)
    assert "is not a port range" in caplog.text


def test_a_subnet_pool_that_is_not_a_network_falls_back(tmp_path, caplog):
    write_challenge(tmp_path, "vague", "proxy_port: 9000\nsubnet_pool: somewhere\n")
    chal = instancer.load_challenges(str(tmp_path))["vague"]
    assert chal.subnet_pool == ipaddress.ip_network(instancer.DEFAULT_SUBNET_POOL)
    assert "is not a network" in caplog.text


def test_a_subnet_prefix_that_does_not_divide_the_pool_falls_back(tmp_path, caplog):
    write_challenge(tmp_path, "coarse", "proxy_port: 9000\nsubnet_pool: 10.9.0.0/24\n"
                                        "subnet_prefix: 16\n")
    chal = instancer.load_challenges(str(tmp_path))["coarse"]
    assert chal.subnet_prefix == instancer.DEFAULT_SUBNET_PREFIX
    assert "does not divide" in caplog.text


def test_a_challenge_may_cap_how_many_of_it_run_at_once(tmp_path):
    write_challenge(tmp_path, "few", "proxy_port: 9000\ninstance_ports: 40000-40099\n"
                                     "max_instances: 8\n")
    chal = instancer.load_challenges(str(tmp_path))["few"]
    assert chal.max_instances == 8
    assert chal.capacity == 8            # the cap is what binds, not the 100 ports


def test_without_a_cap_the_range_and_the_pool_decide(tmp_path):
    write_challenge(tmp_path, "open", "proxy_port: 9000\ninstance_ports: 40000-40099\n"
                                      "subnet_pool: 10.9.0.0/22\nsubnet_prefix: 24\n")
    chal = instancer.load_challenges(str(tmp_path))["open"]
    assert chal.max_instances == 0
    assert chal.capacity == 4            # a /22 in /24s, not the 100 ports


def test_a_cap_above_what_the_pools_allow_is_said_out_loud(tmp_path, caplog):
    write_challenge(tmp_path, "hopeful", "proxy_port: 9000\ninstance_ports: 40000-40009\n"
                                         "max_instances: 500\n")
    chal = instancer.load_challenges(str(tmp_path))["hopeful"]
    assert chal.capacity == 10           # the ten ports still win
    assert "so 10 is the real limit" in caplog.text


def test_a_negative_cap_is_no_cap(tmp_path, caplog):
    write_challenge(tmp_path, "odd", "proxy_port: 9000\nmax_instances: -3\n")
    assert instancer.load_challenges(str(tmp_path))["odd"].max_instances == 0
    assert "not a number of instances" in caplog.text


def test_challenges_are_served_in_a_stable_order(tmp_path):
    for cid in ("zeta", "alpha", "middle"):
        write_challenge(tmp_path, cid, "proxy_port: %d\n" % (9000 + len(cid)))
    assert list(instancer.load_challenges(str(tmp_path))) == ["alpha", "middle", "zeta"]


def test_a_challenge_without_a_dockerfile_is_skipped(tmp_path, caplog):
    write_challenge(tmp_path, "nobuild", dockerfile=None)
    assert instancer.load_challenges(str(tmp_path)) == {}
    assert "no Dockerfile" in caplog.text


def test_a_challenge_without_a_config_is_skipped(tmp_path, caplog):
    write_challenge(tmp_path, "silent", config=None)
    assert instancer.load_challenges(str(tmp_path)) == {}
    assert "no usable config.yml" in caplog.text


def test_a_challenge_whose_config_is_not_valid_yaml_is_skipped(tmp_path, caplog):
    write_challenge(tmp_path, "broken", "name: [unclosed\n")
    assert instancer.load_challenges(str(tmp_path)) == {}
    assert "not valid YAML" in caplog.text


def test_a_challenge_whose_config_is_not_a_mapping_is_skipped(tmp_path, caplog):
    write_challenge(tmp_path, "listy", "- pwn\n")
    assert instancer.load_challenges(str(tmp_path)) == {}
    assert "not a mapping" in caplog.text


def test_a_challenge_name_is_always_a_string(tmp_path):
    # `name: 1337` is a number to YAML and a name to everyone else.
    write_challenge(tmp_path, "numeric", "proxy_port: 9000\nname: 1337\n")
    assert instancer.load_challenges(str(tmp_path))["numeric"].name == "1337"


def test_a_challenge_without_a_proxy_port_is_skipped(tmp_path, caplog):
    write_challenge(tmp_path, "unreachable", "name: Nowhere\n")
    assert instancer.load_challenges(str(tmp_path)) == {}
    assert "not a port players could reach" in caplog.text


def test_two_challenges_cannot_share_a_proxy_port(tmp_path, caplog):
    write_challenge(tmp_path, "first", "proxy_port: 1337\n")
    write_challenge(tmp_path, "second", "proxy_port: 1337\n")
    assert list(instancer.load_challenges(str(tmp_path))) == ["first"]
    assert "already first's" in caplog.text


def test_a_challenge_id_has_to_be_a_name_we_can_put_in_a_url(tmp_path, caplog):
    write_challenge(tmp_path, "Not Valid", "proxy_port: 1337\n")
    assert instancer.load_challenges(str(tmp_path)) == {}
    assert "lowercase letters" in caplog.text


def test_an_unknown_mode_falls_back_to_http(tmp_path, caplog):
    write_challenge(tmp_path, "pigeon", "proxy_port: 1337\nmode: carrier-pigeon\n")
    assert instancer.load_challenges(str(tmp_path))["pigeon"].mode == "http"
    assert "unknown mode" in caplog.text


def test_a_ttl_that_is_not_a_lifetime_falls_back(tmp_path, caplog):
    write_challenge(tmp_path, "forever", "proxy_port: 1337\nttl: 0\n")
    assert instancer.load_challenges(str(tmp_path))["forever"].ttl == instancer.DEFAULT_TTL
    assert "is not a lifetime" in caplog.text


def test_a_challenge_setting_that_is_not_a_number_falls_back(tmp_path, caplog):
    write_challenge(tmp_path, "wordy", "proxy_port: 1337\nttl: soon\n")
    assert instancer.load_challenges(str(tmp_path))["wordy"].ttl == instancer.DEFAULT_TTL
    assert "is not a number" in caplog.text


def test_unknown_challenge_keys_are_ignored_out_loud(tmp_path, caplog):
    write_challenge(tmp_path, "extra", "proxy_port: 1337\nflag: AIS3{...}\n")
    assert instancer.load_challenges(str(tmp_path))["extra"].proxy_port == 1337
    assert "ignoring unknown key(s)" in caplog.text


def test_one_bad_challenge_does_not_take_the_others_down(tmp_path, caplog):
    write_challenge(tmp_path, "good", "proxy_port: 1337\n")
    write_challenge(tmp_path, "broken", config=None)
    assert list(instancer.load_challenges(str(tmp_path))) == ["good"]


def test_a_directory_that_is_not_there_serves_nothing(tmp_path, caplog):
    assert instancer.load_challenges(str(tmp_path / "nope")) == {}
    assert "serving no challenges" in caplog.text


# --- create / status / destroy ------------------------------------------------

def test_create_starts_container_and_network(web, daemon, chal):
    data = create(web, chal.id).get_json()
    assert data["running"] is True
    assert data["chal"] == chal.id
    assert data["mode"] == "http"
    assert data["proxy_port"] == chal.proxy_port
    assert data["expires_at"] is not None
    assert 0 < data["remaining_time"] <= chal.ttl
    assert daemon.create_calls == 1
    assert len(instance_networks(daemon)) == 1

    container = only_container(daemon)
    assert container.status == "running"
    assert container.image == chal.image
    assert chal.port_min <= port_of(container) <= chal.port_max
    assert container.network == instancer.network_name(chal.id, container.labels["ctf.owner"])


def test_names_carry_the_challenge_and_the_owner(web, daemon, chal):
    create(web, chal.id)
    container = only_container(daemon)
    owner = container.labels["ctf.owner"]
    assert container.name == "ctf-instance-%s-%s" % (chal.id, owner)
    assert only_network(daemon).name == "ctf-network-%s-%s" % (chal.id, owner)


def test_the_instance_is_told_which_port_to_listen_on(web, daemon, chal):
    create(web, chal.id)
    container = only_container(daemon)
    assert container.environment == {"CHAL_PORT": str(port_of(container))}


def test_nothing_is_published_to_the_host(web, daemon, chal):
    create(web, chal.id)
    assert "ports" not in only_container(daemon).kwargs


def test_the_challenges_limits_are_applied_to_its_instances(web, daemon, monkeypatch):
    add_challenge(monkeypatch, make_challenge("small", port=1400, mem_limit="64m",
                                             pids_limit=8))
    create(web, "small")
    container = only_container(daemon, "small")
    assert container.kwargs["mem_limit"] == "64m"
    assert container.kwargs["pids_limit"] == 8


def test_status_reports_running_instance(web, daemon, chal):
    created = create(web, chal.id).get_json()
    now = status(web, chal.id).get_json()
    assert now["running"] is True
    assert now["key"] == created["key"]
    assert now["expires_at"] == created["expires_at"]
    assert now["remaining_time"] <= created["remaining_time"]


def test_create_is_idempotent_within_a_session(web, daemon, chal):
    first = create(web, chal.id).get_json()
    second = create(web, chal.id).get_json()
    assert first["key"] == second["key"]
    assert daemon.create_calls == 1
    assert len(instances(daemon)) == 1


def test_destroy_removes_container_and_network(web, daemon, chal):
    create(web, chal.id)
    assert destroy(web, chal.id).get_json() == idle(chal)
    assert instances(daemon) == []
    assert instance_networks(daemon) == []
    assert status(web, chal.id).get_json() == idle(chal)


def test_destroy_without_container(web, chal):
    assert destroy(web, chal.id).status_code == 200
    assert destroy(web, chal.id).get_json() == idle(chal)


def test_failed_create_cleans_up_container_and_network(web, daemon, chal):
    daemon.fail_start = True
    response = create(web, chal.id)
    assert response.status_code == 500
    assert response.get_json()["running"] is False
    assert instances(daemon) == []
    assert instance_networks(daemon) == []      # network rolled back too

    daemon.fail_start = False
    assert create(web, chal.id).get_json()["running"] is True
    # the port of the rolled-back attempt was never marked used
    assert port_of(only_container(daemon)) == chal.port_min


def test_concurrent_create_in_one_session(daemon, chal):
    daemon.create_delay = 0.2
    web = instancer.app.test_client()
    web.get("/")
    results = []
    run_together([lambda: results.append(create(web, chal.id).get_json())] * 2)
    assert daemon.create_calls == 1
    assert len(instances(daemon)) == 1
    assert results[0]["key"] == results[1]["key"]


# --- one session, many challenges ---------------------------------------------

def test_one_session_can_hold_one_instance_of_each_challenge(web, daemon, pwn, chal):
    mine = create(web, pwn.id).get_json()
    also = create(web, chal.id).get_json()
    assert mine["key"] != also["key"]
    assert len(instances(daemon)) == 2
    assert len(instance_networks(daemon)) == 2
    assert {c.labels["ctf.chal"] for c in instances(daemon)} == {pwn.id, chal.id}
    # ...and one owner, because it is one browser
    assert len({c.labels["ctf.owner"] for c in instances(daemon)}) == 1


def test_challenges_do_not_see_each_others_instances(web, daemon, pwn, chal):
    create(web, pwn.id)
    assert status(web, chal.id).get_json() == idle(chal)
    assert destroy(web, chal.id).get_json() == idle(chal)
    assert len(instances(daemon)) == 1        # the pwn one is untouched


def test_two_challenges_may_hand_out_the_same_instance_port(web, daemon, pwn, chal,
                                                            monkeypatch):
    # The port lives inside the instance's own network namespace, so the range
    # is walked once per challenge and the same number is free in both.
    for one in (pwn, chal):
        monkeypatch.setattr(one, "port_max", one.port_min)
    create(web, pwn.id)
    create(web, chal.id)
    assert len(instances(daemon)) == 2
    assert {port_of(c) for c in instances(daemon)} == {pwn.port_min}


def test_two_challenges_never_share_a_subnet(web, daemon, pwn, chal):
    create(web, pwn.id)
    create(web, chal.id)
    assert len({c.labels["ctf.subnet"] for c in instances(daemon)}) == 2


def test_each_instance_gets_the_ttl_of_its_own_challenge(web, daemon, monkeypatch):
    add_challenge(monkeypatch, make_challenge("brief", port=1401, ttl=60))
    assert 0 < create(web, "brief").get_json()["remaining_time"] <= 60
    assert create(web, WEB).get_json()["remaining_time"] > 60


def test_a_requested_ttl_is_ignored(web, daemon, chal):
    """Nobody gets to ask for a longer instance -- the challenge decides."""
    data = create(web, chal.id, json={"ttl": 999999}).get_json()
    assert 0 < data["remaining_time"] <= chal.ttl


# --- the cap ------------------------------------------------------------------

def test_a_full_challenge_turns_the_next_player_away(daemon, monkeypatch, web, other):
    capped = add_challenge(monkeypatch, make_challenge("capped", port=1402, cap=1))
    assert create(web, capped.id).get_json()["running"] is True
    response = create(other, capped.id)
    assert response.status_code == 503
    assert response.get_json()["error"] == instancer.ERROR_BUSY
    assert len(instances(daemon, capped.id)) == 1


def test_a_full_challenge_still_hands_you_back_your_own(daemon, monkeypatch, web, other):
    capped = add_challenge(monkeypatch, make_challenge("capped", port=1402, cap=1))
    mine = create(web, capped.id).get_json()
    create(other, capped.id)                       # fills nothing, it is already full
    assert create(web, capped.id).get_json()["key"] == mine["key"]


def test_a_freed_slot_goes_to_the_next_player(daemon, monkeypatch, web, other):
    capped = add_challenge(monkeypatch, make_challenge("capped", port=1402, cap=1))
    create(web, capped.id)
    assert create(other, capped.id).status_code == 503
    destroy(web, capped.id)
    assert create(other, capped.id).get_json()["running"] is True


def test_the_cap_is_one_challenges_own(daemon, monkeypatch, web, chal):
    capped = add_challenge(monkeypatch, make_challenge("capped", port=1402, cap=1))
    other = instancer.app.test_client(); other.get("/")
    create(web, capped.id)
    assert create(other, capped.id).status_code == 503   # that one is full
    assert create(other, chal.id).get_json()["running"] is True   # this one is not


def test_a_crowd_cannot_squeeze_past_the_cap(daemon, monkeypatch):
    """Two players arriving at once must not both be handed the last slot."""
    capped = add_challenge(monkeypatch, make_challenge("capped", port=1402, cap=5))
    daemon.create_delay = 0.02
    _, answers = crowd(daemon, capped.id, count=16)
    started = [a for a in answers if a.get("running")]
    assert len(started) == 5
    assert len(instances(daemon, capped.id)) == 5
    assert all(a["error"] == instancer.ERROR_BUSY
               for a in answers if not a.get("running"))


def test_the_cap_is_what_the_page_promises(daemon, monkeypatch, web):
    add_challenge(monkeypatch, make_challenge("capped", port=1402, cap=7))
    body = web.get("/c/capped").get_data(as_text=True)
    assert "UP TO 7 AT ONCE" in body


# --- a crowd ------------------------------------------------------------------

CROWD = 24


def crowd(daemon, chal=WEB, count=CROWD):
    """`count` players, each with their own session, all pressing START at once."""
    clients = []
    for _ in range(count):
        client = instancer.app.test_client()
        client.get("/")
        clients.append(client)
    answers = []
    run_together([lambda c=c: answers.append(create(c, chal).get_json())
                  for c in clients])
    return clients, answers


def test_a_crowd_never_shares_a_resource(daemon, monkeypatch):
    # The whole point: every player who gets an instance gets one that is
    # entirely theirs -- port, subnet, key, container, network.
    chal = instancer.CHALLENGES[WEB]
    monkeypatch.setattr(chal, "port_max", chal.port_min + CROWD)
    daemon.create_delay = 0.01
    _, answers = crowd(daemon)
    assert all(a["running"] for a in answers)
    assert len(instances(daemon)) == CROWD
    assert len(instance_networks(daemon)) == CROWD

    ports = [port_of(c) for c in instances(daemon)]
    subnets = [c.labels["ctf.subnet"] for c in instances(daemon)]
    keys = [a["key"] for a in answers]
    assert len(set(ports)) == CROWD
    assert len(set(subnets)) == CROWD
    assert len(set(keys)) == CROWD


def test_a_crowd_larger_than_the_pool_is_turned_away_not_double_booked(daemon, monkeypatch):
    # Six ports, twenty-four players: some must be told to wait, and none of
    # them may be handed a port that is already somebody's.
    monkeypatch.setattr(instancer.CHALLENGES[WEB], "port_max",
                        instancer.CHALLENGES[WEB].port_min + 5)
    daemon.create_delay = 0.01
    _, answers = crowd(daemon)
    started = [a for a in answers if a.get("running")]
    turned_away = [a for a in answers if not a.get("running")]
    assert len(started) == 6
    assert len(turned_away) == CROWD - 6
    assert all(a["error"] == instancer.ERROR_BUSY for a in turned_away)
    assert len({port_of(c) for c in instances(daemon)}) == 6


def test_a_crowd_on_two_challenges_at_once(daemon, monkeypatch):
    for one in instancer.CHALLENGES.values():
        monkeypatch.setattr(one, "port_max", one.port_min + 5)
    daemon.create_delay = 0.01
    clients = []
    for _ in range(12):
        client = instancer.app.test_client()
        client.get("/")
        clients.append(client)
    answers = []
    run_together([lambda c=c, k=PWN if i % 2 else WEB:
                  answers.append(create(c, k).get_json())
                  for i, c in enumerate(clients)])
    assert all(a["running"] for a in answers)
    assert len(instances(daemon)) == 12
    assert len(instance_networks(daemon)) == 12
    # every subnet is distinct across challenges; ports only within one
    assert len({c.labels["ctf.subnet"] for c in instances(daemon)}) == 12
    for cid in (PWN, WEB):
        assert len({port_of(c) for c in instances(daemon, cid)}) == 6


def test_reservations_are_given_back_when_a_create_fails(daemon, chal):
    daemon.fail_start = True
    web = instancer.app.test_client()
    web.get("/")
    create(web, chal.id)
    assert instancer.reserved_ports == set()
    assert instancer.reserved_subnets == set()


def test_polling_status_cannot_delete_an_instance_being_built(daemon, chal):
    """A container is briefly "created", not "running". A poll in that window
    used to sweep it away as stale -- while its own create was still building."""
    web = instancer.app.test_client()
    web.get("/")
    poller = instancer.app.test_client()
    poller.set_cookie("session", web.get_cookie("session").value)

    daemon.start_delay = 0.3           # widen the created-but-not-running window
    answers = []

    def poll_mid_start():
        daemon.starting.wait(2)        # land inside the window, not before it
        answers.append(("status", status(poller, chal.id).get_json()))

    run_together([
        lambda: answers.append(("create", create(web, chal.id).get_json())),
        poll_mid_start,
    ])
    created = dict(answers)["create"]
    assert created["running"] is True
    assert len(instances(daemon)) == 1
    assert only_container(daemon).status == "running"


def test_polling_the_challenge_list_cannot_delete_an_instance_being_built(daemon, chal):
    """The list polls too, and it asks the same question /status does."""
    web = instancer.app.test_client()
    web.get("/")
    poller = instancer.app.test_client()
    poller.set_cookie("session", web.get_cookie("session").value)

    daemon.start_delay = 0.3
    answers = []

    def list_mid_start():
        daemon.starting.wait(2)
        answers.append(("list", poller.get("/api/challenges").get_json()))

    run_together([
        lambda: answers.append(("create", create(web, chal.id).get_json())),
        list_mid_start,
    ])
    assert dict(answers)["create"]["running"] is True
    assert len(instances(daemon)) == 1
    assert only_container(daemon).status == "running"


def test_the_reaper_cannot_take_a_network_out_of_a_create(daemon, chal):
    """The network exists a moment before the container does. The reaper used to
    see that moment as an orphan."""
    web = instancer.app.test_client()
    web.get("/")
    daemon.create_delay = 0.3
    answers = []
    run_together([
        lambda: answers.append(create(web, chal.id).get_json()),
        lambda: instancer.reap_expired(),
    ])
    assert answers[0]["running"] is True
    assert len(instances(daemon)) == 1
    assert len(instance_networks(daemon)) == 1


def test_the_reaper_leaves_a_rebuilt_instance_alone(daemon, chal, monkeypatch):
    """An expired instance destroyed and rebuilt between the reaper's listing and
    its kill must not be reaped in its successor's place."""
    web = instancer.app.test_client()
    web.get("/")
    create(web, chal.id)
    only_container(daemon).labels["ctf.expires_at"] = str(int(time.time()) - 1)

    # Slip a rebuild in after the reaper has listed the instance but before it
    # takes the owner's lock -- the exact window another thread would use.
    real_lock, rebuilt = instancer.owner_lock, []

    def rebuild_first(chal_id, owner):
        if not rebuilt:
            rebuilt.append(True)
            destroy(web, chal_id)
            create(web, chal_id)
        return real_lock(chal_id, owner)

    monkeypatch.setattr(instancer, "owner_lock", rebuild_first)
    instancer.reap_expired()

    assert len(instances(daemon)) == 1          # the fresh one survived
    assert instancer.instance_expires_at(only_container(daemon)) > int(time.time())


# --- restart / persistence ----------------------------------------------------

def test_existing_container_is_adopted_across_restart(web, daemon, chal):
    created = create(web, chal.id).get_json()
    # a fresh Flask client (simulating a restarted process) with the same cookie
    restarted = instancer.app.test_client()
    restarted.set_cookie("session", web.get_cookie("session").value)

    now = status(restarted, chal.id).get_json()
    assert now["key"] == created["key"]
    assert now["expires_at"] == created["expires_at"]
    assert create(restarted, chal.id).get_json()["key"] == created["key"]
    assert daemon.create_calls == 1   # not recreated


def test_stale_container_and_network_are_cleaned_up(web, daemon, chal):
    create(web, chal.id)
    only_container(daemon).status = "exited"
    assert status(web, chal.id).get_json() == idle(chal)
    assert instances(daemon) == []
    assert instance_networks(daemon) == []


def test_labels_carry_metadata(web, daemon, chal):
    created = create(web, chal.id).get_json()
    container = only_container(daemon)
    owner = container.labels["ctf.owner"]
    assert container.labels["ctf.chal"] == chal.id
    assert container.labels["ctf.expires_at"].isdigit()
    assert chal.port_min <= port_of(container) <= chal.port_max
    assert container.labels["ctf.key"] == created["key"]
    assert ipaddress.ip_network(container.labels["ctf.subnet"]).prefixlen == 24
    network = only_network(daemon)
    assert network.labels["ctf.chal"] == chal.id
    assert network.labels["ctf.owner"] == owner
    assert network.labels["ctf.subnet"] == container.labels["ctf.subnet"]


def test_an_instance_is_read_back_from_its_labels(web, daemon, chal):
    create(web, chal.id)
    container = only_container(daemon)
    assert instancer.instance_chal(container) == chal.id
    assert instancer.instance_owner(container) == container.labels["ctf.owner"]


def test_an_instance_is_read_back_from_its_name_when_the_labels_are_gone(web, daemon, chal):
    create(web, chal.id)
    container = only_container(daemon)
    owner = container.labels["ctf.owner"]
    container.labels = {}
    assert instancer.instance_chal(container) == chal.id
    assert instancer.instance_owner(container) == owner


def test_a_player_is_told_only_what_they_can_use(web, daemon, chal):
    # How to connect, and how long they have. Nothing about our machinery.
    assert set(create(web, chal.id).get_json()) == {
        "chal", "name", "running", "mode", "key", "proxy_host", "proxy_port",
        "expires_at", "remaining_time"}


def test_a_failure_does_not_hand_the_player_our_logs(web, daemon, chal):
    daemon.fail_start = True
    answer = create(web, chal.id).get_json()
    assert answer["error"] == instancer.ERROR_CREATE
    assert "start failed" not in repr(answer)     # Docker's words stay in the log


# --- network isolation --------------------------------------------------------

def test_instance_network_is_internal_and_gatewayless(web, daemon, chal):
    create(web, chal.id)
    network = only_network(daemon)
    assert network.internal is True
    assert network.options[instancer.GATEWAY_MODE_OPTION] == "isolated"


def test_old_daemon_falls_back_to_a_plain_internal_network(web, daemon, chal, caplog):
    daemon.reject_options = True
    assert create(web, chal.id).get_json()["running"] is True
    network = only_network(daemon)
    assert network.internal is True
    assert network.options == {}
    assert "upgrade to Docker 28+" in caplog.text


def test_the_challenges_own_proxy_is_attached_to_the_instance_network(web, daemon, chal):
    create(web, chal.id)
    assert chal.proxy in only_network(daemon).endpoints


def test_no_other_challenges_proxy_is_on_that_network(web, daemon, pwn, chal):
    create(web, chal.id)
    endpoints = only_network(daemon).endpoints
    assert chal.proxy in endpoints
    assert pwn.proxy not in endpoints


def test_destroy_detaches_the_proxy_before_removing_the_network(web, daemon, chal):
    create(web, chal.id)
    destroy(web, chal.id)
    assert instance_networks(daemon) == []   # a still-attached proxy would have blocked this


def test_create_rolls_back_when_the_proxy_is_missing(web, daemon, chal):
    daemon.containers[chal.proxy].remove(force=True)   # attaching finds nothing
    assert create(web, chal.id).status_code == 500
    assert instances(daemon) == []
    assert instance_networks(daemon) == []


# --- keys ---------------------------------------------------------------------

def test_key_is_unguessable_and_unique_per_session(web, other, daemon, chal):
    mine = create(web, chal.id).get_json()["key"]
    theirs = create(other, chal.id).get_json()["key"]
    assert re.fullmatch(r"[0-9a-f]{32}", mine)
    assert mine != theirs


def test_route_resolves_a_key_to_its_instance(web, daemon, chal):
    # The proxy is the one caller that does get the address and the port.
    created = create(web, chal.id).get_json()
    container = only_container(daemon)
    assert lookup(web, created["key"], chal.id).get_json() == {
        "host": container.ip, "port": port_of(container)}


def test_route_needs_the_proxy_token(web, daemon, chal):
    key = create(web, chal.id).get_json()["key"]
    assert lookup(web, key, chal.id, token=None).status_code == 404
    assert lookup(web, key, chal.id, token="wrong").status_code == 404


def test_every_challenge_has_a_token_of_its_own(daemon, pwn, chal):
    assert instancer.proxy_token(pwn.id) != instancer.proxy_token(chal.id)
    assert instancer.proxy_token(chal.id) == instancer.proxy_token(chal.id)


def test_one_challenges_token_does_not_open_another(web, daemon, pwn, chal):
    key = create(web, chal.id).get_json()["key"]
    assert lookup(web, key, chal.id, token=instancer.proxy_token(pwn.id)).status_code == 404


def test_a_key_is_only_resolved_for_the_challenge_it_belongs_to(web, daemon, pwn, chal):
    key = create(web, chal.id).get_json()["key"]
    # the pwn proxy asking, correctly, about its own challenge -- with a key
    # that is not one of its own
    assert lookup(web, key, pwn.id, token=instancer.proxy_token(pwn.id)).status_code == 404


def test_route_of_an_unknown_challenge_is_a_404(web, daemon, chal):
    key = create(web, chal.id).get_json()["key"]
    assert lookup(web, key, "nope", token=instancer.proxy_token("nope")).status_code == 404


def test_a_token_that_is_not_even_ascii_is_a_404_like_any_other(web, daemon, chal):
    key = create(web, chal.id).get_json()["key"]
    assert lookup(web, key, chal.id, token="nöpe").status_code == 404


def test_route_refuses_everything_without_a_configured_token(web, daemon, chal, monkeypatch):
    key = create(web, chal.id).get_json()["key"]
    monkeypatch.setattr(instancer, "PROXY_TOKEN", "")
    assert lookup(web, key, chal.id, token="").status_code == 404


def test_route_of_an_unknown_key_is_a_404(web, daemon, chal):
    create(web, chal.id)
    assert lookup(web, "deadbeef" * 4, chal.id).status_code == 404


def test_key_stops_working_once_the_instance_is_gone(web, daemon, chal):
    key = create(web, chal.id).get_json()["key"]
    destroy(web, chal.id)
    assert lookup(web, key, chal.id).status_code == 404


def test_key_of_a_stopped_instance_does_not_route(web, daemon, chal):
    key = create(web, chal.id).get_json()["key"]
    only_container(daemon).status = "exited"
    assert lookup(web, key, chal.id).status_code == 404


# --- ports --------------------------------------------------------------------

def test_ports_outside_range_are_never_used(web, daemon, chal, monkeypatch):
    monkeypatch.setattr(chal, "port_min", 31020)
    monkeypatch.setattr(chal, "port_max", 31021)
    create(web, chal.id)
    assert port_of(only_container(daemon)) in (31020, 31021)


def test_each_challenge_walks_its_own_range(web, daemon, pwn, chal, monkeypatch):
    monkeypatch.setattr(pwn, "port_min", 32000)
    monkeypatch.setattr(pwn, "port_max", 32000)
    create(web, pwn.id)
    create(web, chal.id)
    assert port_of(only_container(daemon, pwn.id)) == 32000
    assert port_of(only_container(daemon, chal.id)) == chal.port_min


def test_two_sessions_get_distinct_ports(web, other, daemon, chal):
    create(web, chal.id)
    create(other, chal.id)
    assert len({port_of(c) for c in instances(daemon)}) == 2


def test_a_destroyed_instance_gives_its_port_back(web, daemon, chal):
    create(web, chal.id)
    port = port_of(only_container(daemon))
    destroy(web, chal.id)
    create(web, chal.id)
    assert port_of(only_container(daemon)) == port


def test_create_fails_when_port_range_exhausted(web, other, daemon, chal, monkeypatch):
    monkeypatch.setattr(chal, "port_max", chal.port_min)
    create(web, chal.id)
    response = create(other, chal.id)
    assert response.status_code == 503
    assert response.get_json()["error"] == instancer.ERROR_BUSY
    assert len(instances(daemon)) == 1


# --- subnets ------------------------------------------------------------------

def test_two_sessions_get_distinct_subnets(web, other, daemon, chal):
    create(web, chal.id)
    create(other, chal.id)
    assert len({c.labels["ctf.subnet"] for c in instances(daemon)}) == 2
    assert len(instance_networks(daemon)) == 2


def test_pick_subnet_skips_already_used_subnet(daemon, pwn):
    daemon.add_network("someone", "10.100.0.0/24")
    assert instancer.pick_subnet(pwn) == ipaddress.ip_network("10.100.1.0/24")


def test_pick_subnet_ignores_networks_outside_the_pool(daemon, pwn):
    daemon.add_network("bridge", "172.17.0.0/16")
    assert instancer.pick_subnet(pwn) == ipaddress.ip_network("10.100.0.0/24")


def test_pick_subnet_exhaustion_raises(daemon, pwn, monkeypatch):
    monkeypatch.setattr(pwn, "subnet_pool", ipaddress.ip_network("10.100.0.0/24"))
    daemon.add_network("taken", "10.100.0.0/24")
    with pytest.raises(RuntimeError, match="no free"):
        instancer.pick_subnet(pwn)


def test_subnets_come_from_the_pool(web, daemon, chal):
    create(web, chal.id)
    subnet = ipaddress.ip_network(only_container(daemon).labels["ctf.subnet"])
    assert subnet.subnet_of(chal.subnet_pool)
    assert subnet.prefixlen == chal.subnet_prefix


def test_each_challenge_draws_from_its_own_pool(web, daemon, pwn, chal):
    create(web, pwn.id)
    create(web, chal.id)
    for one in (pwn, chal):
        subnet = ipaddress.ip_network(
            only_container(daemon, one.id).labels["ctf.subnet"])
        assert subnet.subnet_of(one.subnet_pool)


def test_overlapping_pools_share_the_space_instead_of_colliding(web, other, daemon,
                                                                pwn, chal, monkeypatch):
    """Two challenges pointed at the same pool is allowed -- they just run out
    sooner. Nothing may be handed the same /24 twice."""
    monkeypatch.setattr(chal, "subnet_pool", pwn.subnet_pool)
    create(web, pwn.id)
    create(other, chal.id)
    assert len({c.labels["ctf.subnet"] for c in instances(daemon)}) == 2


# --- TTL ----------------------------------------------------------------------

def test_ttl_is_the_challenges_own(web, daemon, chal):
    before = int(time.time())
    data = create(web, chal.id).get_json()
    assert before + chal.ttl <= data["expires_at"] <= int(time.time()) + chal.ttl


# --- reaper -------------------------------------------------------------------

def test_reaper_destroys_expired_instance(web, daemon, chal):
    create(web, chal.id)
    only_container(daemon).labels["ctf.expires_at"] = str(int(time.time()) - 1)
    instancer.reap_expired()
    assert instances(daemon) == []
    assert instance_networks(daemon) == []


def test_reaper_keeps_live_instance(web, daemon, chal):
    create(web, chal.id)
    instancer.reap_expired()
    assert len(instances(daemon)) == 1
    assert len(instance_networks(daemon)) == 1


def test_reaper_only_kills_the_expired_one(web, other, daemon, chal):
    create(web, chal.id)
    create(other, chal.id)
    victim = instances(daemon)[0]
    victim.labels["ctf.expires_at"] = str(int(time.time()) - 1)
    instancer.reap_expired()
    assert len(instances(daemon)) == 1
    assert victim.name not in daemon.containers
    assert len(instance_networks(daemon)) == 1


def test_reaper_does_not_cross_challenges(web, daemon, pwn, chal):
    create(web, pwn.id)
    create(web, chal.id)
    doomed = only_container(daemon, pwn.id)
    doomed.labels["ctf.expires_at"] = str(int(time.time()) - 1)
    instancer.reap_expired()
    assert instances(daemon, pwn.id) == []
    assert len(instances(daemon, chal.id)) == 1


def test_reaper_still_expires_an_instance_of_a_challenge_that_is_gone(web, daemon, chal,
                                                                     monkeypatch):
    """Take a challenge off disk mid-event and its instances still expire: the
    reaper reads labels, not the config it was started with."""
    create(web, chal.id)
    only_container(daemon).labels["ctf.expires_at"] = str(int(time.time()) - 1)
    monkeypatch.setattr(instancer, "CHALLENGES", {})
    instancer.reap_expired()
    assert instances(daemon) == []


def test_reaper_prunes_orphan_network(daemon, chal):
    daemon.add_network(instancer.network_name(chal.id, "ghost"), "10.100.9.0/24")
    instancer.reap_expired()
    assert instance_networks(daemon) == []


def test_reaper_prunes_an_orphan_network_the_proxy_is_still_on(daemon, chal):
    name = instancer.network_name(chal.id, "ghost")
    daemon.add_network(name, "10.100.9.0/24")
    daemon.networks[name].connect(chal.proxy)
    instancer.reap_expired()
    assert instance_networks(daemon) == []


def test_reaper_keeps_network_with_container(web, daemon, chal):
    create(web, chal.id)
    instancer.prune_orphan_networks()
    assert len(instance_networks(daemon)) == 1


# --- proxies ------------------------------------------------------------------

def test_startup_gives_every_challenge_a_proxy(daemon, pwn, chal):
    instancer.startup()
    assert {c.name for c in proxies(daemon)} == {pwn.proxy, chal.proxy}


def test_a_proxy_is_told_what_it_is_and_nothing_else(daemon, chal):
    proxy = daemon.containers[chal.proxy]
    assert proxy.environment["PROXY_CHAL"] == chal.id
    assert proxy.environment["MODE"] == chal.mode
    assert proxy.environment["PROXY_PORT"] == str(chal.proxy_port)
    assert proxy.environment["PROXY_TOKEN"] == instancer.proxy_token(chal.id)
    assert proxy.environment["PROXY_BIND"] == proxy.ip
    assert proxy.status == "running"


def test_a_proxy_binds_an_address_of_its_own_on_the_control_network(daemon, pwn, chal):
    bound = {daemon.containers[c.proxy].environment["PROXY_BIND"] for c in (pwn, chal)}
    assert len(bound) == 2
    control = ipaddress.ip_network(CONTROL_SUBNET)
    for address in bound:
        assert ipaddress.ip_address(address) in control


def test_a_proxy_publishes_its_challenges_port(daemon, chal):
    proxy = daemon.containers[chal.proxy]
    assert proxy.kwargs["ports"] == {"%d/tcp" % chal.proxy_port: chal.proxy_port}
    assert proxy.kwargs["restart_policy"] == {"Name": "unless-stopped"}


def test_a_running_proxy_is_adopted_not_replaced(daemon, chal, caplog):
    caplog.set_level(logging.INFO, logger="instancer")
    created = daemon.containers[chal.proxy]
    adopted = instancer.ensure_proxy(chal)
    assert adopted is created
    assert "adopted proxy" in caplog.text


def test_a_proxy_of_the_previous_config_is_replaced(daemon, chal, monkeypatch):
    moved = make_challenge(chal.id, mode=chal.mode, port=chal.proxy_port + 1)
    monkeypatch.setitem(instancer.CHALLENGES, chal.id, moved)
    instancer.ensure_proxy(moved)
    proxy = daemon.containers[moved.proxy]
    assert proxy.kwargs["ports"] == {"%d/tcp" % moved.proxy_port: moved.proxy_port}


def test_a_stopped_proxy_is_replaced(daemon, chal):
    daemon.containers[chal.proxy].status = "exited"
    assert instancer.ensure_proxy(chal).status == "running"


def test_the_proxy_of_a_challenge_that_is_gone_is_removed(daemon, chal, monkeypatch):
    monkeypatch.setattr(instancer, "CHALLENGES", {})
    instancer.remove_stale_proxies()
    assert proxies(daemon) == []


def test_removing_a_stale_proxy_takes_its_instances_with_it(web, daemon, chal, monkeypatch):
    create(web, chal.id)
    monkeypatch.setattr(instancer, "CHALLENGES", {})
    instancer.remove_stale_proxies()
    assert proxies(daemon) == []
    assert instances(daemon) == []
    assert instance_networks(daemon) == []


def test_startup_builds_the_proxy_image_and_one_per_challenge(daemon, pwn, chal):
    instancer.startup()
    assert (instancer.PROXY_DIR, instancer.PROXY_IMAGE) in daemon.builds
    assert (pwn.dir, pwn.image) in daemon.builds
    assert (chal.dir, chal.image) in daemon.builds


def test_startup_skips_builds_when_the_images_are_present(daemon, pwn, chal):
    daemon.images.update({instancer.PROXY_IMAGE, pwn.image, chal.image})
    instancer.startup()
    assert daemon.builds == []                     # no rebuild -> fast startup


def test_force_build_rebuilds_even_when_present(daemon, pwn, chal, monkeypatch):
    daemon.images.update({instancer.PROXY_IMAGE, pwn.image, chal.image})
    monkeypatch.setattr(instancer, "FORCE_BUILD", True)
    instancer.startup()
    assert len(daemon.builds) == 3


def test_a_challenge_that_will_not_build_is_dropped_not_fatal(daemon, pwn, chal,
                                                              monkeypatch, caplog):
    def build(tag, path, what):
        if tag == pwn.image:
            raise APIError("build failed")

    monkeypatch.setattr(instancer, "build_image", build)
    instancer.startup()
    assert list(instancer.CHALLENGES) == [chal.id]
    assert "dropping challenge %s" % pwn.id in caplog.text


def test_startup_says_so_when_there_is_no_control_network(daemon, monkeypatch, caplog):
    monkeypatch.setattr(instancer, "CONTROL_NETWORK", "not-there")
    with pytest.raises(NotFound):
        instancer.startup()
    assert "no control network" in caplog.text


# --- shutdown -----------------------------------------------------------------

def test_destroy_all_takes_every_instance_and_every_proxy(web, other, daemon, pwn, chal):
    create(web, pwn.id)
    create(other, chal.id)
    assert len(instances(daemon)) == 2
    instancer.destroy_all()
    assert daemon.containers == {}
    assert instance_networks(daemon) == []


def test_shutdown_destroys_what_compose_cannot_see(web, daemon, chal):
    create(web, chal.id)
    with pytest.raises(SystemExit):
        instancer.handle_shutdown(signal.SIGTERM, None)
    assert daemon.containers == {}


def test_shutdown_can_be_told_to_leave_instances_running(web, daemon, chal, monkeypatch):
    monkeypatch.setattr(instancer, "REAP_ON_SHUTDOWN", False)
    create(web, chal.id)
    with pytest.raises(SystemExit):
        instancer.handle_shutdown(signal.SIGTERM, None)
    assert len(instances(daemon)) == 1     # adopted again on the way back up
    assert len(proxies(daemon)) == len(instancer.CHALLENGES)   # and so are the proxies


def test_shutdown_exits_even_when_cleanup_fails(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "destroy_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("docker is gone")))
    with pytest.raises(SystemExit):
        instancer.handle_shutdown(signal.SIGTERM, None)


# --- mode ---------------------------------------------------------------------

def test_the_mode_reported_is_the_challenges_own(web, daemon, pwn, chal):
    assert create(web, pwn.id).get_json()["mode"] == "netcat"
    assert create(web, chal.id).get_json()["mode"] == "http"
    assert status(web, pwn.id).get_json()["mode"] == "netcat"
    assert destroy(web, pwn.id).get_json() == idle(pwn)


def test_the_page_carries_its_challenges_mode_and_proxy(web, daemon, pwn):
    body = web.get("/c/%s" % pwn.id).get_data(as_text=True)
    assert '"netcat"' in body
    assert "const PROXY_PORT = 1337" in body


# --- multi-session isolation --------------------------------------------------

def test_a_session_only_sees_its_own_instance(web, other, daemon, chal):
    create(web, chal.id)
    assert status(other, chal.id).get_json() == idle(chal)


def test_a_session_cannot_destroy_another(web, other, daemon, chal):
    create(web, chal.id)
    assert destroy(other, chal.id).get_json() == idle(chal)
    assert status(web, chal.id).get_json()["running"] is True
    assert len(instances(daemon)) == 1

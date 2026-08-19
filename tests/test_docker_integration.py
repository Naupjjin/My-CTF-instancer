"""Integration tests: the real Docker daemon, the real proxies, the real challenges.

The instancer runs in-process (a Flask server in a thread, so the proxy
containers can call it back); everything else is what production runs, including
the two challenges in challenges/ and the proxy the instancer creates for each.
Skipped automatically when no Docker daemon is reachable.
"""

import http.client
import ipaddress
import socket
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

import app as instancer

REPO = Path(__file__).resolve().parents[1]
CHALLENGES = REPO / "challenges"

# The two challenges the repo ships, one of each kind.
PWN = "special-love"
WEB = "cookie-jar"

# Everything the test makes is named apart from anything real on the daemon, and
# published on ports nothing else is likely to want.
PREFIX = "ctf-instance-test-"
NET_PREFIX = "ctf-network-test-"
PROXY_PREFIX = "ctf-proxy-test-"
IMAGE = "ctf-challenge-{chal}:test"
PROXY_IMAGE = "ctf-proxy:test"
CONTROL_NET = "ctf-control-test"
CONTROL_SUBNET = "10.239.9.0/24"
SECRET = "integration-test-secret"
TOKEN = "integration-test-token"

PORTS = {PWN: 31337, WEB: 31338}

# Where each challenge's instances live for the run. In production this is what
# challenges/<id>/config.yml says; here it is overridden onto the loaded
# Challenge, off the ranges a real deployment would use.
POOLS = {PWN: "10.245.0.0/16", WEB: "10.246.0.0/16"}
PORT_MIN = 30000
PORT_MAX = 30005

docker = pytest.importorskip("docker")


# --- the world under test -----------------------------------------------------

@pytest.fixture(scope="module")
def real_client():
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.skip("no reachable Docker daemon: %s" % exc)
    for port in PORTS.values():
        if in_use(port):
            pytest.skip("port %d is already taken on this machine" % port)
    remove_leftovers(client)
    yield client
    remove_leftovers(client)
    for tag in [PROXY_IMAGE] + [IMAGE.format(chal=c) for c in PORTS]:
        try:
            client.images.remove(tag, force=True)
        except docker.errors.ImageNotFound:
            pass


def in_use(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


@pytest.fixture(scope="module")
def world(real_client):
    """Configure the instancer, serve it, and let it raise its own proxies."""
    saved = {name: getattr(instancer, name) for name in CONFIG}
    for name, value in CONFIG.items():
        setattr(instancer, name, value)
    instancer.app.secret_key = SECRET

    control = real_client.networks.create(
        CONTROL_NET, driver="bridge",
        ipam=docker.types.IPAMConfig(
            pool_configs=[docker.types.IPAMPool(subnet=CONTROL_SUBNET)]))
    gateway = control.attrs["IPAM"]["Config"][0]["Gateway"]

    # The proxies ask the instancer where a key leads, so it has to be listening
    # before they start -- they reach us over the control gateway.
    server = make_server("0.0.0.0", 0, instancer.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    instancer.INSTANCER_URL = "http://%s:%d" % (gateway, server.socket.getsockname()[1])

    # The real challenge directories, on ports that are ours for the run. Loaded
    # only now: a Challenge takes its image and proxy names at birth.
    challenges = instancer.load_challenges(str(CHALLENGES))
    assert set(challenges) >= {PWN, WEB}, sorted(challenges)
    for cid, port in PORTS.items():
        challenges[cid].proxy_port = port
        challenges[cid].subnet_pool = ipaddress.ip_network(POOLS[cid])
        challenges[cid].port_min, challenges[cid].port_max = PORT_MIN, PORT_MAX
    instancer.CHALLENGES = challenges

    instancer.startup()                     # builds the images, raises the proxies
    yield challenges
    server.shutdown()
    instancer.CHALLENGES = {}
    for name, value in saved.items():
        setattr(instancer, name, value)


# Set before the challenges are loaded: the name templates decide what a
# Challenge calls its image and its proxy, and it decides that once.
CONFIG = {
    "CHALLENGE_IMAGE": IMAGE,
    "PROXY_IMAGE": PROXY_IMAGE,
    "PROXY_DIR": str(REPO / "proxy-core"),
    "CONTAINER_PREFIX": PREFIX,
    "NETWORK_PREFIX": NET_PREFIX,
    "PROXY_PREFIX": PROXY_PREFIX,
    "CONTAINER_NAME": PREFIX + "{chal}-{owner}",
    "NETWORK_NAME": NET_PREFIX + "{chal}-{owner}",
    "PROXY_NAME": PROXY_PREFIX + "{chal}",
    "CONTROL_NETWORK": CONTROL_NET,
    "PROXY_TOKEN": TOKEN,
    "CHALLENGES": {},
}


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
    remove_instances(client)
    for container in client.containers.list(all=True):
        if container.name.startswith(PROXY_PREFIX):
            container.remove(force=True)
    for network in client.networks.list():
        if network.name == CONTROL_NET:
            scrub(network)


def remove_instances(client):
    for container in client.containers.list(all=True):
        if container.name.startswith(PREFIX):
            container.remove(force=True)
    for network in client.networks.list():
        if not network.name.startswith(NET_PREFIX):
            continue
        scrub(network)


def scrub(network):
    """Empty a network and take it away.

    Every step of it is best effort, because the instancer is cleaning up at the
    same time we are: the containers were force-removed a moment ago and an
    inspect taken before that still lists them, and the network itself may
    already have been pruned by the reaper. Losing a race to the thing under
    test is not a test failure.
    """
    try:
        network.reload()
        for endpoint in (network.attrs.get("Containers") or {}):
            try:
                network.disconnect(endpoint, force=True)
            except docker.errors.APIError:
                pass
        network.remove()
    except docker.errors.APIError:
        pass


# --- looking at what is there -------------------------------------------------

def port_of(container):
    """The instance's port -- a label, not something the player is handed."""
    return int(container.labels["ctf.port"])


def instances(client, chal=None):
    prefix = PREFIX + (chal + "-" if chal else "")
    return [c for c in client.containers.list() if c.name.startswith(prefix)]


def networks(client, chal=None):
    # list() answers with a summary; only inspect knows who is attached.
    prefix = NET_PREFIX + (chal + "-" if chal else "")
    found = [n for n in client.networks.list() if n.name.startswith(prefix)]
    for network in found:
        network.reload()
    return found


def proxy_of(chal):
    return instancer.client().containers.get(chal.proxy)


def address_on(container, network_name):
    container.reload()
    return container.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]


def default_routes(container):
    """The instance's default routes, read from inside it (destination 0.0.0.0)."""
    routes = container.exec_run(["cat", "/proc/net/route"]).output.decode()
    return [line for line in routes.splitlines()[1:] if line.split()[1] == "00000000"]


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


# --- talking to a proxy -------------------------------------------------------

def play(port, key, timeout=20):
    """Do what a player does in netcat mode: connect, hand over the key, read."""
    deadline = time.time() + timeout
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
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


def refused(port, key, timeout=5):
    """What the netcat proxy says to a key it will not spend."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.recv(4096)
        sock.sendall(key.encode() + b"\n")
        return sock.recv(4096)


def browse(port, path, cookie=None, timeout=25):
    """Do what a player does in http mode. Returns (status, headers, body)."""
    deadline = time.time() + timeout
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", path, headers={"Cookie": cookie} if cookie else {})
            response = connection.getresponse()
            answer = (response.status, dict(response.getheaders()),
                      response.read().decode(errors="replace"))
            connection.close()
            if answer[0] != 502:            # the instance is still coming up
                return answer
        except OSError:
            pass
        if time.time() > deadline:
            raise AssertionError("nothing came back through the proxy")
        time.sleep(0.3)


# --- the proxies the instancer raised -----------------------------------------

def test_every_challenge_got_a_proxy_of_its_own(world):
    client = instancer.client()
    for cid, chal in world.items():
        proxy = client.containers.get(chal.proxy)
        assert proxy.status == "running"
        published = proxy.attrs["NetworkSettings"]["Ports"]["%d/tcp" % PORTS[cid]]
        assert int(published[0]["HostPort"]) == PORTS[cid]
        # each binds one address of its own, on the control network and nowhere else
        assert list(proxy.attrs["NetworkSettings"]["Networks"]) == [CONTROL_NET]
        assert address_on(proxy, CONTROL_NET) in env_of(proxy)["PROXY_BIND"]


def env_of(container):
    return dict(line.split("=", 1) for line in container.attrs["Config"]["Env"]
                if "=" in line)


def test_a_proxy_is_told_only_about_its_own_challenge(world):
    for cid, chal in world.items():
        env = env_of(proxy_of(chal))
        assert env["PROXY_CHAL"] == cid
        assert env["MODE"] == chal.mode
        assert env["PROXY_TOKEN"] == instancer.proxy_token(cid)
    tokens = {env_of(proxy_of(c))["PROXY_TOKEN"] for c in world.values()}
    assert len(tokens) == len(world)


def test_a_second_startup_adopts_the_proxies_it_finds(world):
    client = instancer.client()
    before = {c.id: client.containers.get(c.proxy).id for c in world.values()}
    instancer.startup()
    after = {c.id: client.containers.get(c.proxy).id for c in world.values()}
    assert before == after          # players mid-connection are not interrupted


# --- lifecycle ----------------------------------------------------------------

def test_lifecycle(web, world):
    client = instancer.client()
    chal = world[PWN]
    assert web.post("/api/%s/destroy" % PWN).get_json()["running"] is False

    created = web.post("/api/%s/create" % PWN).get_json()
    assert created["running"] is True
    assert created["chal"] == PWN
    assert created["mode"] == "netcat"
    assert created["proxy_port"] == PORTS[PWN]
    assert 0 < created["remaining_time"] <= chal.ttl

    container = instances(client)[0]
    assert container.status == "running"
    owner = container.labels["ctf.owner"]
    assert container.name == PREFIX + "%s-%s" % (PWN, owner)
    assert container.labels["ctf.chal"] == PWN
    assert container.labels["ctf.key"] == created["key"]
    assert PORT_MIN <= port_of(container) <= PORT_MAX

    # nothing of the instance is published: the proxy is the only way in
    assert not (container.attrs["NetworkSettings"]["Ports"] or {})

    net = networks(client)[0]
    assert net.name == NET_PREFIX + "%s-%s" % (PWN, owner)
    assert net.attrs["Internal"] is True
    subnet = net.attrs["IPAM"]["Config"][0]["Subnet"]
    assert ipaddress.ip_network(subnet).subnet_of(chal.subnet_pool)
    assert container.labels["ctf.subnet"] == subnet

    # ...and this challenge's proxy is on that network, so it can dial in
    assert chal.proxy in {c["Name"] for c in net.attrs["Containers"].values()}

    # a second create does not start a second container/network
    assert web.post("/api/%s/create" % PWN).get_json()["key"] == created["key"]
    assert len(instances(client)) == 1
    assert len(networks(client)) == 1

    assert web.post("/api/%s/destroy" % PWN).get_json()["running"] is False
    assert instances(client) == []
    assert networks(client) == []          # the proxy was detached first


def test_the_key_gets_a_player_through_the_netcat_proxy(web, world):
    key = web.post("/api/%s/create" % PWN).get_json()["key"]
    assert "Special gifts" in play(PORTS[PWN], key)


def test_the_key_gets_a_player_through_the_http_proxy(web, world):
    key = web.post("/api/%s/create" % WEB).get_json()["key"]
    status, headers, body = browse(PORTS[WEB], "/%s/" % key)
    assert status == 200
    assert "the cookie jar" in body
    # the answer carries the key back as a cookie, so the challenge's own
    # absolute links keep routing
    assert "sz_key=%s" % key in headers["Set-Cookie"]


def test_the_web_challenge_is_playable_through_the_proxy(web, world):
    key = web.post("/api/%s/create" % WEB).get_json()["key"]
    cookie = "sz_key=%s" % key
    assert browse(PORTS[WEB], "/flag", cookie)[0] == 403        # a guest
    admin = "sz_key=%s; jar=eyJuYW1lIjoibmF1cCIsInJvbGUiOiJhZG1pbiJ9" % key
    status, _, body = browse(PORTS[WEB], "/flag", admin)
    assert status == 200
    assert "AIS3{" in body                                      # forged their way in


def test_another_key_gets_nobody_anywhere(web, world):
    web.post("/api/%s/create" % PWN)
    assert b"no instance" in refused(PORTS[PWN], "f" * 32)


def test_a_destroyed_instances_key_stops_working(web, world):
    key = web.post("/api/%s/create" % PWN).get_json()["key"]
    play(PORTS[PWN], key)                    # it worked a moment ago
    web.post("/api/%s/destroy" % PWN)
    assert b"no instance" in refused(PORTS[PWN], key)


# --- one instancer, many challenges -------------------------------------------

def test_one_session_holds_one_instance_of_each_challenge(web, world):
    client = instancer.client()
    pwn = web.post("/api/%s/create" % PWN).get_json()
    site = web.post("/api/%s/create" % WEB).get_json()
    assert pwn["key"] != site["key"]
    assert len(instances(client)) == 2
    assert len(networks(client)) == 2
    assert {c.labels["ctf.chal"] for c in instances(client)} == {PWN, WEB}
    assert len({c.labels["ctf.owner"] for c in instances(client)}) == 1

    # each is reached through its own challenge's proxy, and only there
    assert "Special gifts" in play(PORTS[PWN], pwn["key"])
    assert "the cookie jar" in browse(PORTS[WEB], "/%s/" % site["key"])[2]


def test_a_key_is_no_good_at_another_challenges_proxy(web, world):
    """Even the door it does not belong to cannot be talked into opening."""
    web_key = web.post("/api/%s/create" % WEB).get_json()["key"]
    web.post("/api/%s/create" % PWN)
    assert b"no instance" in refused(PORTS[PWN], web_key)


def test_each_challenge_draws_from_its_own_pool(web, world):
    client = instancer.client()
    web.post("/api/%s/create" % PWN)
    web.post("/api/%s/create" % WEB)
    for cid, chal in world.items():
        subnet = instances(client, cid)[0].labels["ctf.subnet"]
        assert ipaddress.ip_network(subnet).subnet_of(chal.subnet_pool)


def test_two_challenges_instances_cannot_reach_each_other(web, world):
    client = instancer.client()
    web.post("/api/%s/create" % PWN)
    web.post("/api/%s/create" % WEB)
    pwn = instances(client, PWN)[0]
    site = instances(client, WEB)[0]
    site_net = instancer.network_name(WEB, site.labels["ctf.owner"])
    assert not can_reach(pwn, address_on(site, site_net), port_of(site))


# --- isolation ----------------------------------------------------------------

def test_an_instance_can_reach_nothing_but_its_own_subnet(web, world):
    client = instancer.client()
    web.post("/api/%s/create" % PWN)
    instance = instances(client)[0]
    network = networks(client)[0].name
    proxy = proxy_of(world[PWN])

    # The proxy is on the instance's network -- and answers on none of it.
    assert not can_reach(instance, address_on(proxy, network), PORTS[PWN])

    # The control network, where the proxies and the instancer actually live.
    assert not can_reach(instance, address_on(proxy, CONTROL_NET), PORTS[PWN])

    # And the host: an instance with no gateway has no route off its own /24.
    assert default_routes(instance) == []


def test_the_proxy_can_reach_the_instance(web, world):
    web.post("/api/%s/create" % PWN)
    client = instancer.client()
    instance = instances(client)[0]
    network = networks(client)[0].name
    assert dial_from(proxy_of(world[PWN]), address_on(instance, network),
                     port_of(instance))


def test_a_real_crowd_never_shares_a_resource(world, monkeypatch):
    """Twelve players pressing START at once, against the real daemon."""
    monkeypatch.setattr(world[WEB], "port_max", PORT_MIN + 20)
    client = instancer.client()
    players = []
    for _ in range(12):
        browser = instancer.app.test_client()
        browser.get("/")
        players.append(browser)

    answers = []
    threads = [threading.Thread(
        target=lambda b=b: answers.append(b.post("/api/%s/create" % WEB).get_json()))
        for b in players]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(a["running"] for a in answers), answers
    running = instances(client)
    assert len(running) == 12
    assert len({port_of(c) for c in running}) == 12
    assert len({c.labels["ctf.subnet"] for c in running}) == 12
    assert len({a["key"] for a in answers}) == 12
    # and every one of them is really on a network of its own, with the proxy
    assert len(networks(client)) == 12


def test_two_sessions_are_strangers(web, world):
    client = instancer.client()
    other = instancer.app.test_client()
    other.get("/")

    mine = web.post("/api/%s/create" % WEB).get_json()
    theirs = other.post("/api/%s/create" % WEB).get_json()
    assert mine["key"] != theirs["key"]
    assert len(instances(client)) == 2
    assert len({port_of(c) for c in instances(client)}) == 2
    assert len({n.attrs["IPAM"]["Config"][0]["Subnet"] for n in networks(client)}) == 2

    # neither instance's network can carry a packet to the other
    a, b = instances(client)
    net_a = instancer.network_name(WEB, a.labels["ctf.owner"])
    assert not can_reach(b, address_on(a, net_a), port_of(a), shell="sh")


# --- reaping and failure ------------------------------------------------------

def test_reaper_destroys_expired_instance(web, world, monkeypatch):
    client = instancer.client()
    monkeypatch.setattr(world[WEB], "ttl", 1)   # the only lifetime there is
    web.post("/api/%s/create" % WEB)
    assert len(instances(client)) == 1
    time.sleep(2)
    instancer.reap_expired()
    assert instances(client) == []
    assert networks(client) == []


def test_shutdown_takes_the_instances_with_it_and_leaves_the_proxies_alone(web, world):
    """destroy_all() is for going away; the reaper is not."""
    client = instancer.client()
    web.post("/api/%s/create" % WEB)
    assert len(instances(client)) == 1
    instancer.reap_expired()
    assert len(instances(client)) == 1
    for chal in world.values():
        assert client.containers.get(chal.proxy).status == "running"


def test_create_fails_cleanly_on_bad_image(web, world, monkeypatch):
    client = instancer.client()
    monkeypatch.setattr(world[WEB], "image", "ctf-challenge-cookie-jar:does-not-exist")
    response = web.post("/api/%s/create" % WEB)
    assert response.status_code == 500
    assert response.get_json()["running"] is False
    assert instances(client) == []
    assert networks(client) == []          # network rolled back, proxy detached

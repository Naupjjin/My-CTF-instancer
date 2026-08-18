"""Integration tests: the real Docker daemon and the real challenge image.

Skipped automatically when no Docker daemon is reachable.
"""

import importlib.util
import socket
import time
from pathlib import Path

import pytest
import requests

import app as instancer

REPO = Path(__file__).resolve().parents[1]
IMAGE = "ctf-challenge:test"
PREFIX = "ctf-instance-test-"
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


def instances(client):
    return [c for c in client.containers.list() if c.name.startswith(PREFIX)]


def configure(module, monkeypatch):
    monkeypatch.setattr(module, "CHALLENGE_IMAGE", IMAGE)
    monkeypatch.setattr(module, "CONTAINER_PREFIX", PREFIX)
    monkeypatch.setattr(module, "PORT_MIN", PORT_MIN)
    monkeypatch.setattr(module, "PORT_MAX", PORT_MAX)
    monkeypatch.setattr(module.app, "secret_key", SECRET)
    client = module.app.test_client()
    client.get("/")  # like a browser: pick up a session cookie
    return client


def load_fresh_module():
    """Import a second copy of app.py: same code, empty in-process state."""
    spec = importlib.util.spec_from_file_location("app_restarted", REPO / "instancer-core" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_inside(container, timeout=15):
    """Fetch the challenge from inside the container, waiting for it to boot."""
    fetch = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/').read().decode())"
    deadline = time.time() + timeout
    while True:
        exit_code, output = container.exec_run(["python", "-c", fetch])
        if exit_code == 0:
            return output.decode()
        if time.time() > deadline:
            raise AssertionError("challenge never served on its container port: %s" % output.decode())
        time.sleep(0.2)


def bridge_reachable(container):
    """True if this host can talk to container networking at all."""
    for network in container.attrs["NetworkSettings"]["Networks"].values():
        try:
            socket.create_connection((network["IPAddress"], 8080), timeout=3).close()
            return True
        except OSError:
            continue
    return False


def wait_for_http(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return requests.get(url, timeout=1)
        except requests.RequestException:
            time.sleep(0.2)
    raise AssertionError("challenge did not answer on %s" % url)


def test_lifecycle(real_client, monkeypatch):
    web = configure(instancer, monkeypatch)

    # destroy with nothing running must not crash
    assert web.post("/destroy").get_json() == {"running": False}
    assert web.get("/status").get_json() == {"running": False}

    # create
    created = web.post("/create").get_json()
    assert created["running"] is True
    assert PORT_MIN <= created["port"] <= PORT_MAX
    port = created["port"]

    # the container really runs, mapped to the port we advertised
    container = instances(real_client)[0]
    assert container.status == "running"
    bindings = container.attrs["NetworkSettings"]["Ports"]["8080/tcp"]
    assert str(port) in [b["HostPort"] for b in bindings]

    # the challenge itself is serving on the container port
    assert "flag{" in fetch_inside(container)

    # status agrees
    assert web.get("/status").get_json() == {"running": True, "port": port}

    # a second create does not start a second container
    assert web.post("/create").get_json() == {"running": True, "port": port}
    assert len(instances(real_client)) == 1

    # a restarted instancer process adopts the same container for the same session
    restarted = load_fresh_module()
    restarted_web = configure(restarted, monkeypatch)
    restarted_web.set_cookie("session", web.get_cookie("session").value)
    assert restarted_web.get("/status").get_json() == {"running": True, "port": port}
    assert restarted_web.post("/create").get_json() == {"running": True, "port": port}
    assert len(instances(real_client)) == 1

    # destroy
    assert web.post("/destroy").get_json() == {"running": False}
    assert instances(real_client) == []
    assert web.get("/status").get_json() == {"running": False}

    # destroy again is still fine
    assert web.post("/destroy").get_json() == {"running": False}


def test_two_sessions_get_two_containers(real_client, monkeypatch):
    mine = configure(instancer, monkeypatch)
    theirs = instancer.app.test_client()
    theirs.get("/")

    my_instance = mine.post("/create").get_json()
    their_instance = theirs.post("/create").get_json()
    try:
        assert my_instance["port"] != their_instance["port"]
        assert len(instances(real_client)) == 2

        # neither session sees or can stop the other's container
        assert mine.get("/status").get_json() == my_instance
        assert theirs.get("/status").get_json() == their_instance
        assert theirs.post("/destroy").get_json() == {"running": False}
        assert mine.get("/status").get_json() == my_instance
        assert len(instances(real_client)) == 1
    finally:
        mine.post("/destroy")
        theirs.post("/destroy")
    assert instances(real_client) == []


def test_create_fails_cleanly_on_bad_image(real_client, monkeypatch):
    web = configure(instancer, monkeypatch)
    monkeypatch.setattr(instancer, "CHALLENGE_IMAGE", "ctf-challenge:does-not-exist")

    response = web.post("/create")
    assert response.status_code == 500
    assert response.get_json()["running"] is False
    assert instances(real_client) == []
    assert web.get("/status").get_json() == {"running": False}


def test_challenge_reachable_on_host_port(real_client, monkeypatch):
    """The published host port must actually serve the challenge.

    Some hosts block traffic between the host and the docker bridge; there the
    mapping is all the instancer can be held responsible for, so we skip loudly
    instead of reporting a pass.
    """
    web = configure(instancer, monkeypatch)
    port = web.post("/create").get_json()["port"]
    try:
        try:
            response = wait_for_http("http://127.0.0.1:%d/" % port, timeout=10)
        except AssertionError:
            if bridge_reachable(instances(real_client)[0]):
                raise
            pytest.skip("host cannot reach docker container networking (port mapping verified instead)")
        assert response.status_code == 200
        assert "flag{" in response.text
    finally:
        web.post("/destroy")

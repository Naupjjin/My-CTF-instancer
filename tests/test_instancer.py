"""Unit tests: the instancer logic against a fake Docker daemon."""

import socket
import threading
import time

import pytest
from docker.errors import APIError, NotFound

import app as instancer


class FakeContainer:
    def __init__(self, daemon, name, ports):
        self.daemon = daemon
        self.name = name
        self.ports = ports
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

    def stop(self, timeout=None):
        self.status = "exited"

    def remove(self, force=False):
        self.daemon.containers.pop(self.name, None)


class FakeContainerCollection:
    def __init__(self, daemon):
        self.daemon = daemon

    def get(self, name):
        if name not in self.daemon.containers:
            raise NotFound(name)
        return self.daemon.containers[name]

    def create(self, image, name=None, ports=None, **kwargs):
        self.daemon.create_calls += 1
        time.sleep(self.daemon.create_delay)
        if name in self.daemon.containers:
            raise APIError("name conflict")
        container = FakeContainer(self.daemon, name, ports or {})
        self.daemon.containers[name] = container
        return container

    def list(self):
        return [c for c in self.daemon.containers.values() if c.status == "running"]


class FakeImageCollection:
    def __init__(self, daemon):
        self.daemon = daemon

    def build(self, path=None, tag=None, rm=False):
        self.daemon.builds.append((path, tag))
        return object(), []


class FakeDaemon:
    def __init__(self):
        self.containers_collection = FakeContainerCollection(self)
        self.images_collection = FakeImageCollection(self)
        self.containers = {}
        self.builds = []
        self.create_calls = 0
        self.create_delay = 0
        self.fail_start = False


class FakeClient:
    def __init__(self, daemon):
        self.containers = daemon.containers_collection
        self.images = daemon.images_collection


@pytest.fixture
def daemon(monkeypatch):
    fake = FakeDaemon()
    monkeypatch.setattr(instancer, "_client", FakeClient(fake))
    monkeypatch.setattr(instancer, "PORT_MIN", 31000)
    monkeypatch.setattr(instancer, "PORT_MAX", 31010)
    monkeypatch.setattr(instancer.app, "secret_key", "test-secret")
    return fake


@pytest.fixture
def web(daemon):
    """One user: a Flask test client keeps its session cookie between calls."""
    return instancer.app.test_client()


@pytest.fixture
def other(daemon):
    """A second user, with a session of its own."""
    return instancer.app.test_client()


def only_container(daemon):
    assert len(daemon.containers) == 1
    return next(iter(daemon.containers.values()))


# 1 + 2: the app serves the UI
def test_index_serves_ui(web):
    response = web.get("/")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "CTF Instancer" in body
    assert "Start Instance" in body
    assert "Stop Instance" in body


# 3: no instance yet
def test_status_without_instance(web):
    assert web.get("/status").get_json() == {"running": False}


# 4 + 5: create makes one container named after the session, status reports it
def test_create_starts_container(web, daemon):
    data = web.post("/create").get_json()
    assert data["running"] is True
    assert instancer.PORT_MIN <= data["port"] <= instancer.PORT_MAX
    assert daemon.create_calls == 1

    container = only_container(daemon)
    assert container.name.startswith(instancer.CONTAINER_PREFIX)
    assert container.status == "running"
    assert container.ports == {"8080/tcp": data["port"]}
    assert web.get("/status").get_json() == {"running": True, "port": data["port"]}


# 6: the same session creating twice keeps one container
def test_create_is_idempotent_within_a_session(web, daemon):
    first = web.post("/create").get_json()
    second = web.post("/create").get_json()
    assert first == second
    assert daemon.create_calls == 1
    assert len(daemon.containers) == 1


# 7: destroy removes the container
def test_destroy_removes_container(web, daemon):
    web.post("/create")
    assert web.post("/destroy").get_json() == {"running": False}
    assert daemon.containers == {}
    assert web.get("/status").get_json() == {"running": False}


# 8: destroy without a container is a no-op, not a crash
def test_destroy_without_container(web):
    assert web.post("/destroy").status_code == 200
    assert web.post("/destroy").get_json() == {"running": False}


# 9: a failed start leaves nothing behind, and the port stays usable
def test_failed_create_cleans_up(web, daemon):
    daemon.fail_start = True
    response = web.post("/create")
    assert response.status_code == 500
    assert response.get_json()["running"] is False
    assert daemon.containers == {}
    assert web.get("/status").get_json() == {"running": False}

    daemon.fail_start = False
    retry = web.post("/create").get_json()
    assert retry == {"running": True, "port": instancer.PORT_MIN}


# 10: one session clicking twice at once still gets one container
def test_concurrent_create_in_one_session(daemon):
    daemon.create_delay = 0.2
    web = instancer.app.test_client()
    web.get("/")  # a browser loads the page before clicking
    results = []

    def worker():
        results.append(web.post("/create").get_json())

    run_together([worker, worker])

    assert daemon.create_calls == 1
    assert len(daemon.containers) == 1
    assert results[0] == results[1]


def run_together(workers):
    threads = [threading.Thread(target=worker) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# 11: a session that outlived the process is adopted, not duplicated
def test_existing_container_is_adopted(web, daemon):
    created = web.post("/create").get_json()
    restarted = instancer.app.test_client()
    restarted.set_cookie("session", web.get_cookie("session").value)

    assert restarted.get("/status").get_json() == created
    assert restarted.post("/create").get_json() == created
    assert daemon.create_calls == 1


def test_stale_container_is_cleaned_up(web, daemon):
    web.post("/create")
    only_container(daemon).status = "exited"
    assert web.get("/status").get_json() == {"running": False}
    assert daemon.containers == {}


# 12: ports come from the configured range only
def test_ports_outside_range_are_never_used(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "PORT_MIN", 31020)
    monkeypatch.setattr(instancer, "PORT_MAX", 31021)
    port = web.post("/create").get_json()["port"]
    assert port in (31020, 31021)


def test_create_fails_when_range_is_exhausted(web, daemon, monkeypatch):
    monkeypatch.setattr(instancer, "PORT_MIN", 31030)
    monkeypatch.setattr(instancer, "PORT_MAX", 31030)
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 31030))
    blocker.listen(1)
    try:
        response = web.post("/create")
        assert response.status_code == 500
        assert "no free host port" in response.get_json()["error"]
        assert daemon.create_calls == 0
        assert daemon.containers == {}
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
    other = FakeContainer(daemon, "someone-else", {"9999/tcp": instancer.PORT_MIN})
    daemon.containers["someone-else"] = other
    other.start()
    assert instancer.pick_port() == instancer.PORT_MIN + 1


def test_startup_builds_image(daemon):
    instancer.startup()
    assert daemon.builds == [(instancer.CHALLENGE_DIR, instancer.CHALLENGE_IMAGE)]


# --- one instance per session -------------------------------------------------

def test_each_session_gets_its_own_instance(web, other, daemon):
    mine = web.post("/create").get_json()
    theirs = other.post("/create").get_json()

    assert mine["port"] != theirs["port"]
    assert len(daemon.containers) == 2
    assert sorted(daemon.containers) != [instancer.CONTAINER_PREFIX] * 2  # distinct names


def test_a_session_only_sees_its_own_instance(web, other):
    mine = web.post("/create").get_json()
    assert other.get("/status").get_json() == {"running": False}
    assert web.get("/status").get_json() == mine


def test_a_session_cannot_destroy_someone_elses_instance(web, other, daemon):
    mine = web.post("/create").get_json()

    assert other.post("/destroy").get_json() == {"running": False}
    assert web.get("/status").get_json() == mine
    assert len(daemon.containers) == 1

    theirs = other.post("/create").get_json()
    assert other.post("/destroy").get_json() == {"running": False}
    assert web.get("/status").get_json() == mine
    assert theirs["port"] != mine["port"]


def test_concurrent_create_by_two_sessions(daemon):
    daemon.create_delay = 0.2
    clients = [instancer.app.test_client() for _ in range(2)]
    results = []

    run_together([lambda c=c: results.append(c.post("/create").get_json()) for c in clients])

    assert daemon.create_calls == 2
    assert len(daemon.containers) == 2
    assert results[0]["port"] != results[1]["port"]


def test_session_comes_from_the_page_not_from_polling(web):
    web.get("/status")
    assert web.get_cookie("session") is None
    web.get("/")
    assert web.get_cookie("session") is not None

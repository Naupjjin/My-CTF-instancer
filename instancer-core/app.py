"""Minimal CTF instancer.

One challenge, one instance per browser session. All state lives in Docker, so
a restart of this process re-discovers the containers instead of duplicating
them.
"""

import logging
import os
import secrets
import socket
import threading

import docker
from docker.errors import DockerException, NotFound
from flask import Flask, jsonify, render_template, session

PORT_MIN = int(os.environ.get("PORT_MIN", "30000"))
PORT_MAX = int(os.environ.get("PORT_MAX", "30100"))
CHALLENGE_DIR = os.environ.get("CHALLENGE_DIR", "/challenge")
CHALLENGE_IMAGE = os.environ.get("CHALLENGE_IMAGE", "ctf-challenge:latest")
CONTAINER_PORT = int(os.environ.get("CONTAINER_PORT", "8080"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "5000"))
SECRET_KEY = os.environ.get("SECRET_KEY")
CONTAINER_PREFIX = "ctf-instance-"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("instancer")

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


def get_instance(owner):
    """Return this session's running container, or None.

    A leftover stopped container is removed so its name and port are free again.
    """
    try:
        container = client().containers.get(container_name(owner))
    except NotFound:
        return None
    if container.status != "running":
        log.info("removing stale container of %s (status=%s)", owner, container.status)
        container.remove(force=True)
        return None
    return container


def instance_port(container):
    bindings = container.attrs["NetworkSettings"]["Ports"].get("%d/tcp" % CONTAINER_PORT)
    if not bindings:
        return None
    return int(bindings[0]["HostPort"])


def docker_used_ports():
    """Host ports published by any running container."""
    used = set()
    for container in client().containers.list():
        ports = container.attrs["NetworkSettings"]["Ports"] or {}
        for bindings in ports.values():
            for binding in bindings or []:
                used.add(int(binding["HostPort"]))
    return used


def port_is_free(port):
    """True if nothing on this host is bound to the port right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def pick_port():
    used = docker_used_ports()
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port not in used and port_is_free(port):
            return port
    raise RuntimeError("no free host port in %d-%d" % (PORT_MIN, PORT_MAX))


def build_image():
    log.info("building challenge image %s from %s", CHALLENGE_IMAGE, CHALLENGE_DIR)
    client().images.build(path=CHALLENGE_DIR, tag=CHALLENGE_IMAGE, rm=True)
    log.info("challenge image built: %s", CHALLENGE_IMAGE)


@app.get("/")
def index():
    # Mint the session here, so two fast clicks on Start cannot race each other
    # into two identities (and therefore two containers).
    owner_id()
    return render_template("index.html")


@app.get("/status")
def status():
    owner = session.get("owner")
    container = get_instance(owner) if owner else None
    if container is None:
        return jsonify(running=False)
    return jsonify(running=True, port=instance_port(container))


@app.post("/create")
def create():
    owner = owner_id()
    with lock:
        container = get_instance(owner)
        if container is not None:
            return jsonify(running=True, port=instance_port(container))

        container = None
        try:
            port = pick_port()
            container = client().containers.create(
                CHALLENGE_IMAGE,
                name=container_name(owner),
                ports={"%d/tcp" % CONTAINER_PORT: port},
            )
            container.start()
            container.reload()
        except Exception as exc:
            log.error("create failed for %s: %s", owner, exc)
            if container is not None:
                try:
                    container.remove(force=True)
                except (DockerException, OSError) as cleanup_exc:
                    log.error("cleanup after failed create: %s", cleanup_exc)
            return jsonify(running=False, error=str(exc)), 500

        log.info("instance created for %s: host %d -> container %d", owner, port, CONTAINER_PORT)
        return jsonify(running=True, port=instance_port(container))


@app.post("/destroy")
def destroy():
    owner = session.get("owner")
    if owner is None:
        return jsonify(running=False)
    with lock:
        try:
            container = client().containers.get(container_name(owner))
        except NotFound:
            return jsonify(running=False)
        try:
            container.stop(timeout=5)
            container.remove(force=True)
        except NotFound:
            pass
        except Exception as exc:
            log.error("destroy failed for %s: %s", owner, exc)
            return jsonify(running=True, error=str(exc)), 500
        log.info("instance destroyed for %s", owner)
        return jsonify(running=False)


def startup():
    if not SECRET_KEY:
        log.warning("SECRET_KEY not set: sessions, and with them instance ownership, "
                    "are lost when the instancer restarts")
    build_image()
    existing = [c.name for c in client().containers.list() if c.name.startswith(CONTAINER_PREFIX)]
    if existing:
        log.info("adopted %d existing instance(s): %s", len(existing), ", ".join(existing))
    log.info("server started on port %d (host ports %d-%d)", LISTEN_PORT, PORT_MIN, PORT_MAX)


if __name__ == "__main__":
    startup()
    app.run(host="0.0.0.0", port=LISTEN_PORT)

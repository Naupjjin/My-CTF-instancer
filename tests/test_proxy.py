"""Unit tests: the proxy against fake instances (no Docker, no instancer)."""

import io
import socket
import socketserver
import threading
import urllib.error

import pytest

import proxy

KEY = "a1b2c3d4" * 4
OTHER_KEY = "f" * 32


# --- fake instances -----------------------------------------------------------

class Echo(socketserver.BaseRequestHandler):
    """Stands in for a pwn service: a banner, then a parrot."""

    def handle(self):
        self.request.sendall(b"banner\n")
        while True:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            self.request.sendall(chunk)


class Web(socketserver.BaseRequestHandler):
    """Stands in for a web challenge: answers with the request it was handed."""

    def handle(self):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                             b"Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                             % (len(head), head))


class Listener(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(server):
    # A short poll interval: shutdown() waits for it, once per test.
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    return server


@pytest.fixture
def instance(request):
    """A fake instance on loopback; the marker picks what it pretends to be."""
    handler = getattr(request, "param", Echo)
    server = serve(Listener(("127.0.0.1", 0), handler))
    yield server.server_address
    server.shutdown()


@pytest.fixture
def front(monkeypatch, instance):
    """The real proxy, with key lookup answered locally instead of by the instancer."""
    monkeypatch.setattr(proxy, "resolve",
                        lambda key: instance if key == KEY else None)
    monkeypatch.setattr(proxy, "IDLE_TIMEOUT", 5)
    server = serve(proxy.serve(bind="127.0.0.1", port=0))
    yield server.server_address
    server.shutdown()


def connect(address):
    sock = socket.create_connection(address, timeout=5)
    sock.settimeout(5)
    return sock


def read_all(sock):
    chunks = []
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


# --- key lookup ---------------------------------------------------------------

class FakeAnswer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_resolve_asks_the_instancer_with_the_shared_token(monkeypatch):
    seen = {}

    def fake_urlopen(lookup, timeout=None):
        seen["url"] = lookup.full_url
        seen["token"] = lookup.get_header("X-proxy-token")
        return FakeAnswer(b'{"host": "10.240.0.1", "port": 30000}')

    monkeypatch.setattr(proxy, "PROXY_TOKEN", "shared")
    monkeypatch.setattr(proxy.urllib.request, "urlopen", fake_urlopen)
    assert proxy.resolve(KEY) == ("10.240.0.1", 30000)
    assert seen["url"].endswith("/internal/route/" + KEY)
    assert seen["token"] == "shared"


def test_resolve_of_an_unknown_key_is_none(monkeypatch):
    def refuse(lookup, timeout=None):
        raise urllib.error.HTTPError(lookup.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(proxy.urllib.request, "urlopen", refuse)
    assert proxy.resolve(KEY) is None


def test_resolve_never_forwards_a_key_that_is_not_one(monkeypatch):
    def boom(lookup, timeout=None):
        raise AssertionError("asked the instancer about %r" % lookup.full_url)

    monkeypatch.setattr(proxy.urllib.request, "urlopen", boom)
    assert proxy.resolve("../../internal/route") is None
    assert proxy.resolve("") is None
    assert proxy.resolve(None) is None


def test_resolve_survives_an_unreachable_instancer(monkeypatch):
    def down(lookup, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(proxy.urllib.request, "urlopen", down)
    assert proxy.resolve(KEY) is None


# --- netcat mode --------------------------------------------------------------

@pytest.fixture
def netcat(monkeypatch):
    monkeypatch.setattr(proxy, "MODE", "netcat")


def test_netcat_key_reaches_the_instance(front, netcat):
    with connect(front) as sock:
        assert b"key: " in sock.recv(4096)
        sock.sendall(KEY.encode() + b"\n")
        assert sock.recv(4096) == b"banner\n"
        sock.sendall(b"ping\n")
        assert sock.recv(4096) == b"ping\n"


def test_netcat_takes_a_piped_key(front, netcat):
    # `(echo $KEY; cat) | nc proxy port` sends the key and the first input together
    with connect(front) as sock:
        sock.sendall(KEY.encode() + b"\nping\n")
        sock.shutdown(socket.SHUT_WR)
        assert b"ping\n" in read_all(sock)


def test_netcat_wrong_key_gets_nothing(front, netcat):
    with connect(front) as sock:
        sock.sendall(OTHER_KEY.encode() + b"\n")
        answer = read_all(sock)
    assert b"no instance" in answer
    assert b"banner" not in answer


def test_netcat_junk_instead_of_a_key_gets_nothing(front, netcat):
    with connect(front) as sock:
        sock.sendall(b"give me a shell\n")
        assert b"no instance" in read_all(sock)


def test_netcat_hangs_up_when_the_instance_does(front, netcat):
    # Both ends of the splice must see the hangup: the player closing their side
    # ends the instance, and the instance ending closes the player's connection.
    with connect(front) as sock:
        assert b"key: " in sock.recv(4096)
        sock.sendall(KEY.encode() + b"\n")
        assert sock.recv(4096) == b"banner\n"
        sock.shutdown(socket.SHUT_WR)
        assert read_all(sock) == b""


# --- http mode ----------------------------------------------------------------

@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(proxy, "MODE", "http")


def get(front, path, headers=b""):
    with connect(front) as sock:
        sock.sendall(b"GET " + path + b" HTTP/1.1\r\nHost: proxy\r\n" + headers + b"\r\n")
        return read_all(sock)


@pytest.mark.parametrize("instance", [Web], indirect=True)
def test_http_path_prefix_routes_and_is_stripped(front, http):
    answer = get(front, b"/" + KEY.encode() + b"/login")
    assert answer.startswith(b"HTTP/1.1 200 OK")
    assert b"GET /login HTTP/1.1" in answer          # the key never reaches the challenge
    assert KEY.encode() not in answer.split(b"\r\n\r\n", 1)[1]


@pytest.mark.parametrize("instance", [Web], indirect=True)
def test_http_bare_key_becomes_the_root(front, http):
    assert b"GET / HTTP/1.1" in get(front, b"/" + KEY.encode())


@pytest.mark.parametrize("instance", [Web], indirect=True)
def test_http_keeps_the_query_string(front, http):
    assert b"GET /?id=1 HTTP/1.1" in get(front, b"/" + KEY.encode() + b"?id=1")
    assert b"GET /p?id=1 HTTP/1.1" in get(front, b"/" + KEY.encode() + b"/p?id=1")


@pytest.mark.parametrize("instance", [Web], indirect=True)
def test_http_sets_a_cookie_so_absolute_links_keep_working(front, http):
    answer = get(front, b"/" + KEY.encode() + b"/")
    assert b"Set-Cookie: ctf_key=" + KEY.encode() in answer

    # ...and that cookie alone routes the challenge's own absolute paths
    followed = get(front, b"/static/app.js",
                   b"Cookie: ctf_key=" + KEY.encode() + b"\r\n")
    assert b"GET /static/app.js HTTP/1.1" in followed
    assert b"Set-Cookie" not in followed                 # nothing new to remember


@pytest.mark.parametrize("instance", [Web], indirect=True)
def test_http_forces_one_exchange_per_connection(front, http):
    # Keep-alive would let a second request ride in on the first request's key.
    answer = get(front, b"/" + KEY.encode() + b"/", b"Connection: keep-alive\r\n")
    forwarded = answer.split(b"\r\n\r\n", 1)[1]
    assert b"Connection: close" in forwarded
    assert b"keep-alive" not in forwarded.lower()


@pytest.mark.parametrize("instance", [Web], indirect=True)
def test_http_without_a_key_goes_nowhere(front, http):
    assert get(front, b"/login").startswith(b"HTTP/1.1 404")


@pytest.mark.parametrize("instance", [Web], indirect=True)
def test_http_with_someone_elses_key_goes_nowhere(front, http):
    assert get(front, b"/" + OTHER_KEY.encode() + b"/").startswith(b"HTTP/1.1 404")
    assert get(front, b"/x", b"Cookie: ctf_key=" + OTHER_KEY.encode()
               + b"\r\n").startswith(b"HTTP/1.1 404")


def test_http_says_502_when_the_instance_is_down(front, http, monkeypatch, instance):
    monkeypatch.setattr(proxy, "resolve", lambda key: ("127.0.0.1", 1))
    assert get(front, b"/" + KEY.encode() + b"/").startswith(b"HTTP/1.1 502")


# --- parsing ------------------------------------------------------------------

def test_split_key():
    assert proxy.split_key("/" + KEY + "/a/b") == (KEY, "/a/b")
    assert proxy.split_key("/" + KEY) == (KEY, "/")
    assert proxy.split_key("/" + KEY + "/") == (KEY, "/")
    assert proxy.split_key("/login") == (None, "/login")
    assert proxy.split_key("/") == (None, "/")


def test_cookie_key_ignores_other_cookies():
    head = b"GET / HTTP/1.1\r\nCookie: theme=dark; ctf_key=%s\r\n\r\n" % KEY.encode()
    assert proxy.cookie_key(head) == KEY
    assert proxy.cookie_key(b"GET / HTTP/1.1\r\nCookie: theme=dark\r\n\r\n") is None
    assert proxy.cookie_key(b"GET / HTTP/1.1\r\nCookie: ctf_key=nope\r\n\r\n") is None


def test_read_until_returns_what_came_after_the_terminator():
    left, right = socket.socketpair()
    left.sendall(b"first\nsecond")
    left.close()
    assert proxy.read_until(right, b"\n", 4096) == (b"first\n", b"second")
    right.close()


def test_read_until_gives_up_on_an_oversized_head():
    left, right = socket.socketpair()
    threading.Thread(target=lambda: (left.sendall(b"x" * 8192), left.close()),
                     daemon=True).start()
    head, rest = proxy.read_until(right, b"\r\n\r\n", 1024)
    assert not head.endswith(b"\r\n\r\n")
    right.close()

"""SpawnZero's proxy: the only door into an instance.

Instances live on internal networks with no gateway, so nothing can route to
them -- except this proxy, which the instancer attaches to every instance
network as it creates one. A player arrives on the single published proxy port,
hands over the key the instancer printed, and is spliced onto their own
instance:

  netcat  the first line of the connection is the key
  http    the first path segment is the key, and a cookie remembers it so the
          challenge's own absolute links keep working afterwards

The proxy stores nothing: every key is resolved by asking the instancer, which
answers from the container labels. And it binds exactly one address -- the one
on the control network -- so the instance networks it is attached to have
nothing to connect back to.
"""

import http.cookies
import json
import logging
import os
import re
import socket
import socketserver
import threading
import urllib.error
import urllib.request

# The one address players reach. Binding the control-network address rather than
# 0.0.0.0 is what stops an instance from talking to the proxy: a wildcard socket
# would also answer on every instance network we get attached to later.
PROXY_BIND = os.environ.get("PROXY_BIND", "0.0.0.0")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "1337"))

# Where keys are resolved. The instancer is the only source of truth.
INSTANCER_URL = os.environ.get("INSTANCER_URL", "http://instancer:5000").rstrip("/")
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")

CONNECT_TIMEOUT = int(os.environ.get("CONNECT_TIMEOUT", "5"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))

# Keys are hex out of secrets.token_hex; anything else is not worth a lookup.
KEY_RE = re.compile(r"\A[0-9a-f]{8,128}\Z")
COOKIE_NAME = "sz_key"

BUFSIZE = 65536
MAX_HEAD = 65536      # a request head larger than this is not a challenge request
MAX_KEY_LINE = 256

MODE = os.environ.get("MODE", "http").lower()
if MODE not in ("http", "netcat"):
    logging.getLogger("proxy").warning("unknown MODE %r, falling back to http", MODE)
    MODE = "http"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")


# --- key -> instance ----------------------------------------------------------

def resolve(key):
    """Ask the instancer where a key leads. Returns (host, port), or None."""
    if not KEY_RE.match(key or ""):
        return None
    lookup = urllib.request.Request("%s/internal/route/%s" % (INSTANCER_URL, key),
                                    headers={"X-Proxy-Token": PROXY_TOKEN})
    try:
        with urllib.request.urlopen(lookup, timeout=CONNECT_TIMEOUT) as response:
            route = json.load(response)
    except urllib.error.HTTPError:
        return None                       # unknown key, or a token we got wrong
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.error("instancer lookup failed: %s", exc)
        return None
    try:
        return route["host"], int(route["port"])
    except (KeyError, TypeError, ValueError):
        log.error("instancer answered with %r", route)
        return None


def dial(route):
    """Open a connection to an instance."""
    return socket.create_connection(route, timeout=CONNECT_TIMEOUT)


# --- plumbing -----------------------------------------------------------------

def pump(src, dst):
    """Copy one direction until it dries up, then half-close the far end."""
    try:
        while True:
            chunk = src.recv(BUFSIZE)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def splice(client, upstream):
    """Join player and instance. Returns once the instance is done talking.

    The player's direction runs in a daemon thread: a client that never closes
    its end must not keep the connection -- and the thread -- alive after the
    instance has gone. Closing the sockets on the way out unblocks it.
    """
    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
    pump(upstream, client)


def read_until(sock, terminator, limit):
    """Read until the terminator (returning what came after it), or until EOF.

    Returns (before_including_terminator, leftover). If the terminator never
    arrives the whole read lands in the first element, so callers can decide
    whether that is a truncated request or just a client that hung up.
    """
    buf = b""
    while len(buf) < limit:
        index = buf.find(terminator)
        if index >= 0:
            cut = index + len(terminator)
            return buf[:cut], buf[cut:]
        chunk = sock.recv(BUFSIZE)
        if not chunk:
            break
        buf += chunk
    index = buf.find(terminator)
    if index >= 0:
        cut = index + len(terminator)
        return buf[:cut], buf[cut:]
    return buf, b""


# --- netcat mode --------------------------------------------------------------

PROMPT = b"spawnzero // paste the key your instance was created with\nkey: "
NO_ROUTE = b"no instance for that key\n"


def handle_netcat(client):
    """First line is the key; everything after it belongs to the instance."""
    client.sendall(PROMPT)
    line, rest = read_until(client, b"\n", MAX_KEY_LINE)
    route = resolve(line.strip().decode("ascii", "ignore"))
    if route is None:
        client.sendall(NO_ROUTE)
        return
    with dial(route) as upstream:
        if rest:
            upstream.sendall(rest)        # a piped `(echo key; cat)` loses nothing
        splice(client, upstream)


# --- http mode ----------------------------------------------------------------

def split_key(path):
    """Pull a key off the front of the path.

    `/<key>/rest` and `/<key>` route to `/rest` and `/`; anything else is left
    alone for the cookie to answer for.
    """
    head, slash, rest = path.lstrip("/").partition("/")
    key, mark, query = head.partition("?")
    if not KEY_RE.match(key):
        return None, path
    return key, "/" + rest if slash else "/" + mark + query


def cookie_key(head):
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() != b"cookie":
            continue
        jar = http.cookies.SimpleCookie()
        jar.load(value.decode("latin-1", "replace"))
        if COOKIE_NAME in jar and KEY_RE.match(jar[COOKIE_NAME].value):
            return jar[COOKIE_NAME].value
    return None


def rewrite_request(head, path):
    """Point the request at `path` and make it a single, closing exchange.

    Keep-alive would leave a second request on the same socket routed by the
    first one's key, so every exchange gets its own connection.
    """
    lines = head.split(b"\r\n")
    method, _, tail = lines[0].partition(b" ")
    _, _, version = tail.rpartition(b" ")
    rewritten = [b" ".join((method, path.encode("latin-1"), version))]
    rewritten += [line for line in lines[1:]
                  if not line.split(b":")[0].strip().lower()
                  in (b"connection", b"keep-alive", b"proxy-connection")]
    return b"\r\n".join(rewritten[:1] + [b"Connection: close"] + rewritten[1:])


def with_cookie(head, key):
    """Remember the key, so the challenge's absolute links keep routing."""
    status, _, rest = head.partition(b"\r\n")
    cookie = ("Set-Cookie: %s=%s; Path=/; HttpOnly; SameSite=Lax"
              % (COOKIE_NAME, key)).encode("latin-1")
    return b"\r\n".join((status, cookie, rest))


def http_error(status, message):
    body = message.encode("utf-8")
    return b"".join((
        b"HTTP/1.1 " + status.encode("ascii") + b"\r\n",
        b"Content-Type: text/plain; charset=utf-8\r\n",
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n",
        b"Connection: close\r\n\r\n", body))


NO_KEY = http_error("404 Not Found",
                    "No instance here. Open http://<this host>:<this port>/<key>/ "
                    "with the key your instance was created with.\n")
BAD_GATEWAY = http_error("502 Bad Gateway", "The instance did not answer.\n")


def handle_http(client):
    """Route one request by its key, then get out of the way."""
    head, body = read_until(client, b"\r\n\r\n", MAX_HEAD)
    if not head.endswith(b"\r\n\r\n"):
        return                            # truncated or not HTTP at all
    request_line = head.split(b"\r\n", 1)[0].split(b" ")
    if len(request_line) < 3:
        client.sendall(NO_KEY)
        return

    path = request_line[1].decode("latin-1")
    key, path = split_key(path)
    from_path = key is not None
    if key is None:
        key = cookie_key(head)
    route = resolve(key) if key else None
    if route is None:
        client.sendall(NO_KEY)
        return

    try:
        upstream = dial(route)
    except OSError as exc:
        log.warning("instance %s:%d did not answer: %s", route[0], route[1], exc)
        client.sendall(BAD_GATEWAY)
        return

    with upstream:
        upstream.sendall(rewrite_request(head, path) + body)
        response, rest = read_until(upstream, b"\r\n\r\n", MAX_HEAD)
        if from_path and response.endswith(b"\r\n\r\n"):
            response = with_cookie(response, key)
        client.sendall(response + rest)
        splice(client, upstream)


# --- server -------------------------------------------------------------------

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(IDLE_TIMEOUT)
        try:
            (handle_http if MODE == "http" else handle_netcat)(self.request)
        except OSError as exc:
            log.debug("connection from %s ended: %s", self.client_address[0], exc)


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(bind=None, port=None):
    """Bind the one address we answer on. Never 0.0.0.0 in production."""
    return Proxy((bind or PROXY_BIND, PROXY_PORT if port is None else port), Handler)


def startup():
    if not PROXY_TOKEN:
        log.warning("PROXY_TOKEN not set: the instancer will refuse every lookup")
    if PROXY_BIND in ("", "0.0.0.0", "::"):
        log.warning("PROXY_BIND is a wildcard: instances attached to this proxy can "
                    "reach it back -- bind the control network address instead")


if __name__ == "__main__":
    startup()
    server = serve()
    log.info("proxy started on %s:%d (mode=%s, instancer %s)",
             PROXY_BIND, PROXY_PORT, MODE, INSTANCER_URL)
    server.serve_forever()

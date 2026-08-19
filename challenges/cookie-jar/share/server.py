"""Cookie Jar: a web challenge in the standard library and nothing else.

The whole of it is one idea. Who you are is kept in a cookie, the cookie is
JSON in base64, and nothing signs it -- so the role the server trusts is a
field the player can rewrite. Bake your own admin cookie, read the flag.

Which port to listen on is the instancer's call: it hands every instance one of
its own and passes it in as CHAL_PORT.
"""

import base64
import http.cookies
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote_plus

PORT = int(os.environ.get("CHAL_PORT", "8080"))
FLAG = open(os.path.join(os.path.dirname(__file__), "flag.txt")).read().strip()

COOKIE = "jar"
NAME_RE = re.compile(r"\A[\w .-]{1,32}\Z")
MAX_FORM = 4096


# --- the recipe ---------------------------------------------------------------

def bake(claims):
    """A cookie is claims in JSON, in base64. That is the entire recipe."""
    raw = json.dumps(claims, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def taste(cookie):
    """Read a cookie back. A crumbled one is nobody, not an error."""
    try:
        claims = json.loads(base64.urlsafe_b64decode(cookie + "=" * (-len(cookie) % 4)))
    except (ValueError, TypeError):
        return None
    return claims if isinstance(claims, dict) else None


def escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# --- the page -----------------------------------------------------------------

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Cookie Jar</title>
<style>
  body {{ background:#e9e7f1; color:#4b485e; font-family:ui-monospace,monospace;
          display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  main {{ background:#fcfbff; border:3px solid #4b485e; box-shadow:8px 8px 0 #dad4f0;
          padding:26px 30px; width:min(520px,92vw); }}
  h1 {{ margin:0 0 4px; font-size:20px; letter-spacing:1px; }}
  p.sub {{ margin:0 0 20px; color:#918fa6; }}
  code {{ background:#f1eff8; padding:1px 5px; word-break:break-all; }}
  a {{ color:#8a7fc7; }}
  input, button {{ font:inherit; border:3px solid #4b485e; background:#fcfbff;
                   color:#4b485e; padding:7px 10px; }}
  button {{ cursor:pointer; }}
  button:hover {{ background:#8a7fc7; color:#fcfbff; }}
  .flag {{ color:#6fb39a; }}
  .no {{ color:#d98c9b; }}
</style>
<main>
  <h1>the cookie jar</h1>
  <p class="sub">members only. guests get biscuits.</p>
  {body}
</main>
"""

WELCOME = """
  <p>hello, <b>{name}</b> &mdash; you are a <b>{role}</b>.</p>
  <p>your jar holds:<br><code>{cookie}</code></p>
  <p><a href="/flag">/flag</a> is for admins.</p>
  <form method="post" action="/login">
    <input name="name" placeholder="another name" maxlength="32">
    <button type="submit">take a new one</button>
  </form>
"""

STRANGER = """
  <p>nobody has handed you a cookie yet.</p>
  <form method="post" action="/login">
    <input name="name" placeholder="your name" maxlength="32" autofocus>
    <button type="submit">have one</button>
  </form>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cookiejar/1.0"

    # --- answers --------------------------------------------------------------

    def send(self, status, html, cookie=None):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", "%s=%s; Path=/" % (COOKIE, cookie))
        self.end_headers()
        self.wfile.write(body)

    def page(self, body, cookie=None):
        self.send(200, PAGE.format(body=body), cookie)

    # --- what the player is holding -------------------------------------------

    def cookie(self):
        jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return jar[COOKIE].value if COOKIE in jar else None

    # --- routes ---------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.index()
        if path == "/flag":
            return self.flag()
        self.send(404, PAGE.format(body="<p>no such shelf.</p>"))

    def do_POST(self):
        if self.path.split("?")[0] != "/login":
            return self.send(404, PAGE.format(body="<p>no such shelf.</p>"))
        length = min(int(self.headers.get("Content-Length") or 0), MAX_FORM)
        fields = dict(field.partition("=")[::2]
                      for field in self.rfile.read(length).decode("utf-8", "replace").split("&"))
        name = unquote_plus(fields.get("name", "")).strip()
        if not NAME_RE.match(name):
            name = "guest"
        # Everyone who walks in is a guest. Admin is something you become by
        # taking it, which is the whole challenge.
        cookie = bake({"name": name, "role": "guest"})
        self.page(WELCOME.format(name=escape(name), role="guest", cookie=cookie), cookie)

    def index(self):
        cookie = self.cookie()
        claims = taste(cookie) if cookie else None
        if claims is None:
            return self.page(STRANGER)
        self.page(WELCOME.format(name=escape(claims.get("name", "nobody")),
                                 role=escape(claims.get("role", "guest")),
                                 cookie=escape(cookie)))

    def flag(self):
        claims = taste(self.cookie() or "") or {}
        if claims.get("role") != "admin":
            return self.send(403, PAGE.format(
                body='<p class="no">admins only. yours says <b>%s</b>.</p>'
                     % escape(claims.get("role", "nobody"))))
        self.send(200, PAGE.format(body='<p class="flag">%s</p>' % escape(FLAG)))

    def log_message(self, fmt, *args):
        pass                               # a challenge log nobody reads is noise


if __name__ == "__main__":
    print("cookie jar listening on 0.0.0.0:%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

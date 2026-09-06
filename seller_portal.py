#!/usr/bin/env python3
"""
seller_portal.py -- the delivery mechanism for the seller diagnostic.

`seller_report.py` answers "why agents are not paying you". It ran on a laptop,
which is not a product: a seller could not get one without us mailing it, and the
seller email has been blocked for weeks. This serves it self-serve at a URL, so
the answer to "why is nobody buying from me" is a link rather than an outreach
campaign.

A SEPARATE PROCESS, deliberately, for three reasons:

 1. `seller_report`'s rule 3 says the engine must never import the report, or a
    seller could influence their own verdict through their own report. Serving
    from `blackwall.py` would make that structural test a lie.
 2. The threat surfaces are genuinely different. The verdict API takes JSON from
    an agent; this takes a string from a browser and renders HTML back. That is
    an XSS surface the verdict API does not have, and a defect here must not be
    able to reach the engine holding the signing keys.
 3. It can be deployed, restarted, rate-limited or switched off on its own,
    without touching the endpoint agents depend on.

WHY IT IS PUBLIC RATHER THAN PER-SELLER AUTHENTICATED: every finding is derived
from public data -- the chain, the seller's own advertised prices, and their live
402 -- and it is the SAME answer `/v1/forecast-payment` already returns to any
anonymous caller who asks about that payee. Publishing it discloses nothing new;
withholding it would only mean a seller cannot see what every buyer already can.

THE SECURITY PROPERTY THAT MATTERS IS SSRF. This service makes an outbound HTTP
request as part of answering, and the caller supplies a string. If that string
could ever become the probe URL, anyone could point us at a cloud metadata
endpoint or an internal host and read the result through our report. It cannot:
the caller's input is only ever used to LOOK UP a row in our committed corpus,
and the probe target comes from that row's `resources`. A key we do not hold
produces a report and NO network call at all. `test_seller_portal` asserts this
directly rather than trusting the reading.

CLI:
  python seller_portal.py [--port 8410] [--store rep.db] [--host 127.0.0.1]
"""

import html
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import seller_report as SR

# A payee address is 42 characters and a hostname is capped at 253 by DNS, so
# anything longer is not a key -- rejected BEFORE any lookup, parsing or
# allocation, because the cheapest place to refuse junk is the front door.
MAX_KEY = 260

# Reports are cached so that refreshing a page does not re-probe the seller. That
# protects THEM (we are hitting a stranger's endpoint) as much as us, and it is
# what keeps a public URL from becoming a way to aim traffic at a third party.
CACHE_TTL = 900.0
CACHE_MAX = 512

DEFAULT_RATE = 30          # reports per window, per client
DEFAULT_WINDOW = 60.0
DEFAULT_BURST = 10


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def clean_key(raw):
    """Normalize a caller-supplied key, or None when it cannot be one.

    Rejects on LENGTH and on control characters. It deliberately does NOT try to
    validate the shape further -- `find_rows` matches against the corpus and a
    key that matches nothing is answered honestly. Guessing at "valid" here would
    just be a second, worse copy of the corpus lookup.
    """
    if raw is None:
        return None
    text = unquote(str(raw)).strip()
    if not text or len(text) > MAX_KEY:
        return None
    if any(ch.isspace() or not ch.isprintable() for ch in text):
        return None
    return text


def route_of(path):
    """(route, key) for a request path. Pure, so the routing table is testable.

    Returns ("report", key) / ("home", None) / ("health", None) / (None, None).
    """
    split = urlsplit(path or "")
    route = split.path or "/"
    if route in ("", "/"):
        return "home", None
    if route == "/healthz":
        return "health", None
    if route.startswith("/r/"):
        return "report", clean_key(route[3:])
    return None, None


def wants_json(path):
    params = parse_qs(urlsplit(path or "").query)
    return (params.get("format") or [""])[0].lower() == "json"


class ReportCache:
    """TTL + bounded LRU cache of rendered reports. Thread-safe.

    Bounded because the key space is attacker-chosen: without a cap, a caller
    could ask for a million distinct keys and grow the process until it dies.
    Misses on unknown keys are cached too -- they are the cheap answer, and
    caching them is what stops a key-enumeration flood from being expensive.
    """

    def __init__(self, ttl=CACHE_TTL, max_entries=CACHE_MAX):
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self._data = {}
        self._order = []
        self._lock = threading.Lock()

    def get(self, key, now):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if now - entry[0] >= self.ttl:
                self._data.pop(key, None)
                if key in self._order:
                    self._order.remove(key)
                return None
            return entry[1]

    def put(self, key, value, now):
        with self._lock:
            if key not in self._data and len(self._data) >= self.max_entries:
                oldest = self._order.pop(0)
                self._data.pop(oldest, None)
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
            self._data[key] = (now, value)

    def __len__(self):
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# HTML rendering -- every interpolation is escaped
# ---------------------------------------------------------------------------
# SIXTH instance of the untrusted-echo class in this repo, and the first where it
# is XSS rather than a forged log line: `payee_syntax`'s hint, `approvals`'
# decided_by, `billing_preflight`'s facilitator kinds, `secret_scan`'s whole
# reason for existing, and `seller_report`'s own terminal output. Here the text
# reaches a BROWSER -- the key comes from the URL, and every finding carries
# strings authored by the party being reported on -- so escaping is not
# cosmetic, it is the difference between a report and a script host.
def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


_BADGE = {SR.BLOCKER: ("blocker", "An agent cannot pay you"),
          SR.WARNING: ("warning", "A buyer's engine will not clear you"),
          SR.INFO: ("ok", "Clear"),
          SR.UNKNOWN: ("unknown", "Not established")}

_CSS = """
:root{--bg:#fbfbfa;--fg:#1d1c1a;--mut:#6b6862;--line:#e4e2dd;--card:#fff;
--blocker:#a4231f;--warning:#8a5a12;--ok:#2a6b3f;--unknown:#6b6862}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#1a1a19;--fg:#eceae6;--mut:#9b968e;--line:#33322f;--card:#232220;
--blocker:#e8807c;--warning:#d7a34e;--ok:#7cc294;--unknown:#9b968e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,
BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:40px 20px 80px}
h1{font-size:23px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:15px;margin:34px 0 10px;color:var(--mut);font-weight:600;
text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--mut);margin:0 0 28px}
.hero{border:1px solid var(--line);border-radius:10px;padding:18px 20px;
background:var(--card);margin-bottom:8px}
.hero .verdict{font-size:19px;font-weight:600;margin-bottom:4px}
.meta{color:var(--mut);font-size:13px;word-break:break-all}
.f{border:1px solid var(--line);border-radius:10px;padding:14px 16px;
background:var(--card);margin-bottom:10px}
.f .t{font-weight:600;margin-bottom:4px;display:flex;gap:9px;align-items:baseline}
.tag{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
flex:none}
.blocker .tag,.b-blocker{color:var(--blocker)}
.warning .tag,.b-warning{color:var(--warning)}
.ok .tag,.b-ok{color:var(--ok)}
.unknown .tag,.b-unknown{color:var(--unknown)}
.d{margin:0 0 6px}
.ev{color:var(--mut);font-size:12.5px;word-break:break-word}
form{display:flex;gap:8px;margin:22px 0 10px;flex-wrap:wrap}
input{flex:1 1 320px;padding:10px 12px;font:inherit;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--fg)}
button{padding:10px 18px;font:inherit;font-weight:600;border:0;border-radius:8px;
background:var(--fg);color:var(--bg);cursor:pointer}
code{background:var(--line);padding:1px 5px;border-radius:4px;font-size:13px}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
color:var(--mut);font-size:12.5px}
a{color:inherit}
"""


def page(title, body):
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><style>%s</style></head><body><div class=wrap>%s"
            "</div></body></html>" % (esc(title), _CSS, body))


def render_home():
    return page("x402 seller diagnostic", """
<h1>Why agents are not paying you</h1>
<p class=sub>Enter the wallet address you get paid to, or the host you serve
x402 from. Everything below is derived from public data &mdash; the chain, your
own advertised prices, and your live payment challenge.</p>
<form method=get action=/go>
  <input name=key placeholder="0x… or api.example.com" maxlength=260
         autocomplete=off spellcheck=false>
  <button type=submit>Run the check</button>
</form>
<h2>What you get</h2>
<div class=f><div class=t>The verdict a buyer's agent actually gets on you</div>
<p class=d>Not our opinion of your endpoint &mdash; the real output of the engine
agents call before they sign, with its reasons.</p></div>
<div class=f><div class=t>Whether an agent can read your price at all</div>
<p class=d>Checked live. An agent prices and signs from your challenge; if it
cannot parse one, you are not expensive, you are invisible.</p></div>
<div class=f><div class=t>Whether your payers pay anyone else</div>
<p class=d>The one thing you cannot check from your own receipts. Your receipts
show who paid you; only a cross-payee view shows whether those wallets exist
anywhere else in the market.</p></div>
<footer>Descriptive only. Nothing here changes any verdict, and no report is an
input to the engine that scores you.</footer>""")


def render_report(report):
    key = esc(report.get("key"))
    cls, headline = _BADGE.get(report.get("severity"), _BADGE[SR.UNKNOWN])
    parts = ["<h1>Why agents are not paying you</h1>",
             "<p class=sub>Report for <code>%s</code></p>" % key,
             "<div class=hero><div class='verdict b-%s'>%s</div>"
             % (cls, esc(SR._HEADLINE.get(report.get("severity"), "")))]
    if report.get("found"):
        parts.append(
            "<div class=meta>%s &middot; %s settlements from %s distinct payers "
            "&middot; %s</div>"
            % (esc(", ".join(report.get("hosts") or []) or "no known host"),
               esc(report.get("settlements")), esc(report.get("distinct_payers")),
               esc(report.get("category") or "unclassified")))
    parts.append("</div><h2>Findings</h2>")
    for f in report.get("findings") or []:
        badge = _BADGE.get(f.get("severity"), _BADGE[SR.UNKNOWN])[0]
        parts.append(
            "<div class='f %s'><div class=t><span class=tag>%s</span>"
            "<span>%s</span></div><p class=d>%s</p><p class=ev>%s</p></div>"
            % (badge, esc(badge), esc(f.get("title")), esc(f.get("detail")),
               esc(f.get("evidence"))))
    parts.append(
        "<footer>Derived from public data: on-chain settlement history, the "
        "prices you advertise, and a live read of your payment challenge. "
        "Descriptive only &mdash; this report is never an input to the engine "
        "that scores you. <a href=/>Run another</a></footer>")
    return page("Seller diagnostic — %s" % report.get("key"), "".join(parts))


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------
class Portal:
    """Holds the corpus and the precomputed graph; answers report requests.

    The payer graph is built ONCE at startup, never per request: building it
    takes minutes over the real corpus, so doing it on the hot path would make
    the first visitor wait and every visitor after them wait again. This is the
    same precompute-at-boot pattern `issuer_trust_gate` uses.
    """

    def __init__(self, rows=None, coverage=None, category_index=None,
                 store_path=None, probe=True, cache=None, clock=time.monotonic):
        self.rows = rows if rows is not None else SR.load_json(SR.DIRECTORY_PATH, [])
        self.coverage = (coverage if coverage is not None
                         else SR.load_json(SR.COVERAGE_PATH, {}))
        self.category_index = (category_index if category_index is not None
                               else SR.load_json(SR.CATEGORY_INDEX_PATH, {}))
        self.probe = probe
        self.cache = cache if cache is not None else ReportCache()
        self.clock = clock
        self.graph = {}
        self.graph_error = None
        if store_path:
            self._build_graph(store_path)

    def _build_graph(self, store_path):
        try:
            from payer_reputation import PayerReputationSource
            from reputation_store import ReputationStore
            source = PayerReputationSource.from_store(ReputationStore(store_path))
            for row in self.rows:
                payee = row.get("payee")
                if payee:
                    self.graph[str(payee).lower()] = source.cross_signal(payee)
        except Exception as e:
            # Fail-soft and LOUD: the portal still answers, and the demand
            # finding says the graph could not be read rather than implying
            # nobody asked for it.
            self.graph_error = "%s: %s" % (type(e).__name__, e)
            sys.stderr.write("seller_portal: payer graph unavailable (%s)\n"
                             % self.graph_error)

    def _cross_fn(self, payee):
        return self.graph.get(str(payee or "").lower()), self.graph_error

    def _probe_fn(self, resources):
        """THE SSRF BOUNDARY.

        `resources` comes from the corpus row we selected, never from the
        caller: `build_report` looks the key up first and hands US the row's own
        resource list. A key that matches no row never reaches this function at
        all, because `build_report` returns the not-found report before probing.
        """
        if not self.probe:
            return None
        return SR.probe_resources(resources)

    def report(self, key):
        """Cached report for `key`. Returns the report dict."""
        now = self.clock()
        cached = self.cache.get(key, now)
        if cached is not None:
            return cached
        report = SR.build_report(key, self.rows, coverage=self.coverage,
                                 probe_fn=self._probe_fn,
                                 cross_fn=self._cross_fn,
                                 category_index=self.category_index)
        self.cache.put(key, report, now)
        return report


class _Handler(BaseHTTPRequestHandler):
    portal = None
    limiter = None
    server_version = "blackwall-seller-portal"
    sys_version = ""

    def _send(self, code, body, content_type, extra=None):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # A report is a document, not an app: it loads no scripts, styles or
        # images of its own, so the strictest policy costs nothing and removes
        # the whole injected-script class even if an escape were ever missed.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _limited(self):
        if self.limiter is None:
            return False
        from ratelimit import client_ip_from
        key = client_ip_from(self.headers.get("X-Forwarded-For"),
                             self.client_address[0] if self.client_address else None)
        allowed, retry = self.limiter.allow(key, time.monotonic())
        if allowed:
            return False
        self._send(429, page("Too many requests",
                             "<h1>Too many requests</h1><p class=sub>Each report "
                             "makes a live request to someone else's endpoint, so "
                             "this is rate limited. Try again shortly.</p>"),
                   "text/html; charset=utf-8",
                   {"Retry-After": str(int(retry) + 1)})
        return True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route, key = route_of(self.path)
        if route == "health":
            return self._send(200, '{"ok":true}', "application/json")
        if route == "home":
            return self._send(200, render_home(), "text/html; charset=utf-8")
        # `/go?key=` is the form target: it normalizes into the shareable
        # /r/<key> URL rather than answering, so a report always has one address.
        if (urlsplit(self.path).path or "") == "/go":
            raw = (parse_qs(urlsplit(self.path).query).get("key") or [""])[0]
            clean = clean_key(raw)
            if not clean:
                return self._send(400, page("Not a key",
                                            "<h1>That is not an address or host</h1>"
                                            "<p class=sub><a href=/>Try again</a>"
                                            "</p>"),
                                  "text/html; charset=utf-8")
            from urllib.parse import quote
            return self._send(302, "", "text/html; charset=utf-8",
                              {"Location": "/r/" + quote(clean, safe="")})
        if route != "report":
            return self._send(404, page("Not found", "<h1>Not found</h1>"
                                        "<p class=sub><a href=/>Start here</a></p>"),
                              "text/html; charset=utf-8")
        if key is None:
            return self._send(400, page("Not a key",
                                        "<h1>That is not an address or host</h1>"
                                        "<p class=sub><a href=/>Try again</a></p>"),
                              "text/html; charset=utf-8")
        if self._limited():
            return
        report = self.portal.report(key)
        if wants_json(self.path):
            return self._send(200, json.dumps(report, indent=1, sort_keys=True),
                              "application/json")
        self._send(200, render_report(report), "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        sys.stderr.write("seller-portal: %s\n" % (fmt % args))


def serve_forever(host="127.0.0.1", port=8410, store=None, probe=True,
                  rate=DEFAULT_RATE, window=DEFAULT_WINDOW, burst=DEFAULT_BURST):
    from ratelimit import RateLimiter

    portal = Portal(store_path=store, probe=probe)
    sys.stdout.write("seller-portal: %d payees in the corpus, %d with a payer "
                     "graph\n" % (len(portal.rows), len(portal.graph)))
    sys.stdout.flush()

    handler = type("_Bound", (_Handler,),
                   {"portal": portal,
                    "limiter": RateLimiter(rate, window, burst=burst)})
    httpd = ThreadingHTTPServer((host, port), handler)
    sys.stdout.write("seller-portal on http://%s:%d\n" % (host, port))
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(description="Serve the seller diagnostic.")
    p.add_argument("--host", default=os.environ.get("PORTAL_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORTAL_PORT", "8410")))
    p.add_argument("--store", default=os.environ.get("PORTAL_STORE"),
                   help="reputation store; enables the demand-authenticity "
                        "finding (built once at startup)")
    p.add_argument("--no-probe", action="store_true",
                   help="never make the live outbound request")
    p.add_argument("--rate", type=int, default=DEFAULT_RATE)
    args = p.parse_args(argv)

    serve_forever(host=args.host, port=args.port, store=args.store,
                  probe=not args.no_probe, rate=args.rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())

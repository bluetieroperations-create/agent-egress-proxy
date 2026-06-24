#!/usr/bin/env python3
"""
egress_proxy.py -- localhost-only HTTP/HTTPS (CONNECT) forward proxy.

A NETWORK-layer egress control for the OpenClaw agent orchestrator. Its Python
engine uses `requests`, which honors the HTTPS_PROXY / HTTP_PROXY environment
variables, so pointing those at this proxy routes outbound agent traffic
through it. The proxy:

  * LOGS every FORWARDED destination (host:port) an agent reaches AND every
    rejected/blocked attempt (JSONL). A rejected attempt egresses NOTHING (we
    never connect upstream) but is still logged so the observe log is a
    complete attempt record for building the allowlist. Invariant: every
    accepted connection produces at least one log line.
  * In `enforce` mode, BLOCKS any host not on a dot-boundary allowlist.

It binds 127.0.0.1 ONLY -- it is NOT an open relay. Stdlib only, no deps.

The three pure functions at the top are the security boundary
(parse_connect_target / host_allowed / decide); they are unit-tested TDD-first.

HONEST LIMIT: this governs the normal proxy-respecting path (requests / env
vars) and logs it. It is NOT airtight against a fully-compromised agent that
opens raw sockets bypassing the proxy env vars -- that requires OS-level
default-deny egress (firewall / network namespace). See README.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MAX_HEADER_BYTES = 16 * 1024      # cap request line + headers (oversize/slowloris guard)
HEADER_READ_TIMEOUT = 10.0        # seconds to read the request header
UPSTREAM_CONNECT_TIMEOUT = 10.0   # seconds for socket.create_connection upstream
IDLE_TUNNEL_TIMEOUT = 300.0       # seconds of total idle before tearing a tunnel down
MAX_CONCURRENT = 200              # bounded concurrency (semaphore)
RELAY_BUF = 65536                 # tunnel relay chunk size
MAX_HOST_LEN = 255                # RFC-ish hostname cap (also our parse cap)

# Control chars that must never appear in a host token. Anything < 0x20, plus
# DEL, plus space. This is the CRLF / request-smuggling injection guard.
_FORBIDDEN_HOST_CHARS = set(chr(c) for c in range(0x21)) | {chr(0x7f)}


# ===========================================================================
# SECURITY-BOUNDARY PURE FUNCTIONS (unit-tested first)
# ===========================================================================
def parse_connect_target(request_line: str):
    """
    Parse a CONNECT request line of the form `CONNECT host:port HTTP/1.1`.

    Returns (host, port) on success, or None to REJECT.

    Rejects (None): non-CONNECT verb, missing/garbage structure, missing port,
    non-numeric port, port outside 1..65535, empty host, host > 255 chars,
    host containing spaces / control chars / CRLF (injection guard), and
    ambiguous bare (unbracketed) IPv6.
    """
    if not request_line:
        return None

    # Tolerate a trailing CRLF on the line, but only at the very end.
    line = request_line
    if line.endswith("\r\n"):
        line = line[:-2]
    elif line.endswith("\n"):
        line = line[:-1]

    # After stripping the trailing newline, ANY remaining CR or LF is an
    # injection attempt (e.g. "evil\r\nHost: x") -> reject.
    if "\r" in line or "\n" in line:
        return None

    # Split into exactly three space-separated tokens: VERB TARGET VERSION.
    parts = line.split(" ")
    if len(parts) != 3:
        return None
    verb, target, version = parts

    if verb != "CONNECT":
        return None
    if not version.startswith("HTTP/"):
        return None
    if not target:
        return None

    # ---- split host:port (bracketed IPv6 aware) ----
    if target.startswith("["):
        # Bracketed IPv6: [host]:port
        end = target.find("]")
        if end == -1:
            return None
        host = target[1:end]
        rest = target[end + 1:]
        if not rest.startswith(":"):
            return None
        port_str = rest[1:]
    else:
        # host:port -- the LAST colon separates the port. If there is more than
        # one colon and it's not bracketed, it's an ambiguous bare IPv6 -> reject.
        if target.count(":") != 1:
            return None
        host, port_str = target.rsplit(":", 1)

    if not host:
        return None
    if len(host) > MAX_HOST_LEN:
        return None

    # Control-char / space / CRLF guard on the host token.
    for ch in host:
        if ch in _FORBIDDEN_HOST_CHARS:
            return None

    # Port must be purely digits (rejects "-1", "4a3", "44 3", empty, "+5").
    if not port_str.isdigit():
        return None
    try:
        port = int(port_str)
    except ValueError:
        return None
    if port < 1 or port > 65535:
        return None

    return (host, port)


def host_allowed(host: str, allowlist) -> bool:
    """
    Dot-boundary, case-insensitive, trailing-dot-stripped allowlist match.

    True iff (normalized) host == entry OR host endswith ("." + entry).
    Empty allowlist -> always False (fail-closed). This is the function whose
    naive `endswith(entry)` form lets `evilblackwalltier.com` slip past
    `blackwalltier.com`; the dot boundary is what stops it.
    """
    if not allowlist:
        return False
    if not host:
        return False

    h = host.strip().rstrip(".").lower()
    if not h:
        return False

    for entry in allowlist:
        e = entry.strip().rstrip(".").lower()
        if not e:
            continue
        if h == e or h.endswith("." + e):
            return True
    return False


def decide(host: str, mode, allowlist) -> str:
    """
    Decide what to do with a request to `host`.

    observe -> always "forward" (logging happens regardless of decision).
    enforce -> "forward" iff host_allowed else "block".
    Any unknown / None mode -> treated as enforce (fail-closed).
    """
    if mode == "observe":
        return "forward"
    # enforce + every unknown mode are fail-closed.
    return "forward" if host_allowed(host, allowlist) else "block"


# ===========================================================================
# Config loading
# ===========================================================================
def load_allowlist(path) -> set:
    """
    Load an allowlist file: one host per line, `#` comments and blank lines
    ignored, case-folded, trailing dots stripped. Missing file -> empty set.
    """
    out = set()
    if not path:
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # allow inline trailing comments? Keep it simple: no, a '#' only
                # starts a comment at column 0. Hosts never contain '#'.
                host = line.rstrip(".").lower()
                if host:
                    out.add(host)
    except FileNotFoundError:
        return set()
    except OSError:
        return set()
    return out


# ===========================================================================
# Proxy server
# ===========================================================================
class EgressProxy:
    def __init__(self, host="127.0.0.1", port=8888, mode="observe",
                 allowlist=None, log_path="egress.log"):
        self.host = host
        self.port = port
        self.mode = mode
        self.allowlist = allowlist if allowlist is not None else set()
        self.log_path = log_path
        self._sem = threading.Semaphore(MAX_CONCURRENT)
        self._log_lock = threading.Lock()
        self._listener = None
        self._stop = threading.Event()

    # ---- logging ----
    def _log(self, client, method, host, port, decision, bytes_up=0, bytes_down=0):
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "client": client,
            "method": method,
            "host": host,
            "port": port,
            "decision": decision,
            "bytes_up": bytes_up,
            "bytes_down": bytes_down,
        }
        line = json.dumps(rec, separators=(",", ":"))
        with self._log_lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                sys.stderr.write("egress-proxy: log write failed: %s\n" % e)
        # human one-liner to stdout
        sys.stdout.write(
            "[%s] %s %s %s:%s %s (up=%d down=%d)\n"
            % (rec["ts"], decision.upper(), method, host, port,
               client, bytes_up, bytes_down)
        )
        sys.stdout.flush()

    # ---- header reading (capped) ----
    @staticmethod
    def _read_headers(conn):
        """
        Read up to MAX_HEADER_BYTES until the end of headers (\r\n\r\n) or the
        end of the first line for our purposes. Returns the raw bytes read, or
        None if the cap is exceeded before headers terminate.
        """
        conn.settimeout(HEADER_READ_TIMEOUT)
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_HEADER_BYTES:
                return None
        return bytes(buf)

    # ---- per-connection handler ----
    def _handle(self, conn, addr):
        client = "%s:%s" % (addr[0], addr[1])
        try:
            raw = self._read_headers(conn)
            if raw is None:
                # oversize / slowloris / timeout. The attempt egresses NOTHING
                # (we never connect upstream), but we STILL log it so the
                # observe log is a complete attempt record for the
                # allowlist-building workflow. "no silent egress" != "no log".
                self._send_simple(conn, 400, "Bad Request")
                self._log(client, "?", "?", 0, "reject-oversize")
                return
            if not raw:
                # client opened a connection then sent nothing / closed before
                # any header bytes -- no attempt to record, nothing forwarded.
                return

            try:
                head, _, _body = raw.partition(b"\r\n\r\n")
                first_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            except Exception:
                # could not even extract a request line -> log the failed
                # attempt (nothing egressed, but it must not vanish).
                self._send_simple(conn, 400, "Bad Request")
                self._log(client, "?", "?", 0, "reject-parse")
                return

            if first_line.startswith("CONNECT "):
                self._handle_connect(conn, client, first_line)
            else:
                self._handle_plain(conn, client, first_line, head, _body)
        except Exception as e:
            sys.stderr.write("egress-proxy: handler error: %s\n" % e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ---- CONNECT (HTTPS tunnel) ----
    def _handle_connect(self, conn, client, first_line):
        target = parse_connect_target(first_line)
        if target is None:
            # Malformed CONNECT target. Nothing egressed (we never connect),
            # but log the attempt so it is part of the record.
            self._send_simple(conn, 400, "Bad Request")
            self._log(client, "CONNECT", "?", 0, "reject-parse")
            return
        host, port = target

        action = decide(host, self.mode, self.allowlist)
        if action == "block":
            self._send_simple(conn, 403, "Forbidden")
            self._log(client, "CONNECT", host, port, "block")
            return

        # forward
        try:
            upstream = socket.create_connection((host, port),
                                                timeout=UPSTREAM_CONNECT_TIMEOUT)
        except OSError as e:
            self._send_simple(conn, 502, "Bad Gateway")
            self._log(client, "CONNECT", host, port,
                      "observe-forward" if self.mode == "observe" else "allow")
            sys.stderr.write("egress-proxy: upstream connect failed %s:%s: %s\n"
                             % (host, port, e))
            return

        try:
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except OSError:
            upstream.close()
            return

        decision = "observe-forward" if self.mode == "observe" else "allow"
        # Record the destination the INSTANT forwarding is committed (upstream
        # connected + tunnel about to carry bytes). This is the "no silent
        # egress" guarantee: even if the tunnel raises mid-stream, or runs for
        # the full idle window without closing, the destination is already on
        # disk. The byte-count tally is written on teardown via finally, so an
        # exception in _tunnel can never suppress a destination log line.
        self._log(client, "CONNECT", host, port, decision)
        up = down = 0
        try:
            up, down = self._tunnel(conn, upstream)
        finally:
            self._log(client, "CONNECT", host, port, decision + "-close", up, down)

    @staticmethod
    def _tunnel(client_sock, upstream_sock):
        """Bidirectional relay via select until either side closes / idle timeout."""
        client_sock.setblocking(False)
        upstream_sock.setblocking(False)
        socks = [client_sock, upstream_sock]
        bytes_up = 0    # client -> upstream
        bytes_down = 0  # upstream -> client
        try:
            while True:
                try:
                    rlist, _, xlist = select.select(
                        socks, [], socks, IDLE_TUNNEL_TIMEOUT)
                except (OSError, ValueError):
                    # A socket was closed out from under select (fileno -1 ->
                    # ValueError on CPython) or an OS-level select error. Tear
                    # the tunnel down cleanly; the caller still logs the tally.
                    break
                if xlist:
                    break
                if not rlist:
                    break  # idle timeout
                for s in rlist:
                    try:
                        data = s.recv(RELAY_BUF)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        return bytes_up, bytes_down
                    if not data:
                        return bytes_up, bytes_down
                    if s is client_sock:
                        try:
                            upstream_sock.sendall(data)
                        except OSError:
                            return bytes_up, bytes_down
                        bytes_up += len(data)
                    else:
                        try:
                            client_sock.sendall(data)
                        except OSError:
                            return bytes_up, bytes_down
                        bytes_down += len(data)
        finally:
            try:
                upstream_sock.close()
            except OSError:
                pass
        return bytes_up, bytes_down

    # ---- plain HTTP (absolute-form) -- best effort ----
    def _handle_plain(self, conn, client, first_line, head, body):
        """
        Best-effort plain-HTTP forwarding for absolute-form requests
        (`GET http://host/path HTTP/1.1`). We log + decide; block -> 403;
        forward -> one-shot relay with Connection: close.

        LIMIT: this is a simple one-shot relay (no keep-alive, no chunked
        request bodies beyond what arrived in the first read, no HTTP/2). The
        fully-correct path in this proxy is HTTPS/CONNECT; plain HTTP is
        best-effort + always logged + enforced.
        """
        parts = first_line.split(" ")
        if len(parts) != 3:
            self._send_simple(conn, 400, "Bad Request")
            self._log(client, "?", "?", 0, "reject-parse")
            return
        method, uri, _version = parts

        host, port, path = self._parse_plain_target(uri, head)
        if host is None:
            self._send_simple(conn, 400, "Bad Request")
            self._log(client, method, "?", 0, "reject-parse")
            return

        # Guard the host token the same way parse_connect does (injection).
        if (len(host) > MAX_HOST_LEN
                or any(ch in _FORBIDDEN_HOST_CHARS for ch in host)):
            self._send_simple(conn, 400, "Bad Request")
            self._log(client, method, "?", 0, "reject-parse")
            return

        action = decide(host, self.mode, self.allowlist)
        if action == "block":
            self._send_simple(conn, 403, "Forbidden")
            self._log(client, method, host, port, "block")
            return

        decision = "observe-forward" if self.mode == "observe" else "allow"
        # Same "no silent egress" guarantee as the CONNECT path: record the
        # destination before any upstream bytes flow, and always write the
        # byte-count tally on teardown even if _relay_plain raises.
        self._log(client, method, host, port, decision)
        up = down = 0
        try:
            up, down = self._relay_plain(conn, method, host, port, path,
                                         head, body, _version)
        finally:
            self._log(client, method, host, port, decision + "-close", up, down)

    @staticmethod
    def _parse_plain_target(uri, head):
        """Extract (host, port, origin_path) from an absolute-form URI / Host header."""
        host = None
        port = 80
        path = "/"
        if uri.startswith("http://"):
            rest = uri[len("http://"):]
            slash = rest.find("/")
            if slash == -1:
                authority = rest
                path = "/"
            else:
                authority = rest[:slash]
                path = rest[slash:]
            # strip userinfo if present
            if "@" in authority:
                authority = authority.rsplit("@", 1)[1]
            if authority.startswith("["):  # bracketed IPv6
                end = authority.find("]")
                if end != -1:
                    host = authority[1:end]
                    tail = authority[end + 1:]
                    if tail.startswith(":") and tail[1:].isdigit():
                        port = int(tail[1:])
            elif ":" in authority:
                h, p = authority.rsplit(":", 1)
                host = h
                if p.isdigit():
                    port = int(p)
            else:
                host = authority
        else:
            # origin-form (shouldn't normally hit a proxy) -- fall back to Host header
            path = uri or "/"

        if host is None:
            # parse Host: header
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"host:"):
                    val = line.split(b":", 1)[1].strip().decode("latin-1")
                    if val.startswith("["):
                        end = val.find("]")
                        if end != -1:
                            host = val[1:end]
                            tail = val[end + 1:]
                            if tail.startswith(":") and tail[1:].isdigit():
                                port = int(tail[1:])
                    elif ":" in val:
                        h, p = val.rsplit(":", 1)
                        host = h
                        if p.isdigit():
                            port = int(p)
                    else:
                        host = val
                    break

        if not host or not (1 <= port <= 65535):
            return None, 0, "/"
        return host, port, path

    def _relay_plain(self, conn, method, host, port, path, head, body, version):
        """One-shot origin-form relay with Connection: close."""
        try:
            upstream = socket.create_connection((host, port),
                                                timeout=UPSTREAM_CONNECT_TIMEOUT)
        except OSError as e:
            self._send_simple(conn, 502, "Bad Gateway")
            sys.stderr.write("egress-proxy: plain upstream failed %s:%s: %s\n"
                             % (host, port, e))
            return 0, 0

        bytes_up = 0
        bytes_down = 0
        try:
            # Rebuild header block in origin-form, force Connection: close.
            lines = head.split(b"\r\n")
            rebuilt = ["%s %s %s" % (method, path, version)]
            for line in lines[1:]:
                low = line.lower()
                if low.startswith(b"proxy-connection:") or low.startswith(b"connection:"):
                    continue
                if line:
                    rebuilt.append(line.decode("latin-1"))
            rebuilt.append("Connection: close")
            request = ("\r\n".join(rebuilt) + "\r\n\r\n").encode("latin-1")
            request += body
            upstream.sendall(request)
            bytes_up += len(request)

            upstream.settimeout(IDLE_TUNNEL_TIMEOUT)
            while True:
                try:
                    chunk = upstream.recv(RELAY_BUF)
                except socket.timeout:
                    break
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    conn.sendall(chunk)
                except OSError:
                    break
                bytes_down += len(chunk)
        finally:
            try:
                upstream.close()
            except OSError:
                pass
        return bytes_up, bytes_down

    # ---- helpers ----
    @staticmethod
    def _send_simple(conn, code, reason):
        body = ("%d %s\n" % (code, reason)).encode("latin-1")
        msg = (
            "HTTP/1.1 %d %s\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "\r\n" % (code, reason, len(body))
        ).encode("latin-1") + body
        try:
            conn.sendall(msg)
        except OSError:
            pass

    # ---- server loop ----
    def serve_forever(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bind 127.0.0.1 ONLY -- never 0.0.0.0. Not an open relay.
        self._listener.bind((self.host, self.port))
        # If port==0 (ephemeral, used by tests), record the OS-assigned port so
        # callers / tests can connect. Also re-confirms the bound interface.
        self.port = self._listener.getsockname()[1]
        self._listener.listen(128)
        self._listener.settimeout(1.0)  # so Ctrl-C / stop is responsive

        if self.mode == "enforce" and not self.allowlist:
            sys.stderr.write(
                "egress-proxy: WARNING -- enforce mode with EMPTY allowlist: "
                "ALL egress will be BLOCKED (block-all / fail-closed).\n"
            )
        sys.stdout.write(
            "egress-proxy listening on %s:%d  mode=%s  allowlist=%d entries  log=%s\n"
            % (self.host, self.port, self.mode, len(self.allowlist), self.log_path)
        )
        sys.stdout.flush()

        try:
            while not self._stop.is_set():
                try:
                    conn, addr = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not self._sem.acquire(blocking=False):
                    # concurrency cap hit -> shed load, don't exhaust the host
                    self._send_simple(conn, 503, "Service Unavailable")
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                t = threading.Thread(
                    target=self._handle_wrapped, args=(conn, addr), daemon=True
                )
                t.start()
        finally:
            self.shutdown()

    def _handle_wrapped(self, conn, addr):
        try:
            self._handle(conn, addr)
        finally:
            self._sem.release()

    def shutdown(self):
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None


# ===========================================================================
# CLI
# ===========================================================================
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Localhost-only HTTP/HTTPS (CONNECT) egress-control proxy."
    )
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("EGRESS_PROXY_PORT", "8888")),
                   help="listen port (default 8888; bind is always 127.0.0.1)")
    p.add_argument("--mode", choices=["observe", "enforce"],
                   default=os.environ.get("EGRESS_PROXY_MODE", "observe"),
                   help="observe = log only; enforce = block non-allowlisted hosts")
    p.add_argument("--allowlist",
                   default=os.environ.get("EGRESS_PROXY_ALLOWLIST"),
                   help="path to allowlist file (one host per line, # comments)")
    default_log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "egress.log")
    p.add_argument("--log",
                   default=os.environ.get("EGRESS_PROXY_LOG", default_log),
                   help="JSONL log file path (default egress.log beside script)")
    args = p.parse_args(argv)

    allowlist = load_allowlist(args.allowlist) if args.allowlist else set()

    proxy = EgressProxy(host="127.0.0.1", port=args.port, mode=args.mode,
                        allowlist=allowlist, log_path=args.log)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\negress-proxy: shutting down (Ctrl-C)\n")
        proxy.shutdown()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

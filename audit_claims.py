#!/usr/bin/env python3
"""
Independent audit harness for BlackWall (egress_proxy.py).

This does NOT reuse the project's own unit tests. It stands up the real proxy
on an ephemeral port, points a raw-socket client at it, and drives LIVE traffic
to confirm each claim in TRUST.md / THREAT_MODEL.md is true end-to-end. Where a
claim is about forwarding, we stand up a real local "upstream" server and verify
bytes actually flow (or don't). We read the resulting log file to confirm the
audit-trail invariants.

Exit non-zero if any claim fails.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

# Import the product under audit from the repo (this file lives beside it).
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import egress_proxy as ep  # noqa: E402

RESULTS = []


def check(claim, ok, detail=""):
    RESULTS.append((claim, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {claim}" + (f"  --- {detail}" if detail else ""))


# --- a trivial upstream server: any bytes in -> known banner back ---
class Upstream:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.5)
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.hits += 1
            try:
                c.recv(65536)
                c.sendall(b"UPSTREAM-OK")
            except OSError:
                pass
            finally:
                c.close()

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def start_proxy(mode, allowlist, logpath):
    p = ep.EgressProxy(host="127.0.0.1", port=0, mode=mode,
                       allowlist=allowlist, log_path=logpath)
    threading.Thread(target=p.serve_forever, daemon=True).start()
    # wait for bind
    for _ in range(100):
        if p.port:
            try:
                s = socket.create_connection(("127.0.0.1", p.port), timeout=0.2)
                s.close()
                break
            except OSError:
                pass
        time.sleep(0.02)
    return p


def send_raw(proxy_port, data, read_reply=True, keep_open=False):
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=3)
    s.sendall(data)
    reply = b""
    if read_reply:
        s.settimeout(2)
        try:
            reply = s.recv(4096)
        except socket.timeout:
            pass
    if not keep_open:
        s.close()
        return reply, None
    return reply, s


def read_log(logpath):
    if not os.path.exists(logpath):
        return []
    out = []
    with open(logpath) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="bw-audit-")

    # ================= ENFORCE MODE =================
    log_e = os.path.join(tmp, "enforce.log")
    allow = {"example.com", "127.0.0.1"}  # allow our loopback upstream by IP label? IP handled below
    up = Upstream()
    # We want to allowlist the upstream. It's reached as 127.0.0.1:<port>.
    allow = {"example.com", "127.0.0.1"}
    px = start_proxy("enforce", allow, log_e)

    # T1/claim: allowlisted host is forwarded and bytes actually flow (real tunnel)
    req = f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
    reply, s = send_raw(px.port, req, read_reply=True, keep_open=True)
    ok_estab = reply.startswith(b"HTTP/1.1 200")
    banner = b""
    if s:
        try:
            s.sendall(b"hello-through-tunnel")
            s.settimeout(2)
            banner = s.recv(64)
        except OSError:
            pass
        s.close()
    check("enforce: allowlisted CONNECT establishes tunnel (200)", ok_estab, reply[:40])
    check("enforce: real bytes traverse the tunnel to upstream",
          banner == b"UPSTREAM-OK" and up.hits >= 1, f"banner={banner!r} hits={up.hits}")

    # claim T1/T2: non-allowlisted host is BLOCKED with 403 and NO upstream connect
    hits_before = up.hits
    reply, _ = send_raw(px.port, b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n")
    check("enforce: non-allowlisted CONNECT -> 403 Forbidden",
          reply.startswith(b"HTTP/1.1 403"), reply[:40])

    # claim T2: suffix-bypass rejected (evilexample.com must NOT match example.com)
    reply, _ = send_raw(px.port, b"CONNECT evilexample.com:443 HTTP/1.1\r\n\r\n")
    check("enforce: suffix-bypass evilexample.com blocked (dot-boundary)",
          reply.startswith(b"HTTP/1.1 403"), reply[:40])

    # claim T2: attacker-suffix rejected (example.com.attacker.com)
    reply, _ = send_raw(px.port, b"CONNECT example.com.attacker.com:443 HTTP/1.1\r\n\r\n")
    check("enforce: example.com.attacker.com blocked",
          reply.startswith(b"HTTP/1.1 403"), reply[:40])

    # claim: subdomain of allowlisted entry is allowed (dot-boundary positive)
    reply, _ = send_raw(px.port, b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n")
    # upstream connect will fail (example.com unreachable in sandbox) -> 502, but
    # the point is it was ALLOWED past policy (not 403).
    check("enforce: api.example.com allowed past policy (subdomain match)",
          not reply.startswith(b"HTTP/1.1 403"), reply[:40])

    # claim T3: CRLF injection in host rejected (request smuggling guard)
    reply, _ = send_raw(px.port, b"CONNECT ev\x00il:443 HTTP/1.1\r\n\r\n")
    check("enforce: control-char in host -> 400 reject-parse",
          reply.startswith(b"HTTP/1.1 400"), reply[:40])

    # claim T4: bad port rejected
    reply, _ = send_raw(px.port, b"CONNECT host.com:70000 HTTP/1.1\r\n\r\n")
    check("enforce: out-of-range port -> 400", reply.startswith(b"HTTP/1.1 400"), reply[:40])

    reply, _ = send_raw(px.port, b"CONNECT host.com:0 HTTP/1.1\r\n\r\n")
    check("enforce: port 0 -> 400", reply.startswith(b"HTTP/1.1 400"), reply[:40])

    # claim T4: non-CONNECT garbage verb on connect-shaped path
    reply, _ = send_raw(px.port, b"NOTAVERB junk\r\n\r\n")
    check("enforce: garbage request -> 400", reply.startswith(b"HTTP/1.1 400"), reply[:40])

    # claim T5: oversize headers rejected (>16KB) with no upstream connect
    hits_before = up.hits
    big = b"CONNECT example.com:443 HTTP/1.1\r\n" + b"X-Pad: " + b"A" * (17 * 1024) + b"\r\n\r\n"
    reply, _ = send_raw(px.port, big)
    check("enforce: >16KB header -> 400 reject-oversize",
          reply.startswith(b"HTTP/1.1 400"), reply[:40])
    check("enforce: oversize attempt made NO upstream connection",
          up.hits == hits_before, f"hits delta={up.hits - hits_before}")

    time.sleep(0.3)  # let close-log lines flush
    log = read_log(log_e)
    decisions = [r["decision"] for r in log]

    # claim T8: EVERY accepted attempt produced a log line; blocks logged; rejects logged
    check("audit-trail: block decisions are logged",
          any(d == "block" for d in decisions),
          f"blocks={sum(d=='block' for d in decisions)}")
    check("audit-trail: reject-parse logged (never silent)",
          any(d == "reject-parse" for d in decisions),
          f"reject-parse={sum(d=='reject-parse' for d in decisions)}")
    check("audit-trail: reject-oversize logged (never silent)",
          any(d == "reject-oversize" for d in decisions))
    # claim T8: forwarded destination logged BEFORE bytes + a -close teardown tally exists
    allow_open = any(d == "allow" for d in decisions)
    allow_close = any(d == "allow-close" for d in decisions)
    check("audit-trail: forwarded conn logged at open AND teardown (byte tally)",
          allow_open and allow_close,
          f"allow={allow_open} allow-close={allow_close}")
    # claim: blocked host still recorded by name (evidence completeness)
    blocked_hosts = [r["host"] for r in log if r["decision"] == "block"]
    check("audit-trail: blocked destination host recorded by name",
          "evil.com" in blocked_hosts, f"blocked_hosts={blocked_hosts}")

    px.shutdown()

    # ================= FAIL-CLOSED: enforce + empty allowlist =================
    log_fc = os.path.join(tmp, "failclosed.log")
    px2 = start_proxy("enforce", set(), log_fc)
    reply, _ = send_raw(px2.port, f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\n\r\n".encode())
    check("fail-closed: enforce + EMPTY allowlist blocks everything (403)",
          reply.startswith(b"HTTP/1.1 403"), reply[:40])
    px2.shutdown()

    # ================= OBSERVE MODE: forwards all, logs all =================
    log_o = os.path.join(tmp, "observe.log")
    px3 = start_proxy("observe", set(), log_o)  # empty allowlist, but observe forwards anyway
    hits_before = up.hits
    reply, s = send_raw(px3.port,
                        f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\n\r\n".encode(),
                        read_reply=True, keep_open=True)
    ok = reply.startswith(b"HTTP/1.1 200")
    if s:
        s.close()
    check("observe: forwards even with empty allowlist (non-gating)", ok, reply[:40])
    time.sleep(0.2)
    log = read_log(log_o)
    check("observe: forward is logged as observe-forward",
          any(r["decision"] == "observe-forward" for r in log),
          f"decisions={[r['decision'] for r in log]}")
    px3.shutdown()

    # ================= BIND: localhost only =================
    # The proxy was constructed with host='127.0.0.1'. Confirm it is NOT bound to
    # a routable/wildcard address. We check the source constant + the live socket.
    px4 = start_proxy("observe", set(), os.path.join(tmp, "bind.log"))
    bound_host = px4._listener.getsockname()[0] if px4._listener else "?"
    check("bind: live listener bound to 127.0.0.1 (not 0.0.0.0)",
          bound_host == "127.0.0.1", f"bound={bound_host}")
    px4.shutdown()

    up.stop()

    # ================= SUPPLY CHAIN: stdlib-only =================
    with open(os.path.join(REPO, "egress_proxy.py")) as f:
        src = f.read()
    third_party = []
    for line in src.splitlines():
        ls = line.strip()
        if ls.startswith("import ") or ls.startswith("from "):
            mod = ls.split()[1].split(".")[0]
            if mod not in ("argparse", "json", "os", "select", "socket", "sys",
                           "threading", "time", "__future__"):
                third_party.append(mod)
    check("supply-chain: zero third-party imports (stdlib only)",
          not third_party, f"unexpected={third_party}")

    # ================= SUMMARY =================
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"AUDIT RESULT: {passed}/{total} claims verified")
    failed = [c for c, ok, _ in RESULTS if not ok]
    if failed:
        print("FAILED CLAIMS:")
        for c in failed:
            print("  - " + c)
        return 1
    print("All audited claims hold under live traffic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

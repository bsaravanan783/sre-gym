"""Contract tests: one test per promise in sandbox.py. Every backend must pass the same file.

Run:  python3 test_sandbox.py [docker|firecracker|e2b]     (no pytest needed; under pytest set SBX_KIND)
      firecracker: nix shell nixpkgs#firecracker nixpkgs#e2fsprogs -c python3 test_sandbox.py firecracker
      e2b:         ~/fc/.venv/bin/python test_sandbox.py e2b        (costs a fraction of a cent of credit)
"""
import os, resource, subprocess, sys, tempfile, time
from sandbox import make_sandbox, SandboxError, OUTPUT_CAP

KIND = sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else os.environ.get("SBX_KIND", "docker")


def box(writable=False, workdir=None):
    return make_sandbox(KIND, workdir or tempfile.mkdtemp(prefix="sbx-"), writable)


def live_boxes():
    """Leak check: how many of our boxes exist right now."""
    if KIND == "docker":
        r = subprocess.run(["docker", "ps", "-q", "--filter", "label=sre-gym=1"], capture_output=True, text=True)
        return len(r.stdout.split())
    if KIND == "e2b":                       # ask E2B which of OUR boxes are still running (and billing)
        from e2b import Sandbox, SandboxQuery
        from sandbox import E2BSandbox
        q = SandboxQuery(metadata={"sre-gym": "1"})
        return len(Sandbox.list(query=q, api_key=E2BSandbox._api_key()).next_items())
    n = 0                                   # firecracker: scan /proc ourselves (no pgrep -f: it can match itself)
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if argv[0].endswith(b"firecracker") and any(b"/sbx-fc-" in a for a in argv):
            n += 1
    return n


# 1. run() returns within its timeout, always
def test_1_returns_within_timeout():
    with box() as b:
        t0 = time.perf_counter()
        r = b.run("sleep 100", timeout=2)
        took = time.perf_counter() - t0
    assert r.timed_out, r
    assert took < 2 + 3, f"took {took:.1f}s for a 2s timeout"


def test_1b_ignoring_term_still_returns():
    with box() as b:
        r = b.run("trap '' TERM; sleep 100", timeout=2)            # the command refuses the polite signal
    assert r.timed_out, r


# 2. sandbox failures raise; command failures return
def test_2_command_failure_is_a_result():
    with box() as b:
        r = b.run("exit 3")
    assert r.exit_code == 3 and not r.timed_out, r


def test_2b_dead_sandbox_raises():
    b = box()
    b.start()
    b.stop()
    try:
        b.run("echo hi")
    except SandboxError:
        return
    raise AssertionError("run() on a stopped sandbox did not raise SandboxError")


# 3. stop() always runs, and twice is fine
def test_3_stop_runs_on_exception():
    before = live_boxes()
    try:
        with box() as b:
            b.run("echo hi")
            raise RuntimeError("the agent crashed mid-task")
    except RuntimeError:
        pass
    time.sleep(0.5)
    assert live_boxes() == before, f"leaked a box: {live_boxes() - before} still running"


def test_3b_stop_twice():
    b = box()
    b.start()
    b.stop()
    b.stop()


# 4. output capped WHILE reading (the host's memory), not only in the result
def test_4_output_capped_in_result():
    with box() as b:
        r = b.run("head -c 1000000 /dev/zero | tr '\\0' y")
    assert len(r.stdout) < OUTPUT_CAP + 100, len(r.stdout)
    assert "more bytes cut" in r.stdout


def test_4b_flood_does_not_fill_host_memory():
    peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss          # KiB, highest so far
    with box() as b:
        b.run("head -c 300000000 /dev/zero")                                   # 300 MB of output
    grew_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - peak_before) / 1024
    assert grew_mb < 50, f"host process peak memory grew {grew_mb:.0f} MB for 300 MB of output"


# 5. no network. A TCP connect is not enough: E2B's egress proxy completes the handshake and THEN blocks,
#    so "connected" ≠ "reached". Only an answer from the far side counts.
def test_5_no_network():
    probe = ("exec 3<>/dev/tcp/1.1.1.1/80 && printf 'GET / HTTP/1.0\\r\\nHost: 1.1.1.1\\r\\n\\r\\n' >&3 "
             "&& timeout 5 head -c 12 <&3")
    with box() as b:
        r = b.run(probe, timeout=10)
    assert "HTTP/" not in r.stdout, f"the box got an HTTP answer from the internet: {r.stdout!r}"


# 6. no host secrets
def test_6_no_host_env():
    os.environ["SRE_FAKE_SECRET"] = "s3cr3t-value"
    try:
        with box() as b:
            r = b.run("env")
    finally:
        del os.environ["SRE_FAKE_SECRET"]
    assert "s3cr3t-value" not in r.stdout, r


# 7. only the workdir, ro or rw as asked
def test_7_readonly_workdir():
    d = tempfile.mkdtemp(prefix="sbx-")
    with box(writable=False, workdir=d) as b:
        r = b.run("echo x > /work/report.txt")
    assert r.exit_code != 0 and not os.path.exists(os.path.join(d, "report.txt")), r


def test_7b_writable_workdir_visible_on_host():
    d = tempfile.mkdtemp(prefix="sbx-")
    with box(writable=True, workdir=d) as b:
        r = b.run("echo done > /work/report.txt")
    assert r.exit_code == 0, r
    assert open(os.path.join(d, "report.txt")).read() == "done\n"


def test_7c_host_home_not_visible():
    with box() as b:
        r = b.run(f"ls {os.path.expanduser('~')} 2>&1")
    assert r.exit_code != 0, r


# hygiene: background leftovers don't survive into the next call
def test_8_leftovers_reset():
    with box() as b:
        r1 = b.run("sleep 1234 &")
        r2 = b.run("cat /proc/[0-9]*/cmdline | tr '\\0' ' ' | grep -c 'sleep 123[4]'")   # [4]: grep can't match itself
    assert "leftover" in r1.note, r1
    assert r2.stdout.strip() == "0", r2


# hostile guest: the agent is untrusted. No VM needed: a fake unix socket plays the guest.
def _fake_guest(behaviour):
    """Start a fake Firecracker vsock socket. Returns a FirecrackerSandbox pointed at it."""
    import socket, threading
    from sandbox import FirecrackerSandbox
    d = tempfile.mkdtemp(prefix="sbx-hostile-")
    path = os.path.join(d, "v.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        f = conn.makefile("rb")
        f.readline()                                     # CONNECT 52
        conn.sendall(b"OK 1\n")
        f.readline()                                     # the request
        try:
            behaviour(conn)
        except OSError:                                  # host hung up: good
            pass
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    b = FirecrackerSandbox(d)
    b.uds = path
    return b


def _call_within(b, deadline, limit):
    """Run b._call in a thread; report (finished_in_time, exception)."""
    import threading
    box = {}
    def go():
        try:
            b._call({"op": "run", "cmd": "true", "timeout": 1}, deadline=deadline)
        except Exception as e:
            box["err"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(limit)
    return not t.is_alive(), box.get("err")


def test_9_hostile_guest_slow_drip():
    def drip(conn):                                      # one byte every 0.5 s, never a newline
        while True:
            conn.sendall(b"x")
            time.sleep(0.5)
    done, err = _call_within(_fake_guest(drip), deadline=2, limit=5)
    assert done, "a 2 s call was still waiting after 5 s: the deadline is per read, not per call"
    assert err is not None, "a guest that never finished its reply was accepted"


def test_9b_hostile_guest_flood():
    def flood(conn):                                     # endless bytes, never a newline
        chunk = b"x" * 65536
        while True:
            conn.sendall(chunk)
    peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    done, err = _call_within(_fake_guest(flood), deadline=10, limit=12)
    grew_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - peak_before) / 1024
    assert done and err is not None, f"flood not rejected (done={done}, err={err!r})"
    assert grew_mb < 50, f"host memory grew {grew_mb:.0f} MB from a flooding guest"


def test_10_hostile_archive_stays_in_workdir():
    """E2B copy-back: the tar is built inside the box, so a command can plant `../escaped.txt` in it."""
    import io, tarfile
    from types import SimpleNamespace
    from sandbox import E2BSandbox
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"planted\n"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    parent = tempfile.mkdtemp(prefix="sbx-hostile-")
    work = os.path.join(parent, "work")
    os.mkdir(work)
    fake = SimpleNamespace(                                   # plays the E2B box: no network, no credit
        commands=SimpleNamespace(run=lambda cmd, **kw: SimpleNamespace(stdout=str(len(buf.getvalue())))),
        files=SimpleNamespace(read=lambda *a, **kw: buf.getvalue()))
    b = E2BSandbox(work, writable=True)
    b.sbx = fake
    try:
        b._copy_back()
    except Exception:
        pass                                                  # refusing the archive is fine
    assert not os.path.exists(os.path.join(parent, "escaped.txt")), "a file from the box landed OUTSIDE the workdir"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        t0 = time.perf_counter()
        try:
            fn()
            print(f"PASS  {name}  ({time.perf_counter() - t0:.1f}s)")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed   (boxes still running: {live_boxes()})")
    sys.exit(1 if failed else 0)

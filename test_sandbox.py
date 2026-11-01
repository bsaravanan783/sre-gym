"""Contract tests: one test per promise in sandbox.py. Every backend must pass the same file.

Run:  python3 test_sandbox.py [docker]        (no pytest needed; also works under pytest)
"""
import os, resource, subprocess, sys, tempfile, time
from sandbox import make_sandbox, SandboxError, OUTPUT_CAP

KIND = sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "docker"


def box(writable=False, workdir=None):
    return make_sandbox(KIND, workdir or tempfile.mkdtemp(prefix="sbx-"), writable)


def live_boxes():
    """Docker-backend leak check: how many of our boxes exist right now."""
    r = subprocess.run(["docker", "ps", "-q", "--filter", "label=sre-gym=1"], capture_output=True, text=True)
    return len(r.stdout.split())


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


# 5. no network
def test_5_no_network():
    with box() as b:
        r = b.run("timeout 3 bash -c 'echo > /dev/tcp/1.1.1.1/443' && echo REACHED")
    assert "REACHED" not in r.stdout, r


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
        r1 = b.run("sleep 100 &")
        r2 = b.run("cat /proc/[0-9]*/comm | grep -c '^sleep$'")    # no ps in this image; only the box's own sleep should remain
    assert "leftover" in r1.note, r1
    assert r2.stdout.strip() == "1", r2


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

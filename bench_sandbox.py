"""Per-backend: time to a usable box (start) and per-call latency of a trivial command (warm).

Run:  nix shell nixpkgs#firecracker nixpkgs#e2fsprogs -c python3 bench_sandbox.py [runs]
"""
import statistics, sys, tempfile, time
from sandbox import make_sandbox

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
CALLS = 20


def bench(kind):
    starts, calls = [], []
    for _ in range(RUNS):
        b = make_sandbox(kind, tempfile.mkdtemp(prefix="sbx-"), writable=False)
        t0 = time.perf_counter()
        b.start()
        try:
            starts.append((time.perf_counter() - t0) * 1000)
            for _ in range(CALLS):
                t1 = time.perf_counter()
                r = b.run("echo hi", timeout=10)
                calls.append((time.perf_counter() - t1) * 1000)
                if r.stdout != "hi\n":
                    raise RuntimeError(f"{kind}: unexpected output {r!r}")
        finally:
            b.stop()
    return starts, calls


def show(kind, label, xs):
    print(f"{kind:12} {label:22} min {min(xs):7.1f}   median {statistics.median(xs):7.1f}   max {max(xs):7.1f} ms")


for kind in ("docker", "firecracker"):
    s, c = bench(kind)
    show(kind, f"start ({RUNS} boxes)", s)
    show(kind, f"run('echo hi') ×{len(c)}", c)

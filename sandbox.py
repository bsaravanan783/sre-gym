"""Sandbox interface: one run(), many machines.

    with make_sandbox("docker", workdir, writable=False) as box:
        r = box.run("ls", timeout=30)        # Result(exit_code, stdout, stderr, timed_out, ms, note)

The contract every backend keeps (test_sandbox.py checks each one):
  1. run() returns within its timeout, always.
  2. Failures of the SANDBOX raise SandboxError. A command that fails is not one: it returns its exit code.
  3. stop() always runs (also on errors) and is safe to call twice.
  4. Output is capped while it is read, so a flood can't fill the host's memory.
  5. No network.
  6. No host secrets go in (no env vars, no host files except the workdir).
  7. Only the workdir is shared, read-only or read-write as asked.
"""
import os, subprocess, threading, time, uuid
from dataclasses import dataclass

OUTPUT_CAP = 64 * 1024        # bytes kept per stream (stdout, stderr)
BACKSTOP = 5                  # extra seconds the HOST waits after the in-box timer, if docker itself hangs


@dataclass
class Result:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    ms: float
    note: str = ""            # anything the sandbox did on its own, e.g. a reset


class SandboxError(Exception):
    """The sandbox itself failed (couldn't start, died, hung). Not raised for a command's own failure."""


class Sandbox:
    def __init__(self, workdir, writable=False):
        self.workdir, self.writable = os.path.realpath(workdir), writable
        self.id = None

    def start(self): raise NotImplementedError
    def run(self, cmd, timeout=30): raise NotImplementedError
    def stop(self): raise NotImplementedError

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.stop()
            return False                         # let the error propagate to the caller
        self.stop()


def _drain(stream, cap, out):
    """Read a pipe to the end; keep the first `cap` bytes, count the rest."""
    kept, total = bytearray(), 0
    for chunk in iter(lambda: stream.read1(65536), b""):
        total += len(chunk)
        if len(kept) < cap:
            kept += chunk[:cap - len(kept)]
    out.append(( bytes(kept), total))


def _text(kept, total):
    s = kept.decode("utf-8", errors="replace")
    if total > len(kept):
        s += f"\n[... {total - len(kept)} more bytes cut]"
    return s


class DockerSandbox(Sandbox):
    IMAGE = "redis:7"         # temporary: has bash, grep, awk, sed, sort — swap for a purpose-built image later
    LIFETIME = 3600           # the box kills itself after this many seconds, even if we die without stop()

    def start(self):
        mode = "rw" if self.writable else "ro"
        cmd = ["docker", "run", "-d", "--rm",
               "--name", f"sre-box-{uuid.uuid4().hex[:8]}", "--label", "sre-gym=1",   # findable for leak checks
               "--pids-limit", "100",
               "--memory", "256m", "--memory-swap", "256m",
               "--read-only", "--tmpfs", "/tmp:size=64m",
               "--network", "none",                                        # promise 5
               "--user", "1000:1000",
               "--cap-drop=ALL", "--security-opt", "no-new-privileges",
               "-v", f"{self.workdir}:/work:{mode}", "-w", "/work",        # promise 7: the only mount
               "--entrypoint", "sleep", self.IMAGE, str(self.LIFETIME)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise SandboxError(f"docker run failed: {r.stderr.strip()}")
        self.id = r.stdout.strip()

    def run(self, cmd, timeout=30):
        if self.id is None:
            raise SandboxError("sandbox is not running")
        # The timer runs INSIDE the box: killing the `docker exec` client would NOT kill the process in the box.
        # -k 1: TERM first, KILL one second later if the command ignores TERM.
        argv = ["docker", "exec", self.id, "timeout", "-k", "1", str(timeout), "bash", "-c", cmd]
        t0 = time.perf_counter()
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            raise SandboxError(f"could not start docker exec: {e}") from e
        out, err = [], []
        readers = [threading.Thread(target=_drain, args=(p.stdout, OUTPUT_CAP, out), daemon=True),
                   threading.Thread(target=_drain, args=(p.stderr, OUTPUT_CAP, err), daemon=True)]
        for t in readers:
            t.start()
        try:
            p.wait(timeout=timeout + 1 + BACKSTOP)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise SandboxError(f"docker exec hung past the {timeout}s timeout + {BACKSTOP}s backstop")
        for t in readers:
            t.join(timeout=2)
        ms = (time.perf_counter() - t0) * 1000
        timed_out = p.returncode in (124, 137) and ms >= timeout * 1000
        note = self._reset_if_dirty()
        return Result(p.returncode, _text(*out[0]) if out else "", _text(*err[0]) if err else "",
                      timed_out, ms, note)

    def _reset_if_dirty(self):
        """Background processes left behind (`sleep 100 &`) would outlive the call → restart the box."""
        r = subprocess.run(["docker", "top", self.id], capture_output=True, text=True)
        if r.returncode != 0:
            raise SandboxError(f"sandbox died: {r.stderr.strip()}")
        leftovers = len(r.stdout.strip().splitlines()) - 2          # header + our `sleep` are not leftovers
        if leftovers <= 0:
            return ""
        r = subprocess.run(["docker", "restart", "-t", "0", self.id], capture_output=True, text=True)
        if r.returncode != 0:
            raise SandboxError(f"could not reset the sandbox: {r.stderr.strip()}")
        return f"{leftovers} leftover background process(es) were killed; /tmp was wiped, /work is unchanged"

    def stop(self):
        if self.id is None:                       # already stopped: calling twice is fine
            return
        cid, self.id = self.id, None
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def make_sandbox(kind, workdir, writable=False):
    if kind == "docker":
        return DockerSandbox(workdir, writable)
    if kind in ("firecracker", "e2b"):
        raise NotImplementedError(f"{kind} backend comes in chunk 4b/4c")
    raise ValueError(f"unknown sandbox kind: {kind!r}")

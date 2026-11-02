"""Sandbox interface: one run(), many machines.

    with make_sandbox("docker" | "firecracker" | "e2b", workdir, writable=False) as box:
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


class FirecrackerSandbox(Sandbox):
    """A microVM per sandbox. Needs firecracker + e2fsprogs on PATH:
         nix shell nixpkgs#firecracker nixpkgs#e2fsprogs -c python3 ...

    Host ⇄ guest: Firecracker turns guest vsock port 52 into a unix socket on the host.
    Workdir: copied INTO a fresh ext4 image (second disk), copied BACK after the VM has stopped.
    """
    FC = os.path.expanduser("~/fc")
    KERNEL = os.path.join(FC, "vmlinux-6.1.155")
    ROOTFS = os.path.join(FC, "rootfs-agent.ext4")              # rootfs-min + /fc-agent, /fc-agent.py, /work
    ARGS = "console=ttyS0 reboot=k panic=1 ro i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd init=/fc-agent"
    PORT = 52
    MAX_REPLY = 2 * 6 * OUTPUT_CAP + 64 * 1024                  # 2 capped streams × 6 (a \0 byte → \u0000 in JSON) + slack
    BOOT_LIMIT = 10                                             # seconds for the agent to answer its first ping

    def __init__(self, workdir, writable=False):
        super().__init__(workdir, writable)
        self.proc = None

    def start(self):
        import shutil, json, tempfile
        for tool in ("firecracker", "mkfs.ext4", "debugfs"):
            if shutil.which(tool) is None:
                raise SandboxError(f"{tool} not on PATH (run inside nix shell nixpkgs#firecracker nixpkgs#e2fsprogs)")
        self.dir = tempfile.mkdtemp(prefix="sbx-fc-")
        self.img = os.path.join(self.dir, "work.ext4")
        self.uds = os.path.join(self.dir, "v.sock")
        try:
            self._make_work_image()
            cfg = {
                "boot-source": {"kernel_image_path": self.KERNEL,
                                "boot_args": f"{self.ARGS} sbx.rw={int(self.writable)}"},
                "drives": [
                    {"drive_id": "rootfs", "path_on_host": self.ROOTFS, "is_root_device": True, "is_read_only": True},
                    {"drive_id": "work", "path_on_host": self.img, "is_root_device": False,
                     "is_read_only": not self.writable},                   # promise 7: ro is enforced by the VMM too
                ],
                "machine-config": {"vcpu_count": 1, "mem_size_mib": 256},
                "vsock": {"guest_cid": 3, "uds_path": self.uds},
                # no "network-interfaces": the guest has no NIC at all (promise 5)
            }
            path = os.path.join(self.dir, "vm.json")
            with open(path, "w") as f:
                json.dump(cfg, f)
            self.log = open(os.path.join(self.dir, "console.log"), "wb")   # a FILE, not a pipe nobody reads
            self.proc = subprocess.Popen(["firecracker", "--no-api", "--config-file", path],
                                         stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
                                         env={"PATH": os.environ["PATH"]})          # promise 6: no host env
            self._wait_ready()
        except BaseException:
            self.id = self.dir           # so stop() cleans up whatever got made
            self.stop()
            raise
        self.id = self.dir

    def _make_work_image(self):
        used = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(self.workdir) for f in fs
                   if os.path.isfile(os.path.join(d, f)))
        size_mb = max(64, 2 * used // 2**20 + 32)
        r = subprocess.run(["mkfs.ext4", "-q", "-F", "-O", "^has_journal", "-E", "root_owner=1000:1000",
                            "-d", self.workdir, self.img, f"{size_mb}M"], capture_output=True, text=True)
        if r.returncode != 0:
            raise SandboxError(f"mkfs.ext4 failed: {r.stderr.strip()}")

    def _wait_ready(self):
        end = time.perf_counter() + self.BOOT_LIMIT
        while time.perf_counter() < end:
            if self.proc.poll() is not None:
                raise SandboxError(f"firecracker exited ({self.proc.returncode}): {self._console_tail()}")
            try:
                self._call({"op": "ping"}, deadline=1)
                return
            except (OSError, ValueError, SandboxError):
                time.sleep(0.01)
        raise SandboxError(f"guest agent not ready after {self.BOOT_LIMIT}s: {self._console_tail()}")

    def _console_tail(self):
        try:
            return open(os.path.join(self.dir, "console.log"), "rb").read()[-500:].decode(errors="replace")
        except OSError:
            return ""

    def _call(self, req, deadline):
        """One request → one reply over Firecracker's vsock unix socket.

        `deadline` is for the WHOLE call: socket.settimeout() alone limits each read separately, so a guest
        sending one byte every few seconds would never trip it. Every read gets only the time that is left.
        The guest is untrusted: replies are size-limited, and a reply without its newline is an error.
        """
        import json, socket
        end = time.monotonic() + deadline

        def left():
            r = end - time.monotonic()
            if r <= 0:
                raise socket.timeout(f"call took longer than {deadline}s")
            return r

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(left())
            s.connect(self.uds)
            s.sendall(f"CONNECT {self.PORT}\n".encode())
            if not self._read_line(s, 64, left).startswith(b"OK"):        # Firecracker's handshake answer
                raise SandboxError("vsock handshake refused (agent not listening yet?)")
            s.settimeout(left())
            s.sendall(json.dumps(req).encode() + b"\n")
            return json.loads(self._read_line(s, self.MAX_REPLY, left))

    @staticmethod
    def _read_line(s, limit, left):
        buf = bytearray()
        while True:
            s.settimeout(left())
            chunk = s.recv(65536)
            if not chunk:
                raise SandboxError("guest closed the connection before a full reply")
            buf += chunk
            nl = buf.find(b"\n")
            if nl >= 0:
                return bytes(buf[:nl + 1])
            if len(buf) > limit:
                raise SandboxError(f"guest reply longer than {limit} bytes")

    def run(self, cmd, timeout=30):
        import socket
        if self.id is None or self.proc is None or self.proc.poll() is not None:
            raise SandboxError("sandbox is not running")
        t0 = time.perf_counter()
        try:
            rep = self._call({"op": "run", "cmd": cmd, "timeout": timeout}, deadline=timeout + 1 + BACKSTOP)
        except socket.timeout:
            self._kill()                                                    # HARD deadline: the whole VM goes
            raise SandboxError(f"guest agent hung past {timeout}s + {BACKSTOP}s backstop; VM killed")
        except (OSError, ValueError) as e:
            self._kill()
            raise SandboxError(f"talking to the guest agent failed: {e}") from e
        ms = (time.perf_counter() - t0) * 1000
        return Result(int(rep["exit_code"]), str(rep["stdout"]), str(rep["stderr"]),
                      bool(rep["timed_out"]), ms, str(rep.get("note", "")))

    def _kill(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def _copy_back(self):
        """Read the stopped VM's workdir image and copy new/changed files to the host workdir (no deletes)."""
        import shutil, tempfile
        out = tempfile.mkdtemp(prefix="sbx-out-", dir=self.dir)
        subprocess.run(["debugfs", "-R", f"rdump / {out}", self.img], capture_output=True)   # chown warnings: fine
        shutil.rmtree(os.path.join(out, "lost+found"), ignore_errors=True)
        shutil.copytree(out, self.workdir, dirs_exist_ok=True)

    def stop(self):
        import shutil
        if self.id is None:                                                 # already stopped
            return
        self.id = None
        try:
            if self.writable and self.proc is not None and self.proc.poll() is None:
                try:
                    self._call({"op": "shutdown"}, deadline=3)              # guest: sync + umount /work
                except (OSError, ValueError, SandboxError):
                    pass
                try:
                    self.proc.wait(timeout=3)                               # reboot=k → firecracker exits 0
                except subprocess.TimeoutExpired:
                    pass
                # Only a clean exit means the guest flushed and unmounted. After a crash or kill the image may be
                # half-written: losing the agent's changes beats copying broken files into the real workdir.
                if self.proc.returncode == 0:
                    self._copy_back()
        finally:
            self._kill()
            if getattr(self, "log", None):
                self.log.close()
            shutil.rmtree(self.dir, ignore_errors=True)


class _StreamCap:
    """on_stdout/on_stderr callback for E2B: keeps the first `cap` characters, counts the rest.
    Second wall only: the runner inside the box already caps (see _E2B_RUNNER)."""
    def __init__(self, cap=OUTPUT_CAP):
        self.cap, self.kept, self.n_kept, self.total = cap, [], 0, 0

    def __call__(self, s):
        self.total += len(s)
        room = self.cap - self.n_kept
        if room > 0:
            self.kept.append(s[:room])
            self.n_kept += len(s[:room])

    def value(self):
        return "".join(self.kept)


# Drains a stream INSIDE the box at local speed and keeps only the first `cap` bytes. Over a 1-3 MB/s link to
# the US, a host-side cap alone can't keep promise 1: every byte must cross the ocean before it can be dropped.
_E2B_CAPPER = r"""
import sys
cap, kept, total, r = int(sys.argv[1]), bytearray(), 0, sys.stdin.buffer
while chunk := r.read1(65536):
    total += len(chunk)
    kept += chunk[:max(0, cap - len(kept))]
sys.stdout.buffer.write(kept)
open(sys.argv[2], "w").write(str(total))
"""

# Runs as root inside the E2B box for every call. The agent's command runs as `sbx`: a user with no sudo
# (E2B's default user has passwordless sudo, so file permissions alone would protect nothing).
_E2B_RUNNER = r'''#!/bin/bash
cmd=$SBX_CMD; t=$SBX_T; cap=$SBX_CAP; unset SBX_CMD SBX_T SBX_CAP
d=$(mktemp -d)                                        # root-only: the command can't touch our bookkeeping
mkfifo "$d/o" "$d/e"
/usr/local/bin/python3 -I /opt/sbx-cap.py "$cap" "$d/ot" <"$d/o" >"$d/out" & p1=$!
/usr/local/bin/python3 -I /opt/sbx-cap.py "$cap" "$d/et" <"$d/e" >"$d/err" & p2=$!
cd /work
# Drop to `sbx` even though the box is a VM: the VM protects the HOST from the box, but inside the box root
# ignores file permissions (ro /work) and owns everything (leftover check counts sbx processes only).
setsid timeout -k 1 "$t" env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp LANG=C.UTF-8 \
    /usr/sbin/runuser -u sbx -- bash -c "$cmd" </dev/null >"$d/o" 2>"$d/e" &
wait $!; rc=$?
n=$(pgrep -c -u sbx)                     # the command has returned: anything sbx still runs is a leftover
[ "$n" -gt 0 ] && pkill -KILL -u sbx     # also closes the fifos they held, so the cappers see EOF
wait $p1 $p2
cat "$d/out"; cat "$d/err" >&2
echo "SBX_META=$n $(cat "$d/ot") $(cat "$d/et")" >&2  # printed LAST, so the command can't forge it
rm -rf "$d"
exit $rc
'''


def _with_cut(text, total):
    kept = len(text.encode("utf-8", errors="replace"))
    return text + (f"\n[... {total - kept} more bytes cut]" if total > kept else "")


class E2BSandbox(Sandbox):
    """A managed Firecracker microVM at E2B (US). Needs the e2b package and E2B_API_KEY (env or ~/fc/.env).

    No shared folder: the workdir is tarred up and uploaded at start, and (writable) downloaded at stop.
    The copy-back reads from INSIDE the running box, i.e. through the same kernel that did the writes,
    so unlike the Firecracker backend no shutdown is needed first.
    """
    LIFETIME = 600            # E2B kills the box itself after this, even if we die without stop() (Q1's 2nd wall)
    MAX_TAR = 100 * 2**20     # largest workdir we upload or accept back

    def __init__(self, workdir, writable=False):
        super().__init__(workdir, writable)
        self.sbx = None

    @staticmethod
    def _api_key():
        key = os.environ.get("E2B_API_KEY")
        if not key:                                       # read the file, but do NOT put it into os.environ
            path = os.path.expanduser("~/fc/.env")
            for line in open(path) if os.path.exists(path) else []:
                if line.startswith("E2B_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"')
        if not key:
            raise SandboxError("no E2B_API_KEY in the environment or ~/fc/.env")
        return key

    def start(self):
        import io, tarfile
        try:
            from e2b import Sandbox as E2B
        except ImportError as e:
            raise SandboxError("e2b package missing (use ~/fc/.venv/bin/python)") from e
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(self.workdir, arcname=".")
        if buf.tell() > self.MAX_TAR:
            raise SandboxError(f"workdir is {buf.tell() >> 20} MB, limit {self.MAX_TAR >> 20} MB")
        try:
            self.sbx = E2B.create(timeout=self.LIFETIME, allow_internet_access=False,     # promise 5
                                  metadata={"sre-gym": "1"}, api_key=self._api_key())     # no envs: promise 6
            self.id = self.sbx.sandbox_id
            self.sbx.files.write("/tmp/in.tar", buf.getvalue())
            self.sbx.files.write("/tmp/sbx-run", _E2B_RUNNER)
            self.sbx.files.write("/tmp/sbx-cap.py", _E2B_CAPPER)
            owner = "chown -R sbx:sbx /work" if self.writable else "chown -R root:root /work && chmod -R a+rX,go-w /work"
            self.sbx.commands.run(
                "useradd -M -s /bin/bash sbx && install -o root -m 755 /tmp/sbx-run /opt/sbx-run"
                " && install -o root -m 644 /tmp/sbx-cap.py /opt/sbx-cap.py && rm /tmp/sbx-run /tmp/sbx-cap.py"
                f" && mkdir /work && tar -xf /tmp/in.tar -C /work && rm /tmp/in.tar && {owner}",
                user="root", timeout=30)
        except Exception as e:
            self.id = None                                # half set up: nothing to copy back, just kill it
            self.stop()
            raise SandboxError(f"e2b start failed: {e}") from e

    def run(self, cmd, timeout=30):
        from e2b import CommandExitException, TimeoutException
        if self.id is None:
            raise SandboxError("sandbox is not running")
        out, err = _StreamCap(), _StreamCap(OUTPUT_CAP + 256)          # + room for the SBX_META line
        t0 = time.perf_counter()
        try:
            self.sbx.commands.run("/opt/sbx-run", user="root",
                                  envs={"SBX_CMD": cmd, "SBX_T": str(timeout), "SBX_CAP": str(OUTPUT_CAP)},
                                  on_stdout=out, on_stderr=err, retain_output=False,
                                  timeout=timeout + 1 + BACKSTOP)                       # host-side hard deadline
            rc = 0
        except CommandExitException as e:                 # the COMMAND failed: a result, not an error
            rc = e.exit_code
        except TimeoutException as e:
            self.stop()
            raise SandboxError(f"e2b call hung past {timeout}s + {BACKSTOP}s backstop; sandbox killed") from e
        except Exception as e:
            raise SandboxError(f"e2b call failed: {e}") from e
        ms = (time.perf_counter() - t0) * 1000
        stderr, _, meta = err.value().rpartition("SBX_META=")
        try:
            leftovers, out_total, err_total = (int(x) for x in meta.split())
        except ValueError:
            raise SandboxError(f"e2b runner reply malformed: {meta[:80]!r}")
        stderr = stderr[:-1] if stderr.endswith("\n") else stderr          # the newline before our marker
        note = f"{leftovers} leftover background process(es) were killed" if leftovers else ""
        return Result(rc, _with_cut(out.value(), out_total), _with_cut(stderr, err_total),
                      rc in (124, 137) and ms >= timeout * 1000, ms, note)

    def _copy_back(self):
        import io, tarfile
        self.sbx.commands.run("cd /work && tar -cf /tmp/out.tar .", user="root", timeout=60)
        size = int(self.sbx.commands.run("stat -c %s /tmp/out.tar", user="root").stdout)
        if size > self.MAX_TAR:                           # don't pull an unbounded file into host memory (P9)
            raise SandboxError(f"workdir grew to {size >> 20} MB, limit {self.MAX_TAR >> 20} MB; not copied back")
        data = self.sbx.files.read("/tmp/out.tar", format="bytes", user="root")
        with tarfile.open(fileobj=io.BytesIO(bytes(data))) as tar:
            # The archive was built by tar INSIDE the box, from files the agent's commands controlled: untrusted.
            # "data" refuses members that escape the workdir (../x, /abs, symlinks out) and drops setuid/devices.
            tar.extractall(self.workdir, filter="data")

    def stop(self):
        if self.sbx is None:                              # never started, or already stopped
            return
        try:
            if self.writable and self.id is not None:
                self._copy_back()                         # a failure here still raises, AFTER the kill below
        finally:
            sbx, self.sbx, self.id = self.sbx, None, None
            try:
                sbx.kill()
            except Exception:
                pass                                      # already gone; LIFETIME is the backstop anyway


def make_sandbox(kind, workdir, writable=False):
    if kind == "docker":
        return DockerSandbox(workdir, writable)
    if kind == "firecracker":
        return FirecrackerSandbox(workdir, writable)
    if kind == "e2b":
        return E2BSandbox(workdir, writable)
    raise ValueError(f"unknown sandbox kind: {kind!r}")

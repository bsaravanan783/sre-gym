import sys, json, os, shlex, base64
from sandbox import make_sandbox, SandboxError

# Which machine runs the agent's commands: --sandbox=docker|firecracker|e2b (or SBX_KIND), default docker.
#   docker      : bind mount → host and box see the SAME files instantly (one kernel, one page cache)
#   firecracker : workdir copied into a disk image; changes come back only when the VM stops cleanly
#   e2b         : workdir uploaded to a VM in the US; changes downloaded at stop
# For the copy-based backends the BOX is the source of truth while the task runs, so read_file reads from it.
KIND = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--sandbox=")), os.environ.get("SBX_KIND", "docker"))
TIMEOUT = 30
WRITABLE = "--rw" in sys.argv
BOX = None             # set once at startup


def new_box():
    box = make_sandbox(KIND, os.getcwd(), WRITABLE)
    box.start()
    return box


def recover():
    """The box itself failed: throw it away and start a fresh one from the host workdir."""
    global BOX
    BOX.stop()
    BOX = new_box()
    lost = "/tmp was wiped" if KIND == "docker" else "/tmp was wiped and changes since the start may be lost"
    return f"the sandbox was replaced: {lost}"


def run_bash(command):
    try:
        r = BOX.run(command, timeout=TIMEOUT)
    except SandboxError as e:
        raise RuntimeError(f"sandbox failure ({e}); {recover()}. Continue from where you were.")
    if r.timed_out:
        raise TimeoutError(f"Command {command} timed out after {TIMEOUT} seconds")
    note = f"\n[sandbox] {r.note}. Continue from where you were." if r.note else ""
    return f"exit code: {r.exit_code}\nstderr: {r.stderr}\nstdout: {r.stdout}" + note




def is_inside_project_path(path):
        project_dir = os.path.realpath(os.getcwd())
        target_path = os.path.realpath(path)

        if os.path.commonpath([project_dir]) != os.path.commonpath([project_dir, target_path]):
            return False
        else:
            return True 


def _read_lines(path):
    if KIND == "docker":                      # same files as the box: read them directly
        try:
            with open(path, "r") as file:
                return file.readlines()
        except FileNotFoundError:
            raise FileNotFoundError(f"The file {path} was not found in the current directory.")
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(os.getcwd()))
    r = BOX.run(f"cat -- {shlex.quote('/work/' + rel)}", timeout=TIMEOUT)
    if r.exit_code != 0:
        raise FileNotFoundError(f"The file {path} was not found in the current directory.")
    return r.stdout.splitlines(keepends=True)   # capped by the sandbox; page with start_line/end_line


MAX_WRITE = 64 * 1024     # through the box the content travels inside the command (E2B: an env var, ≤128 KB)


def write_file(path, content):
    """Writes where the agent's commands will see it: the host for docker (bind mount), the box otherwise.
    For copy-based backends a host write would be invisible to bash AND overwritten by the copy-back at stop."""
    if not is_inside_project_path(path):
        return "Access denied: Path is outside the project folder."
    if KIND == "docker":
        try:
            with open(path, "w") as file:
                return f"Wrote {file.write(content)} characters to {path}"
        except FileNotFoundError:
            return f"Error: The directory structure for '{path}' was not found."
        except PermissionError as pe:
            return f"Error: permission denied {pe}"
    data = content.encode()
    if len(data) > MAX_WRITE:
        return f"Error: content is {len(data)} bytes; the limit is {MAX_WRITE}. Make a smaller, targeted change."
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(os.getcwd()))
    b64 = base64.b64encode(data).decode()
    r = BOX.run(f"echo {b64} | base64 -d > {shlex.quote('/work/' + rel)}", timeout=TIMEOUT)
    if r.exit_code != 0:
        return f"Error: could not write {path}: {r.stderr.strip()}"
    return f"Wrote {len(content)} characters to {path}"


def read_file(path,start_line=None,end_line=None):
    if not is_inside_project_path(path):
        raise PermissionError("Access denied: Path is outside the project folder.")
    lines = _read_lines(path)

    total = len(lines)
    start = start_line if start_line is not None else 1
    end = end_line if end_line is not None else total

    if total == 0:
        return "[file is empty]"
    if start < 1:
        raise ValueError("start_line must be >= 1")
    if start_line and start > total:
        raise ValueError(f"file has only {total} lines and given start_line as {start_line}")
    if end < start:
        raise  ValueError(f"end_line ({end_line}) must be >= start_line ({start}). To read to the end of the file, omit end_line.")
    
    end = min(end , total)

    content = "".join(lines[start-1:end])
    if start_line is not None or end_line is not None:
        return f"[lines {start}-{end} of {total}]\n" + content

    return content


TOOLS = [
    {"name": "read_file", "description": "Read a text file from the current directory and return its contents.Large files are truncated and using start_line and end_line to page through the content in the file.Line numbers are 1-based and inclusive", "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, e.g. notes.txt"},
                    "start_line": {"type": "integer" , "description": "First line to include (1-based, inclusive)."},
                    "end_line": {"type": "integer" , "description": "Last line to include (1-based, inclusive)."}
                },
                "required": ["path"]
            } },  
    {"name": "run_bash",  "description":f"This is a bash tool and runs in the project directory with a 30-second timeout and must be non-interactive with no input prompts.It returns exit code, stdout and stderr.When outputs are long it gets truncated.It has no network. Working directory is /work ({'writable' if WRITABLE else 'read-only'}); /tmp is writable scratch space.",    
    "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run in bash , e.g. ls , cat"},
                },
                "required": ["command"]
            }
            },
]
WRITE_TOOL = {"name": "write_file", "description": "Write a file to the current directory and return the number of characters written along with the path.It overwrites the content in existing files.Always read a file with read_file before modifying it. Make minimal changes; do not rewrite whole files.",
    "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, e.g. notes.txt"},
                    "content": {"type": "string" , "description": "Content inside the file , e.g. This text has been written in this file"}
                },
                "required": ["path","content"]
            }}
if WRITABLE:                                  # a read-only task is not even offered the tool
    TOOLS.append(WRITE_TOOL)
HANDLERS = {"read_file": read_file, "run_bash": run_bash, **({"write_file": write_file} if WRITABLE else {})}

def handle(msg):
    method = msg.get("method")
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "sre-tools", "version": "0.1"}}
    if method == "tools/list":
        return {"tools":TOOLS}
    if method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments", {})
        try:
            if name not in HANDLERS:
                return {"content": [{"type": "text", "text": f"Unknown tool: {name} . Available: {', '.join(HANDLERS)}"}], "isError": True}
            text = HANDLERS[name](**args)                      # look up the handler, call it with **args
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error : {str(e)}"}], "isError": True}
    raise KeyError(method)                   # unknown method

BOX = new_box()
print(f"{KIND} sandbox {BOX.id} started", file=sys.stderr)
try:
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:                                 # notification? → no reply at all
            continue
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except KeyError:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32601, "message": "Method not found"}}
        print(json.dumps(reply), flush=True)

finally:
    BOX.stop()                                        # writable copy-based boxes copy their changes back here

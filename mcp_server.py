import subprocess,sys,json
import os



IMAGE = "redis:7"      # temporary: has bash, grep, awk, sed, sort (checked) — swap for a purpose-built image later
TIMEOUT = 30
CONTAINER = None       # set once at startup

def start_sandbox(workdir, writable):
    mode = "rw" if writable else "ro"                                    # "rw" or "ro" depending on writable
    cmd = ["docker", "run", "-d", "--rm",
            "--pids-limit", "100",
            "--memory", "256m",
            "--memory-swap", "256m",
            "--read-only",
            "--tmpfs", "/tmp:size=64m",
            "--network", "none",
            "--user", "1000:1000",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
           "-v", f"{workdir}:/work:{mode}", "-w", "/work",
           "--entrypoint", "sleep", IMAGE, "3600"]  # self-destruct: max lifetime in seconds, as a string
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return r.stdout.strip()                        # docker prints the id plus a newline


def box_broken():
    r = subprocess.run(["docker", "exec", CONTAINER, "true"], capture_output=True)
    return r.returncode != 0

def leftover_count():
    r = subprocess.run(["docker", "top", CONTAINER], capture_output=True, text=True)
    return len(r.stdout.strip().splitlines()) - 2



def recover():
    global CONTAINER                          # we reassign CONTAINER below
    r = subprocess.run(["docker", "restart", "-t", "0", CONTAINER], capture_output=True)
    if r.returncode == 0:
        return "the sandbox was restarted"
    CONTAINER = start_sandbox(os.getcwd(), WRITABLE)   # same folder and mode as at startup
    return "the old sandbox had died, so a new one was started"



def run_bash(command):
    try:
        # r = subprocess.run(command,shell=True, capture_output= True,timeout=timeout_duration,text=True,stdin=subprocess.DEVNULL)

        r =  subprocess.run(["docker", "exec", CONTAINER, "timeout", str(TIMEOUT), "bash", "-c", command],   # timer INSIDE the box
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=TIMEOUT + 5) 

        reason = None
        if leftover_count() > 0:
            reason = "leftover background processes were killed"
        elif box_broken():                    # only probe when nothing is left over (~56 ms)
            reason = "the sandbox could not start a new process (likely out of memory)"
        note = ""
        if reason:
            note = f"\n[sandbox] {reason}; {recover()}: /tmp was wiped, /work is unchanged. Continue from where you were."


        if r.returncode == 124:
              raise TimeoutError(f"Command {command} timed out after {TIMEOUT} seconds" + note)

        # return truncate(f"exit code: {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return f"exit code: {r.returncode}\nstderr: {r.stderr}\nstdout: {r.stdout}" + note

    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Command {command} timedout after {TIMEOUT} seconds")




def is_inside_project_path(path):
        project_dir = os.path.realpath(os.getcwd())
        target_path = os.path.realpath(path)

        if os.path.commonpath([project_dir]) != os.path.commonpath([project_dir, target_path]):
            return False
        else:
            return True 


def read_file(path,start_line=None,end_line=None):
    if not is_inside_project_path(path):
        raise PermissionError("Access denied: Path is outside the project folder.")
    try:
        with open(path, "r") as file:
            lines = file.readlines()
    except FileNotFoundError:
        raise FileNotFoundError(f"The file {path} was not found in the current directory.")

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


WRITABLE = "--rw" in sys.argv


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
HANDLERS = {"read_file": read_file, "run_bash": run_bash}

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
                return {"content": [{"type": "text", "text": f"Unknown tool: {name} . Available: read_file,run_bash"}], "isError": True}
            text = HANDLERS[name](**args)                      # look up the handler, call it with **args
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error : {str(e)}"}], "isError": True}
    raise KeyError(method)                   # unknown method

CONTAINER = start_sandbox(os.getcwd(), WRITABLE)      
print(f"sandbox {CONTAINER[:12]} started", file=sys.stderr)
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
    subprocess.run(["docker","rm","-f",CONTAINER], capture_output=True)

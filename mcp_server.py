import subprocess,sys,json
import os



def run_bash(command):
    try:
        timeout_duration = 30
        r = subprocess.run(command,shell=True, capture_output= True,timeout=timeout_duration,text=True,stdin=subprocess.DEVNULL)

        # return truncate(f"exit code: {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return f"exit code: {r.returncode}\nstderr: {r.stderr}\nstdout: {r.stdout}"

    except subprocess.TimeoutExpired:
        return f"Command {command} timedout after {timeout_duration} seconds"

    except Exception as e:
        return f"Error : {e}"



def is_inside_project_path(path):
        project_dir = os.path.realpath(os.getcwd())
        target_path = os.path.realpath(path)

        if os.path.commonpath([project_dir]) != os.path.commonpath([project_dir, target_path]):
            return False
        else:
            return True 


def read_file(path,start_line=None,end_line=None):
    try:
        if not is_inside_project_path(path):
            return "Access denied: Path is outside the project folder."

        with open(path, "r") as file:
            lines = file.readlines()
            total = len(lines)
            start = start_line if start_line is not None else 1
            end = end_line if end_line is not None else total

            if total == 0:
                return "[file is empty]"
            if start < 1:
                return "start_line must be >= 1"
            if start_line and start > total:
                return f"file has only {total} lines and given start_line as {start_line}"
            if end < start:
                return f"Error: end_line ({end_line}) must be >= start_line ({start}). To read to the end of the file, omit end_line."
            
            end = min(end , total)

            content = "".join(lines[start-1:end])
            if start_line is not None or end_line is not None:
                return f"[lines {start}-{end} of {total}]\n" + content

            return content

    except FileNotFoundError:
        return f"The file {path} was not found in the current directory."
    except Exception as e:
        return f"An unexpected error occurred: {e}"




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
    {"name": "run_bash",  "description": "This is a bash tool and runs in the project directory with a 30-second timeout and must be non-interactive with no input prompts.It returns exit code, stdout and stderr.When outputs are long it gets truncated.", "inputSchema": {
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

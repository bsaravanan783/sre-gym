
import argparse, json, os, re, shutil, sys, tempfile, time
from agent import Agent, DEFAULT_POLICY, WORKER_TOOLS, model_config
from mcp_client import MCPClient

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")
TASKS = os.path.join(HERE, "tasks")

def grade(task, answer, workdir):
    """Return (passed, reason). Every check must pass."""
    for c in task["checks"]:
        if c["type"] == "answer_regex":
            if not re.search(c["pattern"] , answer):
                return False, f"answer does not match {c['pattern']}"
        elif c["type"] == "answer_not_contains":
            hit = next(  (v for v in c["values"] if v in answer )  , None)                      # first decoy found in the answer, or None
            if hit:
                return False, f"answer contains decoy {hit!r}"
        elif c["type"] == "file_regex":
            path = os.path.join(workdir, c["path"])
            if not os.path.exists(path) or not re.search(c["pattern"],open(path).read() ):                        # file missing OR pattern not found
                return False, f"{c['path']} does not match {c['pattern']}"
        else:
            raise ValueError(f"unknown check type {c['type']}")
    return True, "all checks passed"

def run_task(task_dir, model):
    task = json.load(open(os.path.join(task_dir, "task.json")))
    workdir = tempfile.mkdtemp(prefix="sre-eval-")
    shutil.copytree(os.path.join(task_dir, "files"), workdir, dirs_exist_ok=True)
    policy =  ({**DEFAULT_POLICY, "rules": [("write_file", None, "allow")] + DEFAULT_POLICY["rules"]}
          if task["needs_write"] else DEFAULT_POLICY)                    # DEFAULT_POLICY, plus ("write_file", None, "allow") when needs_write
    old_cwd = os.getcwd()
    os.chdir(workdir)                       # transcript.jsonl + .outputs/ land here, not in the repo
    client = MCPClient([sys.executable, SERVER], cwd=workdir)
    start = time.time()
    try:
        agent = Agent(model, model, max_steps=task["max_steps"], policy=policy, mcp=client,
                      tools=client.list_tools() + WORKER_TOOLS)            # server tools + the built-in write_file schema
        out = agent.run(task["prompt"])
    finally:
        client.close()
        os.chdir(old_cwd)
    passed, reason = grade(task, out.get("answer") or "", workdir)
    return {"task": os.path.basename(task_dir), "model": model, "passed": passed, "reason": reason,
            "outcome": out["outcome"], "steps": out["steps"], "answer": out.get("answer"),
            "prompt_tokens": out["prompt_tokens"], "completion_tokens": out["completion_tokens"],
            "seconds": round(time.time() - start, 1), "workdir": workdir}

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, choices=list(model_config))
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--only", help="run one task, e.g. 03")
    args = p.parse_args()
    tasks = sorted(d for d in os.listdir(TASKS) if os.path.isdir(os.path.join(TASKS, d)))
    
    if args.only:                              # loop: runs × sorted task dirs (filter by --only),
        tasks = [ t for t in tasks if t.startswith(args.only)]
    
    results = []

    for run in range(args.runs):
        if not tasks:
            sys.exit(f"no tasks matched --only {args.only}")

        for name in tasks:
            try:
                r = run_task(os.path.join(TASKS,name) , args.model)
            except Exception as e:
                 r = {"task": name, "model": args.model, "passed": False, "reason": f"crash: {e}",
                        "outcome": "CRASH", "steps": 0, "answer": None,
                        "prompt_tokens": 0, "completion_tokens": 0}
            
            r["run"] = run + 1
            results.append(r)
            with open(os.path.join(HERE, "results.jsonl"), "a") as f:
                f.write(json.dumps(r) + "\n")
            print(f"{'PASS' if r['passed'] else 'FAIL'} {name} run{run+1} steps={r['steps']} {r['reason']}")
    ok = sum(r["passed"] for r in results)
    print(f"{args.model}: {ok}/{len(results)} = {100 * ok / len(results):.0f}%")

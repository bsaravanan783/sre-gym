
import argparse, json, os, re, shutil, sys, tempfile, time
from agent import Agent, DEFAULT_POLICY, model_config
from mcp_client import MCPClient

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")
TASKS = os.path.join(HERE, "tasks")
BUCKET = "/srv/sre-work"                                # 1G ext4 loop mount (nixos-config hosts/laptop/default.nix)
TRANSCRIPTS = os.path.join(HERE, "results", "transcripts")

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

def keep_transcript_and_clean(workdir):
    """Copy the transcript out of the bucket, then delete the workdir. Returns the kept path or None."""
    src = os.path.join(workdir, "transcript.jsonl")
    kept = None
    if os.path.exists(src):                       # the agent may crash before writing one
        os.makedirs(TRANSCRIPTS, exist_ok=True)
        kept = os.path.join(TRANSCRIPTS, os.path.basename(workdir) + ".jsonl")
        shutil.copy(src, kept)
    shutil.rmtree(workdir)
    return kept

def run_task(task_dir, model, policy=None, server_args=(), prompt=None):
    task = json.load(open(os.path.join(task_dir, "task.json")))
    workdir = tempfile.mkdtemp(prefix="sre-eval-", dir=BUCKET)   # inside the 1G bucket, not the real disk
    try:
        shutil.copytree(os.path.join(task_dir, "files"), workdir, dirs_exist_ok=True)
        base = policy or DEFAULT_POLICY                                      # redteam.py passes its own policy
        policy =  ({**base, "rules": [("write_file", None, "allow")] + base["rules"]}
              if task["needs_write"] else base)                    # plus ("write_file", None, "allow") when needs_write
        old_cwd = os.getcwd()
        os.chdir(workdir)                       # transcript.jsonl + .outputs/ land here, not in the repo
        client = MCPClient( ([sys.executable, SERVER ,"--rw"] if task["needs_write"] else [sys.executable, SERVER]) + list(server_args), cwd=workdir)
        start = time.time()
        try:
            agent = Agent(model, model, max_steps=task["max_steps"], policy=policy, mcp=client,
                          tools=client.list_tools())            # write_file comes from the server, only with --rw
            out = agent.run(prompt or task["prompt"])                     # redteam.py may swap the prompt
        finally:
            client.close()
            os.chdir(old_cwd)
        passed, reason = grade(task, out.get("answer") or "", workdir)   # grade BEFORE the workdir is deleted
    finally:
        kept = keep_transcript_and_clean(workdir)                        # runs on pass, fail and crash
    return {"task": os.path.basename(task_dir), "model": model, "passed": passed, "reason": reason,
            "outcome": out["outcome"], "steps": out["steps"], "answer": out.get("answer"),
            "prompt_tokens": out["prompt_tokens"], "completion_tokens": out["completion_tokens"],
            "seconds": round(time.time() - start, 1), "transcript": kept}

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, choices=list(model_config))
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--only", help="run one task, e.g. 03")
    args = p.parse_args()
    tasks = sorted(d for d in os.listdir(TASKS) if os.path.isdir(os.path.join(TASKS, d)))
    
    if args.only:                              # loop: runs × sorted task dirs (filter by --only),
        tasks = [ t for t in tasks if t.startswith(args.only)]
    
    if not os.path.ismount(BUCKET):           # a failed mount leaves a plain folder on the real disk: no cap
        sys.exit(f"{BUCKET} is not mounted; refusing to run without the size cap")
    for name in os.listdir(BUCKET):           # leftovers from a run killed before its cleanup
        if name.startswith("sre-eval-"):      # only ours: lost+found belongs to the filesystem
            shutil.rmtree(os.path.join(BUCKET, name))

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

import httpx
import os
import json
import sys
import subprocess
import time
from tokenizers import Tokenizer
import uuid
import re

model_config = {
    "qwen-local" : {
        "provider": "local", "model": "qwen3:4b-instruct",
        "url": "http://localhost:11434/v1/chat/completions",
        "context_window": 8192,
        "tokenizer":"tokenizers/qwen3.json",
        "max_output_tokens": 800
        },
    "nemotron" : {
        "provider": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "nvidia/nemotron-3.5-lightning:free",
        "context_window":1000000,
        "max_output_tokens":32000
        } 
}

model_config["fake"] = {**model_config["qwen-local"], "provider": "fake"}



DEFAULT_POLICY = {
    "rules": [
        # (tool, pattern, decision); pattern is a regex on the command/path, None = any
        ("read_file", None, "allow"),
        ("write_file", None, "ask"),
        ("run_bash", r"^(ls|cat|grep|head|tail|wc)\b", "allow"),
        ("run_bash", r"\b(rm|sudo|curl|wget)\b", "deny"),
        ("delegate",None,"allow")
    ],
    "hooks":{"pre":[],"post":[]},
    "default": "ask",          # nothing matched
    "unattended": True,        # True → "ask" becomes "deny"
}


SYSTEM_PROMPT="You are an AI agent and Answer the question as asked; if you only saw part of the data, read the rest before answering"

SUMMARY_PROMPT = """You are writing a handover note for yourself. The conversation below will be deleted; only your note survives.
Include: Progress, findings with exact values and file paths , failures and rule outs , whats left to be done and next steps
Copy exact values (numbers, file names, errors) — never paraphrase them."""

MAX_ATTEMPTS = 4
RETRYABLE_STATUSES = [408, 425, 429, 500, 502, 503, 504]

clear_tool_call_config = {
    "max_allowed_context_percentage":70,
    "clear_down_context_percentage":35,
    "keep_recent_percentage": 20,
    "hard_limit_percentage": 90 
}


def check_permission(tool_name,args,policy):
    target = args.get("command") or args.get("path") or ""
    matched = [d for (t, pat, d) in policy["rules"]
               if t == tool_name and (pat is None or re.search(pat, target))]
    if "deny" in matched:
        return "deny"
    SHELL_META = set(";&|$`<>(){}\n")
    if tool_name == "run_bash" and any(ch in SHELL_META for ch in target):
        matched = [d for d in matched if d != "allow"]  # never auto-allow a compound command
    if "allow" in matched:
        return "allow"
    decision = "ask" if matched else policy["default"]
    if decision == "ask" and policy["unattended"]:
        return "deny"
    return decision


class Agent:
    def __init__(self , model_name , summary_model_name , max_steps=100 , system_prompt=SYSTEM_PROMPT,role="solo",parent_run_id=None,parent_step=None,tools=None,policy=None):
        self.model_name = model_name
        self.summary_model_name = summary_model_name
        self.cfg = model_config[model_name]
        self.summary_cfg = model_config[summary_model_name]
        self.max_steps = max_steps
        self.messages = [{"role":"system" , "content" : system_prompt}]
        self.step = 0
        self.outcome = None
        self.length_cuts = 0
        self.previous_prompt_tokens = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.last_prompt_tokens = None
        self.last_call_msg_len = 0
        self.run_id = uuid.uuid4().hex
        self.role = role
        self.parent_run_id = parent_run_id
        self.parent_step = parent_step
        self.tools = tools if tools is not None else WORKER_TOOLS
        self.policy = policy if policy is not None else DEFAULT_POLICY

    def run(self , task):
        self.messages.append({"role":"user" , "content":task})
        answer = None

        while(True):
            if self.step >= self.max_steps:
                print("Too many iterations")
                self.outcome = "STEP_CAP"
                break
            start_time = time.time()

            event = ""

            current_context_estimate = count_context_tokens(self.messages,self.last_prompt_tokens,self.last_call_msg_len,self.cfg,self.tools)
            model_context_window = self.cfg["context_window"]

            print(f"context estimate : {current_context_estimate} / {model_context_window}  ({ round(((current_context_estimate / model_context_window ) * 100),1) } )")
            
            if current_context_estimate >= ( model_context_window * (clear_tool_call_config["max_allowed_context_percentage"] / 100) ):
                event = "clear"
                tokens_after_clear = clear_old_tool_results(self.messages,current_context_estimate , self.last_call_msg_len , self.cfg)

                print(f"Cleared: from {current_context_estimate} to {tokens_after_clear} ")
                current_context_estimate = tokens_after_clear

            if current_context_estimate >= model_context_window * (clear_tool_call_config["max_allowed_context_percentage"]/100):
                event = "compact"
                print("called compact ")
                try:
                
                    new_summarised_messages = compact_with_summary(self.messages,self.cfg,self.summary_cfg)
                
                except LLMError as e:
                    print("Compaction failed :" , e)
                    log_turn({
                        "run_id":self.run_id,
                        "role":self.role,
                        "parent_run_id":self.parent_run_id,
                        "parent_step":self.parent_step,
                        "step":self.step,
                        "timestamp":time.time(),
                        "error" : f"compaction : {str(e)}"
                    })
                    self.outcome = "COMPACT_ERROR"
                    break
                self.messages = new_summarised_messages
                current_context_estimate = count_context_tokens(self.messages,None,0,self.cfg,self.tools)


            if current_context_estimate > model_context_window * (clear_tool_call_config["hard_limit_percentage"]/100):
                print(f"The tokens are at {current_context_estimate} and the hard limited context window of the model is {( model_context_window * (clear_tool_call_config["hard_limit_percentage"] / 100) )}")
                self.outcome="HARD_LIMITED"
                break

            self.last_call_msg_len = len(self.messages)

            try:
                start_timer = time.perf_counter()

                message , finish_reason , usage , body = llm_call(self.messages,self.cfg,self.tools)
            
                elapsed_time = (time.perf_counter() - start_timer) *1000
                
                end_time = time.time()
            
                duration = (end_time - start_time)*1000 

            except LLMError as e:
                print("Error: ", e)
                log_turn({
                "run_id":self.run_id,
                "role":self.role,
                "parent_run_id":self.parent_run_id,
                "parent_step":self.parent_step,
                "step":self.step,
                "timestamp":time.time(),
                "error" : str(e)
            })
                self.outcome = "LLM_ERROR"
                break
            
            log_turn({
                "run_id":self.run_id,
                "role":self.role,
                "parent_run_id":self.parent_run_id,
                "parent_step":self.parent_step,
                "step":self.step,
                "timestamp":time.time(),
                "latency_ms":round(duration),
                "request":body,
                "response":{"message":message,"finish_reason":finish_reason},
                "usage":usage,
            })
            if usage is not None:

                details = usage.get("prompt_tokens_details") or {}
                cached_tokens = details.get("cached_tokens")
                
                self.last_prompt_tokens = usage["prompt_tokens"]
                current_prompt_tokens = usage["prompt_tokens"]
                self.total_prompt_tokens += current_prompt_tokens
                self.total_completion_tokens += usage["completion_tokens"] 
                print(f"step : {self.step} | prompt tokens : {current_prompt_tokens}  | completion tokens : {usage['completion_tokens']} | Delta prompt tokens : {current_prompt_tokens-self.previous_prompt_tokens}")
                self.previous_prompt_tokens = current_prompt_tokens

                print(f"step {self.step} | {elapsed_time:.2f}ms | cached: {cached_tokens} | event : {event}")
            else:
                self.last_prompt_tokens = None

            if finish_reason == "length" and not message.get("tool_calls"):
                self.length_cuts+=1
                if self.length_cuts >= 3:
                    self.outcome = "LENGTH"
                    break
                self.messages.append(message)
                self.messages.append({"role":"user","content":f"Your reply was cut off at the output limit. Reply again, shorter."})
                self.step+=1
                continue

            self.length_cuts = 0  # on normal reply without length limit

            self.messages.append(message)
            
            tool_calls = message.get("tool_calls")
            if tool_calls:
                
                result = call_tools(tool_calls,self.cfg,self)

                # handling parralel tool calls 
                current_context = count_context_tokens(self.messages,self.last_prompt_tokens,self.last_call_msg_len,self.cfg ,self.tools)
                value1 = (model_context_window * clear_tool_call_config["max_allowed_context_percentage"] /100) - current_context
                value2 = clear_tool_call_config["keep_recent_percentage"]/100 * model_context_window
                budget = max(value1 , value2)
                used = 0
                is_budget_crossed_for_tool_calls = False
                shown_results = 0
                for i , res in enumerate(result):
                    token_estimate = estimate_tokens(json.dumps(res) , self.cfg)

                    if used + token_estimate <= budget and not is_budget_crossed_for_tool_calls:
                        used += token_estimate
                        shown_results = i+1
                    else:
                        is_budget_crossed_for_tool_calls = True
                        res["content"] = f"[not shown: this turn's results exceeded the budget. Full result is at .outputs/{res['tool_call_id']}.txt - call the tool again in a later turn to see it.]" 
                print(f"budget: {budget} used: {used} shown : ({shown_results}/{len(result)})")
                self.messages.extend(result)
                self.step+=1
            else:
                print(message["content"])
                answer = message["content"]
                self.outcome="ANSWERED"
                break
        
        return {"outcome":self.outcome , "answer":answer , "steps":self.step , "prompt_tokens" : self.total_prompt_tokens , "completion_tokens" : self.total_completion_tokens}

def count_context_tokens(messages,prompt_tokens,last_call_msg_len,cfg , tools):
    if prompt_tokens is None:
        return estimate_tokens(json.dumps(messages) , cfg) + estimate_tokens(json.dumps(tools) , cfg)
    
    new_messages = messages[last_call_msg_len:]

    return prompt_tokens + estimate_tokens(json.dumps(new_messages) , cfg)


def write_tool_result_to_file_system(text,call_id):
    project_path = os.path.realpath(os.getcwd())
    dir_path = os.path.join(project_path, ".outputs")
    os.makedirs(dir_path,exist_ok=True)
        
    with open(os.path.join(dir_path, f"{call_id}.txt"), "w") as file:
        n = file.write(text)

    return n

tokenizer_cache = {}
def estimate_tokens(text,cfg):
    path = cfg.get("tokenizer")
    if path is None:
        return len(text)
    full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
   
    if full_path not in tokenizer_cache:
        tokenizer_cache[full_path] = Tokenizer.from_file(full_path)
    return len(tokenizer_cache[full_path].encode(text).ids)

def cap_tool_result(text,call_id,cfg):
    model_context_size = cfg["context_window"]
    budget_context_size = int(model_context_size*0.15)

    estimated_tokens = estimate_tokens(text,cfg)

    n = write_tool_result_to_file_system(text,call_id)

    if estimated_tokens > budget_context_size:
        total_lines = text.count("\n")

        chars_per_token = len(text) / estimated_tokens
        budget_chars = int(budget_context_size * chars_per_token)
        head_chars = int(budget_chars * 0.4)
        tail_chars = int(budget_chars * 0.4)
        head = text[:head_chars]
        tail = text[-tail_chars:]
        head_end = head.rfind("\n")

        if head_end != -1:
            head = head[:head_end]

        tail_start = tail.find("\n")
        if tail_start != -1:
            tail = tail[tail_start + 1:]

        result = head + f"""\n[... OUTPUT TRUNCATED: {total_lines} lines, ~{estimated_tokens} estimated tokens total.
 Full output saved to .outputs/{call_id}.txt
 To see more: read_file with start_line/end_line, or run_bash with grep/sed -n/tail on that file ...] \n""" + tail
        
        return result

    else:
        return text
        
        

def clear_old_tool_results(messages,current_tokens,last_call_msg_len , cfg):
    tool_call_messages = []
    for i , message in enumerate(messages):
        if i < last_call_msg_len and message["role"] == "tool":
            tool_call_messages.append(message)
    
    model_context_window = cfg.get("context_window")
    keep_budget = model_context_window * ( clear_tool_call_config["keep_recent_percentage"] / 100 )
    kept_tokens = 0
    split = 0  # index where clear candidates end
    for j in range(len(tool_call_messages) - 1 , -1 , -1):
        kept_tokens += estimate_tokens(tool_call_messages[j]["content"] , cfg)
        if kept_tokens > keep_budget:
            split = j+1
            break
    


    candidates_for_clearing = tool_call_messages[:split]
    clear_down_context_percentage = model_context_window * (clear_tool_call_config["clear_down_context_percentage"] / 100)
    for message in candidates_for_clearing:
        if current_tokens <= clear_down_context_percentage:
            break

        if message["content"].startswith("[cleared"):
            continue

        placeholder_for_cleared_tool_calls = f"[cleared - full result at .outputs/{message["tool_call_id"]}.txt]"
        
        current_tokens -= ( estimate_tokens(message["content"] , cfg) - estimate_tokens(placeholder_for_cleared_tool_calls , cfg) ) 

        message["content"] = placeholder_for_cleared_tool_calls


    return current_tokens



def find_summary_cut(messages,cfg):
    # idea is finding the cut from index 2 to the recent allowed budget(20%) 2 -- middle -- recent and the recent middle is summarised
    keep_budget = cfg.get("context_window") * clear_tool_call_config["keep_recent_percentage"] / 100

    assistant_idxs = []
    for i , m in enumerate(messages):
        if i >=2 and m["role"] == "assistant":
            assistant_idxs.append(i)
    
    if len(assistant_idxs) ==0 :
        return None
    
    cut = None
    for j in assistant_idxs:
        tail_tokens = estimate_tokens(json.dumps(messages[j:]),cfg)
        if tail_tokens <= keep_budget:
            cut = j
            break
    
    if cut is None:
         cut = assistant_idxs[-1] # keep the last assistant msg alone
    
    if cut <= 2:
        return None  

    return cut

def render_for_summary(messages):
    # converts json to text for llm summary
    lines = []
    for m in messages:
        if m["role"] == "assistant":
            if m.get("content"):
                lines.append(f"ASSISTANT: {m['content']}")
            for tc in m.get("tool_calls") or []:
                lines.append(f"ASSISTANT called { tc['function']['name'] }({ tc['function']['arguments'] })")   # name + arguments
        elif m["role"] == "tool":
            lines.append(f"TOOL RESULT: {m['content']}")
        elif m["role"] == "user":
            lines.append(f"USER: {m['content']}")
    return "\n".join(lines)

def summarize(task_msg, old_msgs , summarize_cfg):
    if summarize_cfg["provider"] == "fake":
        return "FAKE_SUMMARY"
    
    transcript = render_for_summary(old_msgs)

    task = f"The task was : {task_msg} \n"

    request = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user",   "content": f"{task} {transcript} \n Write the handover note now, in plain text, with the 4 sections. Do not output JSON. "},
    ]

    result,finish_reason,usage,body = llm_call(request , summarize_cfg , tools = None)

    return result["content"]

def split_for_summary(old_msgs, budget, cfg):
    chunks, current, current_tokens = [], [], 0
    for m in old_msgs:
        t = estimate_tokens(render_for_summary([m]), cfg)
        if current and current_tokens + t > budget:
            chunks.append(current)
            current, current_tokens = [], 0
        current.append(m)
        current_tokens += t
    if current:
        chunks.append(current)
    return chunks

def compact_with_summary(messages,cfg,summarize_cfg):
    cut_idx = find_summary_cut(messages,cfg)
    if cut_idx is None:
        print("summary: nothing to summarize")
        return messages
    has_old_summary = (messages[2]["role"] == "user" )and (  (messages[2]["content"] or "").startswith("[Summary of earlier work]")  )
    start_idx = 3 if has_old_summary else 2
    
    if start_idx >= cut_idx:
        return messages

    budget =  (summarize_cfg["context_window"] - summarize_cfg["max_output_tokens"] - estimate_tokens(SUMMARY_PROMPT + messages[1]["content"], summarize_cfg) - 200 )

    chunks = split_for_summary(messages[start_idx:cut_idx], budget, summarize_cfg)
    parts = [summarize(messages[1]["content"], c, summarize_cfg) for c in chunks]
    new_part_to_add = "\n".join(parts)

    if has_old_summary:
        summary = f"{messages[2]["content"]} \n [Later work] \n { new_part_to_add }"
    else:
        summary = f"[Summary of earlier work]\n { new_part_to_add }"
    print(f"Summary : {summary}")

    new_summarised_messages = messages[:2]
    new_summarised_messages.append({"role":"user","content":summary})
    new_summarised_messages+= messages[cut_idx:]

    return new_summarised_messages


def log_turn(entry):
    try:
        with open("transcript.jsonl","a") as f:
            f.write(json.dumps(entry))
            f.write("\n")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

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

def truncate(text , limit = 3000):
    if len(text) <= limit:
        return text
    else:
        first_half = text[:limit//2]
        last_half = text[-limit//2:]
        return f"{first_half} \n[... {len(text) - limit} characters truncated ...]\n {last_half}"

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

def write_file(path,content):
    try:
        if not is_inside_project_path(path):
            return "Access denied: Path is outside the project folder."
            
        with open(path, "w") as file:
            n = file.write(content)
            return f"Wrote {n} characters to {path}"

    except PermissionError as pe:
        return f"Error: permission denied {pe}"

    except FileNotFoundError:
        return f"Error: The directory structure for '{path}' was not found."

    except Exception as e:
        return f"An unexpected error occurred: {e}"

def redact_secrets(name,args,result):
    return re.sub(r"sk-[A-Za-z0-9-]{20,}","[REDACTED]", result)

def call_tools(tools,cfg,parent=None):
    policy = parent.policy if parent is not None else DEFAULT_POLICY
    calls = {}          # tool_call_id -> (name, args), so post-hooks know which tool made each result
    result = []
    for tool in tools:
        try:
            arguments = tool["function"]["arguments"]
            dict_arguments = json.loads(arguments)
            if not isinstance(dict_arguments,dict):
                result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error: arguments were not valid JSON: {arguments}.Retry with valid JSON."})
                continue
        except json.JSONDecodeError:
            result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error: arguments were not valid JSON: {arguments}.Retry with valid JSON."})
            continue
        except Exception as e:
            result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error: arguments were not valid JSON: {arguments}.Retry with valid JSON."})
            continue


        name = tool["function"]["name"]
        calls[tool["id"]] = (name, dict_arguments)
        decision = check_permission(name, dict_arguments, policy)
        if decision == "ask":                       # only reachable when attended
            answer = input(f"\n>>> {name} {dict_arguments}\nAllow? (y/n): ")
            decision = "allow" if answer.strip().lower() == "y" else "deny"
        if decision == "deny":
            result.append({"role":"tool","tool_call_id":tool["id"],
            "content":f"Error: {name} denied by policy.Write plain commands without  ; & | $ > < or try a different tool"})
            continue


        blocked = None
        for hook in policy["hooks"]["pre"]:
            blocked = hook(name,dict_arguments)

            if blocked:
                break
        if blocked:
            result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error: blocked by hook: {blocked}"})
            continue



        if tool["function"]["name"] == "read_file":
            print ("read file tool called")
            try:
                res=read_file(dict_arguments["path"] , dict_arguments.get("start_line") , dict_arguments.get("end_line"))
                result.append({"role":"tool","tool_call_id":tool["id"],"content":res})
            except KeyError as e:
                result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error: missing required arguments {e}"})
        elif tool["function"]["name"] == "write_file":
            print ("write file tool called")
            try:
                res=write_file(dict_arguments["path"],dict_arguments["content"])
                result.append({"role":"tool","tool_call_id":tool["id"],"content":res})
            except KeyError as e:
                result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error: missing required arguments {e}"})

        elif tool["function"]["name"] == "run_bash":
            print("run bash tool called")
            try:
                command = dict_arguments["command"]
            except KeyError as e:
                result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error: missing required arguments {e}"})
                continue
            res = run_bash(command)
            result.append({"role":"tool","tool_call_id":tool["id"],"content":res})
                
        elif tool["function"]["name"] == "delegate":
            if parent is None or parent.role != "planner":
                result.append({"role":"tool","tool_call_id":tool["id"],"content":"Error: only the planner can delegate. Do the subtask yourself with your own tools."})           
                continue
                
            try:
                subtask = dict_arguments["subtask"]
            except KeyError as e:
                result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error:  missing required arguments {e}"})
                continue

            worker = Agent(parent.model_name , parent.summary_model_name,role="worker",parent_run_id=parent.run_id,parent_step=parent.step,policy=parent.policy)
            output = worker.run(subtask)
            parent.total_prompt_tokens += output["prompt_tokens"]
            parent.total_completion_tokens += output["completion_tokens"]

            if output["outcome"] == "ANSWERED":
                content = output["answer"]
            else:
                content = f"Worker failed ({output['outcome']}) after {output['steps']} steps . No answer . Retry with a smaller or clearer subtask."

            result.append({"role":"tool","tool_call_id":tool["id"],"content":content})
        else:
            result.append({"role":"tool","tool_call_id":tool["id"],"content":f"Error:Tool not found {tool['function']['name']}"})
    result_to_pass = []
    for res in result:
        content = res["content"]
        name, args = calls.get(res["tool_call_id"], (None, {}))
        for hook in policy["hooks"]["post"]:
            content = hook(name, args, content)
        result_to_pass.append({"role":res["role"], "tool_call_id":res["tool_call_id"],
                               "content":cap_tool_result(content, res["tool_call_id"], cfg)})

    return result_to_pass




WORKER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the current directory and return its contents.Large files are truncated and using start_line and end_line to page through the content in the file.Line numbers are 1-based and inclusive",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, e.g. notes.txt"},
                    "start_line": {"type": "integer" , "description": "First line to include (1-based, inclusive)."},
                    "end_line": {"type": "integer" , "description": "Last line to include (1-based, inclusive)."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file to the current directory and return the number of characters written along with the path.It overwrites the content in existing files.Always read a file with read_file before modifying it. Make minimal changes; do not rewrite whole files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, e.g. notes.txt"},
                    "content": {"type": "string" , "description": "Content inside the file , e.g. This text has been written in this file"}
                },
                "required": ["path","content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "This is a bash tool and runs in the project directory with a 30-second timeout and must be non-interactive with no input prompts.It returns exit code, stdout and stderr.When outputs are long it gets truncated.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run in bash , e.g. ls , cat"},
                },
                "required": ["command"]
            }
        }
    }
]

DELEGATE_TOOL = [{
    "type": "function",
    "function": {
        "name": "delegate",
        "description": "A worker with file/bash tools does the subtask in a fresh context and returns ONLY its final answer, so the subtask must say exactly what to report back",
        "parameters": {
            "type": "object",
            "properties": {"subtask": {"type": "string", "description": "the description prompt of the subtask given by a parent task"}},
            "required": ["subtask"]
        }
    }
}]


def _max20(k):
    with open(f"log{k}.txt") as f:
        return max(int(x) for x in f.readlines()[:20])


def get_fake_script():
    return [
    {"role": "assistant",
     "content": f"Read log{k}.txt lines 1-20. FINDING: largest in those lines = {_max20(k)}. " + ("padding " * 250),
     "tool_calls": [
         {"id": f"call_{k}", "type": "function",
          "function": {"name": "read_file",
                       "arguments": json.dumps({"path": f"log{k}.txt", "start_line": 1, "end_line": 20})}}]}
    for k in range(1, 9)
] + [{"role": "assistant", "content": "done"}]



FAKE_MODE = "chain"     # "script" = old 8-log script, "chain" = follows next: lines

chain_call_n = 0
def chain_fake(messages):
    global chain_call_n
    chain_call_n += 1
    reply = lambda text: ({"role": "assistant", "content": text}, "stop", None, {"messages": messages})

    # first call = the agent hasn't replied yet (derived from the messages, no global counter)
    first_call = not any(m["role"] == "assistant" for m in messages)

    # newest tool result that is still visible (not cleared / not a placeholder)
    latest = next((m for m in reversed(messages)
                   if m["role"] == "tool" and not (m.get("content") or "").startswith(("[cleared", "[not shown"))), None)
    if latest is None:
        start = re.search(r"reading (\S+\.txt)", messages[1]["content"])   # the task message
        if not first_call or not start:
            return reply("FAKE ERROR: lost the chain - no visible tool result left after clearing/compaction")
        path = start.group(1)
    else:
        content = latest["content"]
        final = re.search(r"FINAL CODE: (\S+)", content)
        if final:
            return  reply(f"The final code is {final.group(1)}")
        nxt = re.search(r"next: (\S+)", content)
        if not nxt:
            return reply("FAKE ERROR: newest visible result has no next:line")
        path = nxt.group(1)

    call = {"id": f"chain_{chain_call_n}", "type": "function",
"function": {"name": "read_file", "arguments": json.dumps({"path": path})}}

    return {"role": "assistant", "content": "", "tool_calls": [call]}, "tool_calls", None, {"messages": messages}


fake_step = 0
FAKE_SCRIPT = None
def fake_llm(messages):

    if FAKE_MODE == "chain":
        return chain_fake(messages)

    global fake_step , FAKE_SCRIPT
    if FAKE_SCRIPT is None:
        FAKE_SCRIPT = get_fake_script()
    message = FAKE_SCRIPT[fake_step]
    fake_step+=1
    finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
    return message , finish_reason , None, {"messages" : messages}

class LLMError(Exception):
    pass

def post_with_retries(url,body,header,timeout):
    
    for attempt in range(MAX_ATTEMPTS):
        retry_after = None
        try:
            response = httpx.post(url,headers=header,json=body,timeout=timeout)
        except (httpx.TimeoutException,httpx.ConnectError) as e:
            error = f"{type(e).__name__}"
        else:
            ra = response.headers.get("retry-after")
            if ra is not None and ra.isdigit():
                retry_after = int(ra)
            if response.status_code == 200:
                return response
            
            if response.status_code not in RETRYABLE_STATUSES:
                raise LLMError(f"Non retryable error : {response.status_code} and {response.text}")
            
            error = f"HTTP {response.status_code}"
        
        if attempt < MAX_ATTEMPTS -1:
            wait = retry_after if retry_after is not None else 2 ** attempt
            if wait > 60:
                err_msg = f"Server asked to wait {wait}s and {retry_after} , too long , giving up"
                raise LLMError(err_msg)
            print(f"LLM call failed ({error}) , retry {attempt+1} in {wait}s")
            time.sleep(wait)
    
    err_msg = f"LLM call failed after {MAX_ATTEMPTS} attempts and the last error messages is {error}"
    raise LLMError(err_msg)


def llm_call(messages,cfg,tools=None):

    if cfg["provider"] == "fake":
        return fake_llm(messages)

    header = { "Content-Type": "application/json"}
    
    if cfg["provider"] == "openrouter":
        header["Authorization"] = f"Bearer {os.environ['OPENROUTER_API_KEY']}"
    body = {"model":cfg["model"],"messages":messages,"max_tokens":cfg["max_output_tokens"]}

    if tools is not None:
        body["tools"] = tools

    response = post_with_retries(cfg["url"],body,header,cfg.get("timeout", 120))
    
    data = response.json()
    if data.get("choices") is not None:
        return data["choices"][0]["message"] , data["choices"][0]["finish_reason"], data.get("usage") , body
    else:
        raise LLMError(f"Error :{data.get("error", data)}")
        


if __name__ == "__main__":
    result = Agent("fake", "fake").run("Start by reading node_818_29.txt. Each file has a 'next:' line naming the next file. Follow the chain until you find the FINAL CODE line, then reply with that code.")
    print(result)

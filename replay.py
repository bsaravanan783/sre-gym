import httpx , json , sys

path = sys.argv[1]

records = [json.loads(l) for l in open(path)]

run_id = sys.argv[2] if len(sys.argv) > 2 else str(records[-1]["run_id"])
steps =  [ r for r in records if  str(r["run_id"]) == run_id]


def to_native(messages):
    # here we are converting the string json in arguments in function to actuall json for all the tool calls
    out = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
        #     for tool_call in m["tool_calls"]:
        #         function = tool_call["function"]
        #         arguments_json = function["arguments"]
        #         arguments = json.loads(arguments_json)
        #         function["arguments"] = arguments

            m["tool_calls"] = [
                {**c, "function": {**c["function"], "arguments": json.loads(c["function"]["arguments"])}}
                for c in m["tool_calls"]
            ]
        out.append(m)
    
    return out


def num_of_common_prefix(a,b):
    n = 0 
    for x , y in zip(a,b):
        if x!=y :
            break
        n+=1

    return n

prev = []
for r in steps:
    req = r["request"]
    body = {
        "model" : req.get("model", "qwen3:4b-instruct"),
        "messages" : to_native(req["messages"]),
        "tools":  req.get("tools", []),
        "stream": False,
        "options": {"num_predict":1}        
    }

    response = httpx.post("http://localhost:11434/api/chat", json=body, timeout=300).json()

    if "error" in response: 
        print(f"step {r['step']} | ERROR {response['error']}")
        continue

    eval_ms = response["prompt_eval_duration"] / 1000000

    same_prefix = num_of_common_prefix(prev,req["messages"])

    print(f"step {r['step']} | prompt {response['prompt_eval_count']} tok | eval {eval_ms:.0f} ms | same-prefix msgs {same_prefix}/{len(req['messages'])}")

    prev = req["messages"]


            

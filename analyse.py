import json


def estimate_tokens(text):
    return len(text) / 4


def calculate_token_breakdown(filename):
    entries = []

    # Read transcript
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))

    # Find latest run_id
    latest_run_id = entries[-1]["run_id"]

    # Keep only latest run
    run_entries = [
        entry for entry in entries
        if entry["run_id"] == latest_run_id
    ]

    # Last turn
    last_turn = run_entries[-1]

    request = last_turn["request"]
    messages = request.get("messages", [])
    tools = request.get("tools", [])

    buckets = {
        "system_prompt": 0,
        "user_task": 0,
        "assistant_messages": 0,
        "tool_results": 0,
        "tools_schema": 0,
    }

    # Process messages
    for message in messages:
        role = message.get("role")

        if role == "system":
            content = message.get("content", "")
            buckets["system_prompt"] += estimate_tokens(content)

        elif role == "user":
            content = message.get("content", "")
            buckets["user_task"] += estimate_tokens(content)

        elif role == "assistant":
            # Assistant content
            content = message.get("content", "")
            buckets["assistant_messages"] += estimate_tokens(content)

            # Assistant tool calls
            tool_calls = message.get("tool_calls", [])

            for tool_call in tool_calls:
                # Everything inside the tool call is text that contributes
                # to the request representation.
                buckets["assistant_messages"] += estimate_tokens(
                    json.dumps(tool_call, ensure_ascii=False)
                )

        elif role == "tool":
            content = message.get("content", "")
            buckets["tool_results"] += estimate_tokens(content)

    # Tools schema
    buckets["tools_schema"] = estimate_tokens(
        json.dumps(tools, ensure_ascii=False)
    )

    # Total estimated tokens
    total = sum(buckets.values())

    print(f"Run ID: {latest_run_id}")
    print(f"API prompt_tokens: {last_turn['usage']['prompt_tokens']}")
    print()

    print("Token breakdown:")

    for name, tokens in buckets.items():
        percentage = (tokens / total * 100) if total else 0

        print(
            f"{name:20} "
            f"{tokens:8.2f} tokens "
            f"({percentage:6.2f}%)"
        )

    print("-" * 50)
    print(f"{'Estimated total':20} {total:8.2f} tokens (100.00%)")


calculate_token_breakdown("transcript.jsonl")

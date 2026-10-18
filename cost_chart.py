"""Cost-per-task chart for one harness run.

usage: python cost_chart.py <transcript.jsonl> [run_id]  ->  writes cost_chart.html next to the transcript

Tokens are counted with the real Qwen3 tokenizer on each logged request (json.dumps(messages) + the
tool schemas), so fake-LLM runs (usage = None) get the same numbers the harness gauge sees.
Not included: the compaction summary calls (they are not logged in the transcript).
"""
import json, os, sys, html
from tokenizers import Tokenizer

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW = 8192
TRIGGER, GUARD = 0.70, 0.90

tok = Tokenizer.from_file(os.path.join(HERE, "tokenizers/qwen3.json"))
count = lambda obj: len(tok.encode(json.dumps(obj)).ids)

path = sys.argv[1]
records = [json.loads(l) for l in open(path) if l.strip()]
run_id = sys.argv[2] if len(sys.argv) > 2 else str(records[-1]["run_id"])
run = [r for r in records if str(r["run_id"]) == run_id]

# fake runs don't log tools: borrow the schema from any record that has it (same harness, same tools)
tools = next((r["request"]["tools"] for r in records if r["request"].get("tools")), None)
if tools is None:
    t1 = os.path.join(HERE, "task1/transcript.jsonl")
    if os.path.exists(t1):
        tools = next((json.loads(l)["request"].get("tools") for l in open(t1) if '"tools"' in l), None)
tools_tok = count(tools) if tools else 0

rows, naive, paid, prev_len, prev_tok = [], 0, 0, 0, 0
for i, r in enumerate(run):
    msgs = r["request"]["messages"]
    in_window = count(msgs) + tools_tok
    appended = count(msgs[-2:]) if i else in_window          # newest call + result, always shown in full
    naive = in_window if i == 0 else naive + appended        # prompt size if nothing were ever removed
    paid += in_window
    event = ""
    if i and len(msgs) < prev_len:
        event = "compact"
    elif i and in_window < prev_tok + appended - 50:
        event = "clear"
    rows.append(dict(step=r["step"], in_window=in_window, naive=naive, paid=paid, event=event))
    prev_len, prev_tok = len(msgs), in_window

naive_paid = 0
for row in rows:
    naive_paid += row["naive"]
    row["naive_paid"] = naive_paid

n = len(rows)
peak = max(r["in_window"] for r in rows)
clears = sum(r["event"] == "clear" for r in rows)
compacts = sum(r["event"] == "compact" for r in rows)
first_over = next((r["step"] for r in rows if r["naive"] > WINDOW), None)


def svg_chart(series, ymax, hlines, height=260, markers=False):
    W, H, L, R, T, B = 900, height, 64, 16, 14, 30
    pw, ph = W - L - R, H - T - B
    x = lambda i: L + pw * i / max(n - 1, 1)
    y = lambda v: T + ph * (1 - v / ymax)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" preserveAspectRatio="none">']
    for k in range(5):                                       # recessive grid + y labels
        v = ymax * k / 4
        out.append(f'<line class="grid" x1="{L}" x2="{W-R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(f'<text class="axis" x="{L-8}" y="{y(v)+4:.1f}" text-anchor="end">{v/1000:.0f}k</text>')
    for v, label in hlines:
        out.append(f'<line class="ref" x1="{L}" x2="{W-R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(f'<text class="axis" x="{W-R-4}" y="{y(v)-5:.1f}" text-anchor="end">{label}</text>')
    for k in range(0, n, 10):
        out.append(f'<text class="axis" x="{x(k):.1f}" y="{H-8}" text-anchor="middle">{rows[k]["step"]}</text>')
    for key, cls in series:
        pts = " ".join(f"{x(i):.1f},{y(min(r[key], ymax)):.1f}" for i, r in enumerate(rows))
        out.append(f'<polyline class="{cls}" points="{pts}"/>')
    if markers:
        for i, r in enumerate(rows):
            if r["event"]:
                out.append(f'<circle class="m-{r["event"]}" cx="{x(i):.1f}" cy="{y(r["in_window"]):.1f}" r="4.5"/>')
    out.append(f'<line class="cross" x1="0" x2="0" y1="{T}" y2="{H-B}" visibility="hidden"/>')
    out.append(f'<rect class="hit" x="{L}" y="{T}" width="{pw}" height="{ph}" data-l="{L}" data-pw="{pw}" data-w="{W}"/>')
    out.append("</svg>")
    return "".join(out)


chart1 = svg_chart([("in_window", "s1")], WINDOW, [(WINDOW * TRIGGER, "70% trigger"), (WINDOW * GUARD, "90% guard")],
                   markers=True)
ymax2 = max(rows[-1]["naive_paid"], rows[-1]["paid"]) * 1.05
chart2 = svg_chart([("naive_paid", "s2 dashed"), ("paid", "s1")], ymax2, [])

table = "".join(
    f"<tr><td>{r['step']}</td><td>{r['in_window']:,}</td><td>{r['in_window']/WINDOW:.0%}</td>"
    f"<td>{r['paid']:,}</td><td>{r['naive']:,}</td><td>{r['event']}</td></tr>" for r in rows)

page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cost per task</title>
<style>
:root {{ color-scheme: light; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --grid:#e6e5e1;
  --s1:#2a78d6; --s2:#eb6834; --clear:#1baf7a; --compact:#6250d6; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --surface:#1a1a19; --ink:#f0efec;
  --ink2:#c3c2b7; --grid:#2e2e2c; --s1:#3987e5; --s2:#d95926; --clear:#199e70; --compact:#9085e9; }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --grid:#2e2e2c;
  --s1:#3987e5; --s2:#d95926; --clear:#199e70; --compact:#9085e9; }}
body {{ margin:0; background:var(--surface); color:var(--ink); font:14px/1.45 system-ui,sans-serif; }}
main {{ max-width:960px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:28px 0 2px; }}
.sub {{ color:var(--ink2); margin:0 0 16px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }}
.tile {{ border:1px solid var(--grid); border-radius:8px; padding:10px 12px; }}
.tile b {{ display:block; font-size:22px; }} .tile span {{ color:var(--ink2); font-size:12px; }}
.wrap {{ position:relative; }} svg {{ width:100%; height:auto; display:block; }}
.grid {{ stroke:var(--grid); stroke-width:1; }} .ref {{ stroke:var(--ink2); stroke-width:1; stroke-dasharray:4 4; }}
.axis {{ fill:var(--ink2); font-size:11px; }}
polyline {{ fill:none; stroke-width:2; stroke-linejoin:round; }} .s1 {{ stroke:var(--s1); }} .s2 {{ stroke:var(--s2); }}
.dashed {{ stroke-dasharray:6 4; }}
.m-clear {{ fill:var(--clear); stroke:var(--surface); stroke-width:2; }}
.m-compact {{ fill:var(--compact); stroke:var(--surface); stroke-width:2; }}
.cross {{ stroke:var(--ink2); stroke-width:1; }} .hit {{ fill:transparent; }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; color:var(--ink2); font-size:12px; margin:4px 0 6px; }}
.key {{ display:inline-block; width:14px; height:3px; vertical-align:middle; margin-right:6px; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; vertical-align:middle; margin-right:6px; }}
.tip {{ position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--grid); border-radius:6px;
  padding:6px 8px; font-size:12px; white-space:nowrap; display:none; box-shadow:0 2px 8px rgba(0,0,0,.12); }}
details {{ margin-top:24px; }} table {{ border-collapse:collapse; font-size:12px; width:100%; }}
td,th {{ border-bottom:1px solid var(--grid); padding:3px 8px; text-align:right; }}
.note {{ color:var(--ink2); font-size:12px; margin-top:12px; }}
</style></head><body><main>
<h1>Cost per task: 55-file chain</h1>
<p class="sub">Run {html.escape(run_id)} · {n} steps · {WINDOW:,}-token window · tokens counted with the Qwen3 tokenizer</p>
<div class="tiles">
<div class="tile"><b>{n}</b><span>steps, finished</span></div>
<div class="tile"><b>{peak/WINDOW:.0%}</b><span>peak in-window ({peak:,} tok)</span></div>
<div class="tile"><b>{rows[-1]['paid']:,}</b><span>prompt tokens paid, whole task</span></div>
<div class="tile"><b>{clears} / {compacts}</b><span>clears / compactions</span></div>
</div>
<h2>In-window prompt per step</h2>
<div class="legend"><span><i class="key" style="background:var(--s1)"></i>prompt sent</span>
<span><i class="dot" style="background:var(--clear)"></i>clearing</span><span><i class="dot" style="background:var(--compact)"></i>compaction</span></div>
<div class="wrap" data-chart="1">{chart1}<div class="tip"></div></div>
<p class="note">Without removal the prompt would pass the window at step {first_over} and reach {rows[-1]['naive']:,} tokens ({rows[-1]['naive']/WINDOW:.1f}× the window).</p>
<h2>Cumulative prompt tokens paid</h2>
<div class="legend"><span><i class="key" style="background:var(--s1)"></i>with clearing + compaction</span>
<span><i class="key" style="background:var(--s2)"></i>if nothing were removed (and the window were infinite)</span></div>
<div class="wrap" data-chart="2">{chart2}<div class="tip"></div></div>
<p class="note">Paid {rows[-1]['paid']:,} vs {rows[-1]['naive_paid']:,} without removal ({rows[-1]['naive_paid']/rows[-1]['paid']:.1f}× less).
Not counted: compaction summary calls (not in the transcript) and prompt caching (Ollama only shows it as time).</p>
<details><summary>Table</summary><table><tr><th>step</th><th>in-window</th><th>%</th><th>paid so far</th><th>no-removal</th><th>event</th></tr>{table}</table></details>
</main>
<script>
const rows = {json.dumps(rows)};
document.querySelectorAll('.wrap').forEach(w => {{
  const svg = w.querySelector('svg'), hit = svg.querySelector('.hit'), cross = svg.querySelector('.cross'), tip = w.querySelector('.tip');
  const L = +hit.dataset.l, PW = +hit.dataset.pw, W = +hit.dataset.w, which = w.dataset.chart;
  hit.addEventListener('mousemove', e => {{
    const b = svg.getBoundingClientRect(), sx = (e.clientX - b.left) * W / b.width;
    const i = Math.max(0, Math.min(rows.length - 1, Math.round((sx - L) / PW * (rows.length - 1)))), r = rows[i];
    const cx = L + PW * i / (rows.length - 1);
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('visibility', 'visible');
    tip.innerHTML = which === '1'
      ? `step ${{r.step}}<br>in-window <b>${{r.in_window.toLocaleString()}}</b> (${{Math.round(r.in_window / {WINDOW} * 100)}}%)${{r.event ? '<br>' + r.event : ''}}`
      : `step ${{r.step}}<br>paid <b>${{r.paid.toLocaleString()}}</b><br>no removal ${{r.naive_paid.toLocaleString()}}`;
    tip.style.display = 'block';
    const px = cx * b.width / W; tip.style.left = Math.min(px + 12, b.width - tip.offsetWidth) + 'px'; tip.style.top = '8px';
  }});
  hit.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; cross.setAttribute('visibility', 'hidden'); }});
}});
</script></body></html>"""

out = os.path.join(os.path.dirname(os.path.abspath(path)), "cost_chart.html")
open(out, "w").write(page)
print(f"{n} steps | peak {peak} ({peak/WINDOW:.0%}) | paid {rows[-1]['paid']:,} vs no-removal {rows[-1]['naive_paid']:,} | "
      f"clears {clears} compacts {compacts} | naive passes window at step {first_over} -> {out}")

"""M0.2 spike: can the model do multi-turn tool calling? Fake data only."""
import re, sys, time
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama

MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3:8b"
RUNS = 5
HITS = {"H1": "Supplier A won 5 of 6 awards.", "H2": "Award value is far above peers.", "H3": "Two awards share the same date."}
recorded = []

@tool
def search_hits(buyer: str) -> dict:
    """List integrity-indicator hits for a buyer. Returns hit ids and claims."""
    return {"buyer": buyer, "hits": [{"hit_id": k, "claim": v} for k, v in HITS.items()]}

@tool
def record_finding(hit_id: str, severity: str, rationale: str) -> str:
    """Record a finding for one hit. severity is low, medium or high. rationale must contain no digits."""
    recorded.append((hit_id, severity, rationale))
    return "recorded"

TOOLS = {"search_hits": search_hits, "record_finding": record_finding}
SYSTEM = ("You screen procurement records. Call search_hits once for the buyer. "
          "Then call record_finding exactly once for EVERY hit, using its hit_id. "
          "The rationale must be one short sentence with no numbers. When all are recorded, reply DONE.")

def one_run():
    recorded.clear()
    llm = ChatOllama(model=MODEL, temperature=0, num_ctx=16384, reasoning=False).bind_tools(list(TOOLS.values()))
    msgs = [SystemMessage(SYSTEM), HumanMessage("Screen the buyer 'Test County'.")]
    for _ in range(10):
        ai = llm.invoke(msgs)
        msgs.append(ai)
        if not ai.tool_calls:
            break
        for c in ai.tool_calls:
            out = TOOLS[c["name"]].invoke(c["args"]) if c["name"] in TOOLS else "unknown tool"
            msgs.append(ToolMessage(content=str(out), tool_call_id=c["id"]))
    ids = [r[0] for r in recorded]
    ok = (sorted(ids) == sorted(HITS) and len(ids) == len(set(ids))
          and all(r[1] in ("low", "medium", "high") for r in recorded)
          and all(not re.search(r"\d", r[2]) for r in recorded)
          and not ai.tool_calls)
    return ok, recorded[:]

good = 0
for i in range(1, RUNS + 1):
    t = time.time()
    try:
        ok, rec = one_run()
    except Exception as e:
        ok, rec = False, [repr(e)]
    good += ok
    print(f"run {i}: {'PASS' if ok else 'FAIL'}  {time.time()-t:.0f}s  {rec}")
print(f"\n{MODEL}: {good}/{RUNS} valid sequences")

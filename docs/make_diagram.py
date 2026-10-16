"""Draws docs/flagbox_architecture.png (the architecture diagram for the submission form)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 2000, 1300
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "arialbd.ttf" if bold else "arial.ttf"
    return ImageFont.truetype(name, size)


def box(xy: tuple[int, int, int, int], title: str, lines: list[str], fill: str, edge: str) -> None:
    x0, y0, x1, y1 = xy
    d.rounded_rectangle(xy, 18, fill=fill, outline=edge, width=4)
    d.text((x0 + 18, y0 + 12), title, font=font(30, True), fill="#111")
    for i, line in enumerate(lines):
        d.text((x0 + 18, y0 + 56 + i * 32), line, font=font(24), fill="#222")


def arrow(a: tuple[int, int], b: tuple[int, int], label: str = "", dash: bool = False) -> None:
    d.line([a, b], fill="#333", width=4)
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = max((dx * dx + dy * dy) ** 0.5, 1)
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    tip = b
    p1 = (tip[0] - 22 * ux + 10 * px, tip[1] - 22 * uy + 10 * py)
    p2 = (tip[0] - 22 * ux - 10 * px, tip[1] - 22 * uy - 10 * py)
    d.polygon([tip, p1, p2], fill="#333")
    if label:
        d.text(((a[0] + b[0]) / 2 + 8, (a[1] + b[1]) / 2 - 34), label, font=font(22), fill="#444")


d.text((40, 24), "Flagbox architecture: one agent, two MCP servers, one human gate",
       font=font(44, True), fill="#111")
d.text((40, 84), "Runs on one laptop. No external APIs. Public PPRA award data only.",
       font=font(26), fill="#444")

# Left column
box((40, 170, 420, 330), "Officer (human)",
    ["Names one buyer + dates", "Reads the memo", "Approves or rejects"], "#fff3d6", "#c98a00")
box((40, 800, 420, 1010), "State (SQLite)",
    ["run_state.sqlite: runs, hits,", "findings, memos, approvals", "checkpoints.sqlite (LangGraph)"],
    "#eee", "#777")

# Agent
box((540, 170, 1100, 760), "Agent  (LangGraph, Qwen3 8B via Ollama)",
    ["check_coverage  - which data is usable",
     "plan            - choose indicators",
     "investigate     - call tools in a loop",
     "verify          - code checks every claim",
     "draft           - memo from template",
     "human_gate      - pauses (interrupt)",
     "decide  ->  file (code only)",
     "",
     "Model picks: hit_id, severity,",
     "reason_code from a closed list.",
     "Limits: 25 calls, 15 min."], "#e4f0ff", "#2b6cb0")

box((540, 800, 1100, 1010), "Logging wrapper (MCP client side)",
    ["Every tool call from both servers", "Hash-chained log: logs/<run_id>.jsonl",
     "Model never sees the filesystem tool"], "#f3e8ff", "#7a3fb0")

# Right column
box((1220, 170, 1960, 430), "Our MCP server: flagbox (stdio)",
    ["find_buyer, data_coverage,", "fetch_buyer_awards, repeat_winner,",
     "record_finding, draft_memo",
     "Claims come from fixed templates;", "thresholds from config/indicators.yaml"],
    "#e6f7ea", "#2f855a")
box((1220, 480, 1960, 650), "Data source (read-only SQLite snapshot)",
    ["PPRA OCDS awards, 2023-2025 (OCP registry)", "No live tenders; no personal data"],
    "#eee", "#777")
box((1220, 720, 1960, 900), "Borrowed MCP server: Filesystem",
    ["server-filesystem 2026.8.31 (node)", "Restricted to filed/. Only code calls", "write_file, after hash-bound approval"],
    "#e6f7ea", "#2f855a")
box((1220, 960, 1960, 1090), "filed/ folder",
    ["memo-<hash>.md, byte-identical to the", "memo the human approved (SHA-256)"], "#eee", "#777")

arrow((420, 250), (540, 250), "buyer")
arrow((540, 310), (420, 310), "memo")
arrow((1100, 300), (1220, 300), "MCP")
arrow((1590, 430), (1590, 480))
arrow((1100, 810), (1220, 810), "write")
arrow((820, 760), (820, 800))
arrow((540, 900), (420, 900), "log")
arrow((1590, 900), (1590, 960))

d.text((40, 1180), "Rules: never awards or accuses (wording: warrants review). No source, no flag. "
       "Only code writes files, only after a named human approves the memo's SHA-256.",
       font=font(24), fill="#222")

out = Path(__file__).with_name("flagbox_architecture.png")
img.save(out)
print(out)

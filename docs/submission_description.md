# Flagbox: submission description

*Draft for the application form. About 300 words. Edit freely: it is a starting point, not a statement of record.*

**Theme:** Governance. **Institution served:** the small audit and oversight units of Kenyan public bodies, such as county internal audit offices, assembly oversight committees and ministry audit units, each of which reviews one procuring entity at a time.

**The problem.** Kenya publishes its tenders and awards as open data, but the offices that should read them are too small to do so. Warning signs go unchecked until long after the money is spent, and a manual spreadsheet review of one buyer takes days.

**What Flagbox does.** An officer names a buyer and a date range. A local AI agent, running on open-weights Qwen3 through Ollama on the officer's own laptop, checks which indicators have enough usable data, runs them against the real PPRA award records, and decides which hits to keep and how severe each is. It then drafts a memo in which every finding links to the exact source record (file, line, OCID and the portal page). A named human reads the memo, then approves or rejects it. Only an approval tied to the memo's SHA-256 files the document.

**Why it can be trusted.** The model never writes a number, a name or an explanation: claims come from fixed templates inside our own MCP server, and the model only picks a hit, a severity and a reason from a closed list. Every tool call goes through one logging wrapper that writes a hash-chained log, so an edited log is detectable. The borrowed Filesystem MCP server is the only thing that can write to the filing folder, and only code, never the model, calls it. Personal data is dropped, supplier names are shown only for registered companies, and live tenders are excluded.

**What it is not.** It never awards, recommends or accuses; its wording is "warrants review". It ships one indicator, `repeat_winner`, plus a registry that makes adding another a one-file change. Its thresholds have not been reviewed by a procurement practitioner, and the README lists its limits plainly.

**Built with:** Python 3.12, LangGraph, the Model Context Protocol (our own server plus the borrowed Filesystem server), Ollama with Qwen3 8B, SQLite. Open source under the MIT licence.

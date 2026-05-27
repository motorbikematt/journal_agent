# Gemini Journaling & Archiving Agent

A generic, project-agnostic CLI tool to scan development logs, memories, and Git history, using the Gemini API to automatically synthesize activity and maintain a chronological project journal.

## Key Features

1. **Procedural Pre-Processing:** Reads verbose, token-heavy `.jsonl` session files (like Claude Code transcripts) and filters out large file snapshots, system events, and repetitive commands, reducing input token size by up to 95%.
2. **Multi-Source Context Scan:** Analyzes recent Git commits, markdown memory files (e.g. `memory/*.md`), notes, and active workspace changes.
3. **Chronological Integration:** Integrates synthesized session entries directly into a monolithic journal file (e.g., `project_journal.md`) in correct chronological date order.
4. **Key Security:** Prompts securely at runtime using masked CLI inputs if the `GEMINI_API_KEY` is not set. Keys are never saved to disk or repository files.
5. **Cost Safety/Budget Estimator:** Estimates inputs in characters/tokens prior to execution, calculates predicted Gemini API costs, and requests manual confirmation (`[Y/N]`) before invoking the API.

---

## Installation

1. Clone or copy this repository into your workspace environment (e.g. `D:/vibe/journal_agent`).
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

---

## Command Line Usage

Run the agent from your project directory:

```bash
python journal_agent.py [options]
```

### Options

*   `--logs-dir PATH`: Directory containing `.jsonl` session log files. If omitted, the script searches `.claude/projects/` or default CLI paths matching the current working directory name.
*   `--journal-path PATH`: The path to the markdown journal file (e.g. `docs/journal/project_journal.md`).
*   `--memory-dir PATH`: Optional folder containing assistant markdown memory files (e.g. `memory/`).
*   `--model NAME`: The Gemini API model to use. Defaults to `gemini-1.5-flash` for fast, cost-effective summarization.
*   `--dry-run`: Performs extraction, structures the payload, displays the token/budget estimate, and runs the LLM query without modifying the journal markdown.
*   `--force`: Reprocesses all session logs in the target directory, ignoring whether they have already been indexed in the journal document.
*   `--verbose`: Outputs detailed step logs and printouts of the payload being constructed.

---

## Design and Safety Protocols

### API Key Isolation
The script relies on environment variables or interactive user prompts:
```bash
# Set environment key temporarily
export GEMINI_API_KEY="your-api-key-here"
```
If not set, the agent prompts for input. It does not support `.env` files inside the repository to eliminate accidental key commits.

### Offline Budget Thresholds
The tool approximates tokens:
*   **English text conversion:** `1 token ≈ 4 characters`.
*   **Cost rates:** Employs Gemini 1.5 Flash API rates ($0.075 / 1M input tokens, $0.30 / 1M output tokens).
*   If the estimated cost is calculated, the CLI blocks execution and requires input:
    `Estimated API Call Cost: $0.0034. Proceed? (y/n):`

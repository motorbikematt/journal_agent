#!/usr/bin/env python3
"""
Gemini Journaling & Archiving Agent

A generic CLI tool that parses development logs (.jsonl files), memory documents,
and git history. It leverages the Gemini API to summarize sessions and merge
them chronologically into a monolithic project journal.

Designed for local-first parsing and safety, including pre-flight token & cost budget
estimators and interactive credential prompting.
"""

import os
import sys
import json
import re
import argparse
import getpass
import subprocess
from pathlib import Path
from datetime import datetime

# -----------------------------------------------------------------------------
# Pricing and Token Constraints (Gemini 1.5/2.5 Flash Pricing)
# -----------------------------------------------------------------------------
CHAR_TO_TOKEN_RATIO = 4  # Rule of thumb: 1 token ≈ 4 characters
INPUT_COST_PER_1M_TOKENS = 0.075  # USD
OUTPUT_COST_PER_1M_TOKENS = 0.300  # USD
ESTIMATED_OUTPUT_TOKENS_PER_SESSION = 1000  # Output length heuristic


def log_verbose(msg, verbose=False):
    """Prints status messages to stderr if verbose mode is enabled."""
    if verbose:
        print(f"[INFO] {msg}", file=sys.stderr)


# -----------------------------------------------------------------------------
# Dependency Validation
# -----------------------------------------------------------------------------
def check_dependencies():
    """Validates that the required google-generativeai package is installed."""
    try:
        import google.generativeai as genai
    except ImportError:
        print("[-] Error: The 'google-generativeai' library is not installed.", file=sys.stderr)
        print("    Please run: pip install google-generativeai", file=sys.stderr)
        sys.exit(1)


# -----------------------------------------------------------------------------
# Config & API Key Retrieval
# -----------------------------------------------------------------------------
def get_gemini_api_key(required=True):
    """
    Retrieves the Gemini API key from environment variables or prompts the user
    securely at runtime using masked input.
    """
    key = os.environ.get("GEMINI_API_KEY")
    if not key and required:
        print("[!] GEMINI_API_KEY environment variable not found.")
        try:
            key = getpass.getpass("    Enter your Gemini API Key: ").strip()
        except Exception:
            # Fallback for non-interactive stdin
            print("[-] Error: Non-interactive environment and no GEMINI_API_KEY environment variable set.", file=sys.stderr)
            sys.exit(1)
    return key


# -----------------------------------------------------------------------------
# Logs Auto-Discovery & Parsing
# -----------------------------------------------------------------------------
def auto_discover_logs_dir(cwd_name):
    """
    Attempts to auto-locate Claude CLI projects logs directory.
    Searches ~/.claude/projects/ matching CWD name.
    """
    home = Path.home()
    claude_projects_dir = home / ".config" / "claude" / "projects"
    # Fallback paths for Claude Code on Windows/Mac/Linux
    fallback_paths = [
        home / ".claude" / "projects",
        home / "AppData" / "Roaming" / "claude" / "projects",
        home / "Library" / "Application Support" / "claude" / "projects"
    ]
    
    for base_dir in [claude_projects_dir] + fallback_paths:
        if base_dir.exists() and base_dir.is_dir():
            # Try to match CWD folder names in the configuration path
            for child in base_dir.iterdir():
                if child.is_dir() and cwd_name.lower() in child.name.lower():
                    return child
    return None


def clean_prompt_message(message_str):
    """
    Parses and cleans JSON/dict representations of messages inside logs to 
    strip developer warning caveats, tool results, and structural metadata
    to save token volume.
    """
    try:
        # Check if it's a string representation of a dict
        if message_str.startswith("{") or message_str.startswith("["):
            try:
                data = json.loads(message_str)
            except json.JSONDecodeError:
                # Fallback to safe eval if valid dict format
                import ast
                data = ast.literal_eval(message_str)
            
            if isinstance(data, dict):
                content = data.get("content", "")
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    return "\n".join(text_parts).strip()
                return str(content).strip()
    except Exception:
        pass
    
    # Strip caveat markers from CLI wrapper
    clean = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "", message_str, flags=re.DOTALL)
    clean = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"<command-name>.*?</command-name>", "", clean, flags=re.DOTALL)
    return clean.strip()


def pre_process_jsonl(log_path, verbose=False):
    """
    Reads a raw JSONL log file line-by-line and filters out heavy payload entries
    such as file-history-snapshots and file write buffers.
    
    Returns a list of clean interaction dicts and min/max timestamps.
    """
    log_verbose(f"Pre-processing log file: {log_path.name}", verbose)
    interactions = []
    timestamps = []
    
    with open(log_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, start=1):
            if not line.strip():
                continue
            
            # CRITICAL OPTIMIZATION: Avoid json.loads on massive snapshot/attachment lines
            if '"type":"file-history-snapshot"' in line or '"type":"attachment"' in line:
                continue
                
            try:
                data = json.loads(line)
                
                # Keep track of timestamps
                ts = data.get("timestamp") or data.get("time") or data.get("createdAt")
                if ts:
                    timestamps.append(ts)
                
                t = data.get("type")
                # Skip heavy payloads to save context window and budget
                if t in ["file-history-snapshot", "attachment", "permission-mode", "last-prompt"]:
                    continue
                
                msg = data.get("message")
                if msg:
                    msg_str = str(msg)
                    if t == "user":
                        cleaned = clean_prompt_message(msg_str)
                        if cleaned:
                            interactions.append({"role": "user", "content": cleaned})
                    elif t == "assistant":
                        # Assistant messages could contain code tool runs
                        cleaned = clean_prompt_message(msg_str)
                        if cleaned:
                            interactions.append({"role": "assistant", "content": cleaned})
                
                # Capture tool executions in a compact format
                if t in ["tool_use", "tool-use", "call"]:
                    tool_name = data.get("toolName") or data.get("name")
                    args = data.get("arguments") or data.get("args")
                    interactions.append({"role": "system", "content": f"[Tool Use: {tool_name} with args: {args}]"})
                    
            except Exception as e:
                pass
                
    if timestamps:
        timestamps.sort()
        start_dt = timestamps[0]
        end_dt = timestamps[-1]
    else:
        start_dt = end_dt = "Unknown"
        
    return {
        "session_id": log_path.stem,
        "start_time": start_dt,
        "end_time": end_dt,
        "interactions": interactions
    }


# -----------------------------------------------------------------------------
# Secondary Source Scanners (Git & Memory)
# -----------------------------------------------------------------------------
def scan_git_history(workspace_path):
    """Gathers recent Git commit history inside the workspace."""
    try:
        res = subprocess.run(
            ["git", "log", "-n", "15", "--oneline"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            check=True
        )
        return res.stdout.strip()
    except Exception:
        return "No git repository found or git command failed."


def scan_memory_dir(memory_dir_path, verbose=False):
    """Concatenates all memory markdown files for development context."""
    if not memory_dir_path or not os.path.exists(memory_dir_path):
        return ""
    
    log_verbose(f"Scanning memory directory: {memory_dir_path}", verbose)
    p = Path(memory_dir_path)
    memory_content = []
    
    for file in sorted(p.glob("*.md")):
        try:
            content = file.read_text(encoding="utf-8")
            memory_content.append(f"--- File: {file.name} ---\n{content}\n")
        except Exception:
            pass
            
    return "\n".join(memory_content)


# -----------------------------------------------------------------------------
# Budget Estimation
# -----------------------------------------------------------------------------
def estimate_cost(sessions_data, memory_text, git_text):
    """
    Computes an offline token and cost estimation for the proposed inputs
    to ensure no unexpected billing occurs.
    """
    total_characters = len(memory_text) + len(git_text)
    for session in sessions_data:
        for item in session["interactions"]:
            total_characters += len(item["content"])
            
    estimated_input_tokens = total_characters // CHAR_TO_TOKEN_RATIO
    estimated_output_tokens = len(sessions_data) * ESTIMATED_OUTPUT_TOKENS_PER_SESSION
    
    input_cost = (estimated_input_tokens / 1_000_000) * INPUT_COST_PER_1M_TOKENS
    output_cost = (estimated_output_tokens / 1_000_000) * OUTPUT_COST_PER_1M_TOKENS
    total_cost = input_cost + output_cost
    
    return {
        "characters": total_characters,
        "input_tokens": estimated_input_tokens,
        "output_tokens": estimated_output_tokens,
        "estimated_cost_usd": total_cost
    }


# -----------------------------------------------------------------------------
# Journal Parsing & Integration
# -----------------------------------------------------------------------------
def get_recorded_sessions(journal_path):
    """
    Scans the journal document to extract already recorded Session IDs
    (UUID filenames matching .jsonl format) to prevent double journaling.
    """
    if not os.path.exists(journal_path):
        return set()
        
    content = Path(journal_path).read_text(encoding="utf-8")
    session_ids = set(re.findall(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", content))
    return session_ids


def parse_existing_journal(journal_content):
    """
    Parses a monolithic project journal into four components:
    1. Intro text (everything before the first entry).
    2. Entries: A list of dicts: {"date": date_obj, "raw_date": str, "title": str, "body": str}.
    3. Pending / Next Steps block.
    4. Appendix block.
    """
    lines = journal_content.splitlines()
    
    intro_lines = []
    entries = []
    pending_lines = []
    appendix_lines = []
    
    current_section = "intro"
    current_entry = None
    
    # Standard formats:
    # ## YYYY-MM-DD — Title
    # ## Pending / Next Steps
    # ## Appendix: ...
    entry_header_pattern = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s+[\u2014-]\s+(.*)$")
    
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        
        # Check headings
        if line.startswith("## "):
            entry_match = entry_header_pattern.match(line)
            if entry_match:
                # Save previous entry if exists
                if current_entry:
                    entries.append(current_entry)
                
                date_str, title = entry_match.groups()
                current_section = "entry"
                current_entry = {
                    "date": datetime.strptime(date_str, "%Y-%m-%d"),
                    "raw_date": date_str,
                    "title": title.strip(),
                    "body_lines": []
                }
            elif "Pending / Next Steps" in line:
                if current_entry:
                    entries.append(current_entry)
                    current_entry = None
                current_section = "pending"
                pending_lines.append(line)
            elif "Appendix:" in line:
                if current_entry:
                    entries.append(current_entry)
                    current_entry = None
                current_section = "appendix"
                appendix_lines.append(line)
            else:
                # Treat other headings under the active section
                if current_section == "entry" and current_entry:
                    current_entry["body_lines"].append(line)
                elif current_section == "intro":
                    intro_lines.append(line)
                elif current_section == "pending":
                    pending_lines.append(line)
                elif current_section == "appendix":
                    appendix_lines.append(line)
        else:
            # Append non-heading lines
            if current_section == "intro":
                intro_lines.append(line)
            elif current_section == "entry" and current_entry:
                current_entry["body_lines"].append(line)
            elif current_section == "pending":
                pending_lines.append(line)
            elif current_section == "appendix":
                appendix_lines.append(line)
        idx += 1
        
    if current_entry:
        entries.append(current_entry)
        
    return {
        "intro": "\n".join(intro_lines).strip() + "\n\n",
        "entries": entries,
        "pending": "\n".join(pending_lines).strip(),
        "appendix": "\n".join(appendix_lines).strip()
    }


def merge_and_sort_journal(journal_path, new_entries_text, verbose=False):
    """
    Parses the new LLM generated entries, merges them into the parsed existing
    journal sections, sorts everything chronologically, and writes it back,
    preserving intro, pending, and appendix segments.
    """
    log_verbose(f"Merging new entries into journal: {journal_path}", verbose)
    
    if not os.path.exists(journal_path):
        # Initialize a new journal if none exists
        Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
        Path(journal_path).write_text("# Project Journal\n\n" + new_entries_text, encoding="utf-8")
        return
        
    existing_content = Path(journal_path).read_text(encoding="utf-8")
    parsed = parse_existing_journal(existing_content)
    
    # Parse the new entries text
    new_parsed = parse_existing_journal(new_entries_text)
    
    # Combine entries list
    combined_entries = parsed["entries"] + new_parsed["entries"]
    
    # Deduplicate entries by date+title
    seen = set()
    deduped_entries = []
    for entry in combined_entries:
        key = (entry["raw_date"], entry["title"])
        if key not in seen:
            seen.add(key)
            deduped_entries.append(entry)
            
    # Sort chronologically by date
    deduped_entries.sort(key=lambda x: x["date"])
    
    # Build content
    output = []
    output.append(parsed["intro"])
    
    for entry in deduped_entries:
        body = "\n".join(entry["body_lines"]).strip()
        output.append(f"## {entry['raw_date']} \u2014 {entry['title']}\n\n{body}\n\n---\n")
    
    # Remove trailing divider before final sections if needed
    if output[-1].endswith("\n\n---\n"):
        output[-1] = output[-1][:-6] + "\n\n"
        
    if parsed["pending"]:
        output.append("---\n\n" + parsed["pending"] + "\n\n")
        
    if parsed["appendix"]:
        output.append("---\n\n" + parsed["appendix"] + "\n")
        
    # Write back
    Path(journal_path).write_text("".join(output), encoding="utf-8")
    log_verbose("Journal merged and updated successfully.", verbose)


# -----------------------------------------------------------------------------
# Main Execution Loop
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Gemini Journaling Agent - Summarize & Archive Session Logs"
    )
    parser.add_argument("--logs-dir", help="Directory containing .jsonl session logs.")
    parser.add_argument("--journal-path", help="Path to project_journal.md.")
    parser.add_argument("--memory-dir", help="Folder containing assistant memory .md files.")
    parser.add_argument("--model", default="gemini-1.5-flash", help="Gemini API Model Name.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without writing to journal.")
    parser.add_argument("--force", action="store_true", help="Reprocess all logs ignoring previous entries.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose status logging.")
    args = parser.parse_args()
    
    check_dependencies()
    
    cwd = Path.cwd()
    
    # 1. Resolve parameters or auto-discover
    logs_dir = args.logs_dir
    if not logs_dir:
        discovered = auto_discover_logs_dir(cwd.name)
        if discovered:
            logs_dir = str(discovered)
            log_verbose(f"Auto-discovered logs dir: {logs_dir}", args.verbose)
        else:
            print("[-] Error: Could not auto-discover logs directory. Provide --logs-dir.", file=sys.stderr)
            sys.exit(1)
            
    journal_path = args.journal_path
    if not journal_path:
        candidate = cwd / "docs" / "journal" / "project_journal.md"
        if candidate.parent.exists():
            journal_path = str(candidate)
        else:
            journal_path = str(cwd / "project_journal.md")
        log_verbose(f"Defaulting journal path: {journal_path}", args.verbose)
        
    memory_dir = args.memory_dir
    if not memory_dir:
        candidate = cwd / "memory"
        if candidate.exists() and candidate.is_dir():
            memory_dir = str(candidate)
        else:
            logs_dir_path = Path(logs_dir)
            if (logs_dir_path / "memory").exists():
                memory_dir = str(logs_dir_path / "memory")
                
    # 2. Get logs files
    logs_dir_path = Path(logs_dir)
    jsonl_files = sorted(list(logs_dir_path.glob("*.jsonl")))
    
    if not jsonl_files:
        print(f"[-] No .jsonl files found in log directory: {logs_dir}")
        sys.exit(0)
        
    recorded_sessions = set() if args.force else get_recorded_sessions(journal_path)
    log_verbose(f"Found {len(recorded_sessions)} already indexed session logs in journal.", args.verbose)
    
    sessions_to_process = []
    for f in jsonl_files:
        if args.force or f.stem not in recorded_sessions:
            sessions_to_process.append(f)
            
    if not sessions_to_process:
        print("[+] All available logs have already been integrated into the journal.")
        sys.exit(0)
        
    print(f"[+] Found {len(sessions_to_process)} new session logs to process.")
    
    # 3. Parse session logs and gather other inputs
    processed_sessions = []
    for f in sessions_to_process:
        # Skip active log if size > 1MB unless explicitly forcing
        if f.stat().st_size > 1_000_000 and not args.force:
            log_verbose(f"Skipping active/large session log to prevent self-referencing: {f.name}", args.verbose)
            continue
        processed_sessions.append(pre_process_jsonl(f, args.verbose))
        
    if not processed_sessions:
        print("[+] No inactive logs to process.")
        sys.exit(0)
        
    git_history = scan_git_history(cwd)
    memory_text = scan_memory_dir(memory_dir, args.verbose)
    
    # 4. Budget Estimation
    budget = estimate_cost(processed_sessions, memory_text, git_history)
    print("\n" + "="*50)
    print("PRE-FLIGHT BUDGET ESTIMATION")
    print(f"Total Input Characters: {budget['characters']}")
    print(f"Estimated Input Tokens:  {budget['input_tokens']}")
    print(f"Estimated Output Tokens: {budget['output_tokens']}")
    print(f"Predicted Cost (USD):   ${budget['estimated_cost_usd']:.5f}")
    print("="*50)
    
    # Retrieve API key: if dry-run and no key is set, we run without key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        if args.dry_run:
            print("\n[!] GEMINI_API_KEY environment variable not found.")
            print("    Exiting dry run early. (API call skipped, budget check passed.)")
            sys.exit(0)
        else:
            api_key = get_gemini_api_key(required=True)
            
    if not args.dry_run:
        confirm = input("Do you wish to proceed with the Gemini API calls? (y/n): ").strip().lower()
        if confirm != 'y':
            print("[-] Cancelled by user.")
            sys.exit(0)
            
    # 5. Connect and call Gemini API
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    
    system_prompt = """
You are a journaling and archiving agent. Your goal is to read the chronological history of a developer-assistant coding session, understand the main engineering actions, problems solved, and structural shifts, and write a concise, formal entry matching the target markdown journal format.

Target Format:
## YYYY-MM-DD — Concise Title Reflecting Session Accomplishments

**Session context:** `[session_id].jsonl`

[2-4 concise, professional sentences summarizing the session: what was built, what bug was fixed, what architectural decision was made, and why it matters.]

Rules:
1. Always output exactly one entry per session.
2. Group sessions on the same date together if applicable, or output separate headings.
3. Ensure no PII (addresses, IDs, credentials, or keys) are summarized.
4. Maintain a professional engineering tone.
"""

    llm_payload = []
    llm_payload.append(f"Memory Directories:\n{memory_text}\n")
    llm_payload.append(f"Git History:\n{git_history}\n")
    
    print("[+] Invoking Gemini API to synthesize summaries...")
    
    summaries_text = []
    model = genai.GenerativeModel(args.model, system_instruction=system_prompt)
    
    for session in processed_sessions:
        session_payload = f"Session ID: {session['session_id']}\n"
        session_payload += f"Time: {session['start_time']} to {session['end_time']}\n"
        session_payload += "Interactions:\n"
        for inter in session["interactions"]:
            session_payload += f"[{inter['role'].upper()}]: {inter['content']}\n"
            
        full_prompt = "\n".join(llm_payload) + f"\n\nActive Session to Summarize:\n{session_payload}"
        
        try:
            log_verbose(f"Sending prompt to Gemini for session {session['session_id']}...", args.verbose)
            res = model.generate_content(full_prompt)
            summaries_text.append(res.text.strip())
        except Exception as e:
            print(f"[-] Error calling Gemini API for session {session['session_id']}: {e}", file=sys.stderr)
            
    all_new_entries = "\n\n".join(summaries_text)
    
    if args.dry_run:
        print("\n=== DRY RUN: PROPOSED JOURNAL ENTRIES ===")
        print(all_new_entries)
        print("=========================================")
    else:
        merge_and_sort_journal(journal_path, all_new_entries, args.verbose)
        print(f"[+] Journal file '{journal_path}' updated successfully.")


if __name__ == "__main__":
    main()

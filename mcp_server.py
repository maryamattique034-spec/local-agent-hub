"""mcp_server.py - our own LOCAL MCP server (built with FastMCP).

How it works
------------
* Every function decorated with @mcp.tool() becomes a tool the model can call.
* The model NEVER sees your code. It only sees: the function name, the
  arguments (from the type hints) and the DOCSTRING. So the docstring is the
  instruction manual: say WHEN to use the tool and WHEN NOT to.
* agent.py starts this file as a subprocess and talks to it over stdio.
* IMPORTANT: never print() in this file. stdout is the MCP channel, and
  a stray print will break the protocol. Return strings instead.

Why these tools (picked for what a small 7B model is BAD at)
------------------------------------------------------------
  calculate          -> LLMs make arithmetic mistakes, so use real math
  get_current_time   -> the model cannot know the time, so ask the clock
  list/read/save_note-> memory that survives after the chat is closed
  add/list/complete_task -> a small to-do list saved in a JSON file
  fetch_webpage      -> the model has no internet, so give it a page as text
"""
import ast
import ipaddress
import json
import operator
import re
import socket
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

import certifi
import ssl
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("local-tools")

BASE = Path(__file__).parent.resolve() / "workspace"
NOTES_DIR = BASE / "notes"
TASKS_FILE = BASE / "tasks.json"
NOTES_DIR.mkdir(parents=True, exist_ok=True)

# for export_to_excel tool
EXPORTS_DIR = BASE / "exports"
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)



# =============================================================== 1. CALCULATOR
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node):
    """Evaluate a parsed expression. Only numbers and math operators are allowed."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large (max 100)")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("only numbers and + - * / // % ** ( ) are allowed")


@mcp.tool()
def calculate(expression: str) -> str:
    """Do exact arithmetic. ALWAYS use this for any calculation, even easy ones.

    Use when: the user asks for a sum, product, percentage, discount, average,
    unit conversion or any other number crunching.
    Do NOT do the math yourself, because you can make mistakes.

    Args:
        expression: A math expression using numbers and + - * / // % ** and
            brackets. Example: "(1200 * 0.15) + 50". For percentages, convert
            first: "15% of 200" becomes "0.15 * 200".

    Returns:
        "<expression> = <result>", or "Error: ..." if the expression is invalid.
    """
    try:
        result = _eval(ast.parse(expression.strip(), mode="eval").body)
    except ZeroDivisionError:
        return "Error: division by zero"
    except (ValueError, SyntaxError) as e:
        return f"Error: {e}"
    return f"{expression.strip()} = {result}"


# ==================================================================== 2. CLOCK
@mcp.tool()
def get_current_time(timezone: str = "local") -> str:
    """Get the real current date and time. You cannot know this yourself.

    Use when: the user asks what time or date it is, what day it is today,
    or the time in another city or country.

    Args:
        timezone: "local" for this computer's timezone (default), or an IANA
            timezone name such as "Asia/Tokyo", "Europe/London",
            "America/New_York" or "Asia/Karachi".

    Returns:
        Date, time, timezone and weekday, e.g. "2026-09-28 14:35:10 PKT (Monday)".
    """
    try:
        if timezone.strip().lower() == "local":
            now = datetime.now().astimezone()
        else:
            now = datetime.now(ZoneInfo(timezone.strip()))
    except ZoneInfoNotFoundError:
        return (f"Error: unknown timezone '{timezone}'. "
                "Use a name like 'Asia/Tokyo' or 'Europe/London'.")
    return now.strftime("%Y-%m-%d %H:%M:%S %Z (%A)")


# ==================================================================== 3. NOTES
def _note_path(name: str) -> Path:
    """Turn a note name into a safe file path (blocks '../' tricks)."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip().removesuffix(".txt"))
    if not clean:
        raise ValueError("note name is empty")
    return NOTES_DIR / f"{clean}.txt"


@mcp.tool()
def list_notes() -> str:
    """List the names of all saved notes.

    Use when: the user asks what notes exist, or BEFORE read_note when you
    are not sure of the exact note name.

    Returns:
        One note name per line, or "No notes yet."
    """
    names = sorted(p.stem for p in NOTES_DIR.glob("*.txt"))
    return "\n".join(names) if names else "No notes yet."


@mcp.tool()
def read_note(name: str) -> str:
    """Read the full text of one saved note.

    Use when: the user asks what is in a note, or refers to something they
    saved earlier. Never guess the content of a note. Read it.

    Args:
        name: The note name, like "shopping-list" (without .txt).
            If unsure, call list_notes first.

    Returns:
        The note text, or an error that lists the notes that do exist.
    """
    try:
        path = _note_path(name)
    except ValueError as e:
        return f"Error: {e}"
    if not path.exists():
        return f"Error: note '{name}' not found.\nExisting notes:\n{list_notes()}"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def save_note(name: str, content: str, append: bool = False) -> str:
    """Save text as a note so it is remembered after this chat ends.

    Use when: the user says "save this", "remember this", "write this down"
    or "add this to my notes".
    Warning: this OVERWRITES an existing note unless append is true.

    Args:
        name: Short note name, like "ideas" or "meeting-2026-09-28".
            Letters, numbers, "-" and "_" only (other characters become "_").
        content: The text to store.
        append: True = add to the end of the existing note.
            False (default) = replace the note.

    Returns:
        A confirmation message with the note name.
    """
    try:
        path = _note_path(name)
    except ValueError as e:
        return f"Error: {e}"
    if append and path.exists():
        content = path.read_text(encoding="utf-8").rstrip("\n") + "\n" + content
    path.write_text(content, encoding="utf-8")
    return f"Saved note '{path.stem}' ({len(content)} characters)."


# ==================================================================== 4. TASKS
def _load_tasks() -> list[dict]:
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_tasks(tasks: list[dict]) -> None:
    TASKS_FILE.write_text(json.dumps(tasks, indent=2), encoding="utf-8")


@mcp.tool()
def add_task(title: str, due: str = "") -> str:
    """Add a new item to the to-do list.

    Use when: the user says "add a task", "remind me to ..." or "I need to ...".

    Args:
        title: What needs to be done, e.g. "Submit assignment".
        due: Optional due date or time in plain text, e.g. "Friday" or "2026-10-01".

    Returns:
        A confirmation with the new task id.
    """
    tasks = _load_tasks()
    task_id = max((t["id"] for t in tasks), default=0) + 1
    tasks.append({"id": task_id, "title": title.strip(), "due": due.strip(), "done": False})
    _save_tasks(tasks)
    return f"Added task #{task_id}: {title.strip()}" + (f" (due {due.strip()})" if due.strip() else "")


@mcp.tool()
def list_tasks(status: str = "pending") -> str:
    """Show tasks from the to-do list.

    Use when: the user asks what they have to do, what is pending, or what
    they finished. Also call this BEFORE complete_task to find the task id.

    Args:
        status: "pending" (default), "done" or "all".

    Returns:
        One task per line as "#id [ ] title (due ...)", or a message if empty.
    """
    tasks = _load_tasks()
    if status == "pending":
        tasks = [t for t in tasks if not t["done"]]
    elif status == "done":
        tasks = [t for t in tasks if t["done"]]
    if not tasks:
        return f"No {status} tasks."
    return "\n".join(
        f"#{t['id']} [{'x' if t['done'] else ' '}] {t['title']}"
        + (f" (due {t['due']})" if t["due"] else "")
        for t in tasks
    )


@mcp.tool()
def complete_task(task_id: int) -> str:
    """Mark one task as done.

    Use when: the user says they finished a task or asks to tick it off.
    If you do not know the id, call list_tasks first. Do not guess it.

    Args:
        task_id: The number of the task, e.g. 3 for "#3".

    Returns:
        A confirmation, or an error if the id does not exist.
    """
    tasks = _load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["done"] = True
            _save_tasks(tasks)
            return f"Marked task #{task_id} as done: {t['title']}"
    return f"Error: no task with id {task_id}."


# ================================================================ 5. WEB PAGE
class _TextExtractor(HTMLParser):
    """Collect visible text from HTML (skips scripts and styles)."""
    _SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def _check_url(url: str) -> None:
    """Basic safety: only http/https, and never local or private addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http:// and https:// URLs are allowed")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ValueError(f"cannot resolve host '{parsed.hostname}'")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("local and private network addresses are blocked")


class _SafeRedirect(HTTPRedirectHandler):
    """Re-check every redirect target so a page cannot bounce us to localhost."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _opener():
    return build_opener(_SafeRedirect, HTTPSHandler(context=_SSL_CONTEXT))


@mcp.tool()
def fetch_webpage(url: str) -> str:
    """Download a web page and return its readable text.

    Use when: the user gives a URL and wants it read, summarized or checked.
    Do NOT use for general questions. You cannot search the web with this,
    you can only open one specific address.

    Args:
        url: Full address starting with http:// or https://.

    Returns:
        The page text (first ~5000 characters), or "Error: ..." on failure.
    """
    try:
        _check_url(url)
        req = Request(url, headers={"User-Agent": "local-agent-hub/1.0"})
        with _opener().open(req, timeout=10) as resp:
            ctype = resp.headers.get_content_type()
            if ctype not in ("text/html", "text/plain"):
                return f"Error: unsupported content type '{ctype}' (only text pages)."
            raw = resp.read(500_000)
            charset = resp.headers.get_content_charset() or "utf-8"
        html = raw.decode(charset, errors="replace")
    except Exception as e:  # return the error so the model can explain it
        return f"Error: could not fetch page ({e})"

    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return f"URL: {url}\n\n{text[:5000]}" if text else "Error: page has no readable text."


@mcp.tool()
def export_to_excel(filename: str, rows_json: str) -> str:
    """Export tabular data as a downloadable Excel (.xlsx) file.

    Use when: the user asks to "export this as Excel", "make a spreadsheet",
    "save this as a file", or similar — AFTER you already have the data
    (e.g. from another tool's output). Never invent data; only export data
    you actually have from a previous tool result or the conversation.

    Args:
        filename: Desired file name without extension, e.g. "biblio_mismatch_report".
        rows_json: A JSON string representing a list of objects (rows), all
            sharing the same keys, e.g.
            '[{"title": "...", "expected": "...", "found": "..."}, ...]'

    Returns:
        "EXPORTED:<absolute file path>" on success, or "Error: ..." on failure.
    """
    try:
        rows = json.loads(rows_json)
        if not isinstance(rows, list) or not rows:
            return "Error: rows_json must be a non-empty JSON list of objects."
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON ({e})"

    clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", filename.strip()) or "export"
    path = EXPORTS_DIR / f"{clean_name}.xlsx"

    wb = Workbook()
    ws = wb.active
    headers = list(rows[0].keys())
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h, "") for h in headers])

    for i, h in enumerate(headers, start=1):
        width = max(len(h), *(len(str(r.get(h, ""))) for r in rows)) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 50)

    wb.save(path)
    return f"EXPORTED:{path}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
# Local Agent Hub: Qwen + LangGraph + local MCP tools + Chainlit

Only 2 python files:

```
mcp_server.py   # the tools ONLY (@mcp.tool() functions + docstrings)
app.py          # everything else: Qwen client, MCP client, LangGraph, Chainlit UI
```

## Setup (Mac)

```bash
ollama serve                      # make sure Ollama is running
ollama pull qwen2.5:7b
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
chainlit run app.py -w            # opens http://localhost:8000
```

**Do NOT run `python mcp_server.py` yourself.** It's a background helper with
no output of its own — `app.py` starts it automatically as a subprocess each
time the chat launches. If you ran it manually and it looks "frozen", that's
normal (it's just waiting); press Ctrl+C to stop it.

## The 9 tools (all in mcp_server.py)

| Use case | Tool(s) |
|---|---|
| Math | `calculate` |
| Date / time | `get_current_time` |
| Long-term memory | `save_note`, `list_notes`, `read_note` |
| To-do list | `add_task`, `list_tasks`, `complete_task` |
| Read a web page | `fetch_webpage` |

Add your own tool: write a function in `mcp_server.py`, put `@mcp.tool()`
above it, write a docstring saying WHEN to use it, restart the app. Nothing
in `app.py` needs to change.

## The graph (inside app.py)

```
START -> plan -> agent --(wants a tool)--> tools --(fetch_webpage used)--> summarize_page --> agent
                   |                          \--(other tool)--------------------------------> agent
                   |--(plain answer)--> END
                   \--(6 model calls, no answer)--> give_up --> END
```

## Test prompts

1. `What is 23 * 47 + 15% of 800?`
2. `What time is it in Tokyo?`
3. `Save a note called ideas: build a portfolio project` then `What is in my ideas note?`
4. `Add a task: submit assignment, due Friday` then `What is pending?` then `Mark task 1 as done`
5. `Read https://example.com and summarize it`
6. `Read my project note and tell me the secret word` -> PINEAPPLE-42
7. `Say hi` -> plan says "No tools needed"

Watch the node steps in the UI and the `[graph] node ran:` lines in the terminal.

## Notes
- `qwen2.5:7b` is the weak link for reasoning. Compare: `QWEN_MODEL=qwen3:8b chainlit run app.py -w`
- Notes and tasks are plain files in `workspace/`. Delete them to reset.
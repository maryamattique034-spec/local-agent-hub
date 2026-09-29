"""app.py - EVERYTHING except the tools themselves, in one file.

Run:   chainlit run app.py -w
Needs: mcp_server.py in the same folder (that file is started automatically
       as a subprocess below - you never run it yourself).

Sections in this file, top to bottom:
  1. Qwen client        - talks to Ollama
  2. MCP client          - starts mcp_server.py and loads its tools
  3. LangGraph           - state, nodes, edges, routers (the "wiring")
  4. Chainlit UI          - the chat window (the "face")
"""
import os
import sys
from pathlib import Path
from typing import Annotated, TypedDict

import chainlit as cl
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

MODEL = os.getenv("QWEN_MODEL", "qwen2.5:7b")  # try QWEN_MODEL=qwen3:8b to compare
MAX_STEPS = 6  # safety: max model calls per user message


# ======================================================== 1. QWEN CLIENT
def make_llm():
    """The brain. Talks to your local Ollama server."""
    return ChatOllama(model=MODEL, temperature=0, num_ctx=8192)


# ======================================================== 2. MCP CLIENT
def mcp_servers_config():
    """Where the tools live: our own mcp_server.py, started as a subprocess.

    'command': sys.executable  ->  run it with the SAME python as this app
                                    (so it uses the same virtualenv/packages)
    'args': [path to mcp_server.py]
    'transport': 'stdio'       ->  talk to it over stdin/stdout, not network
    """
    server_path = Path(__file__).parent.resolve() / "mcp_server.py"
    return {
        "local": {
            "command": sys.executable,
            "args": [str(server_path)],
            "transport": "stdio",
        },
    }


async def load_mcp_tools():
    client = MultiServerMCPClient(mcp_servers_config())
    tools = await client.get_tools()
    print("[mcp] tools loaded:", [t.name for t in tools])
    return tools


# ======================================================== HELPERS
def as_text(content) -> str:
    """MCP tools may return a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "\n".join(parts)
    return str(content)


def latest_tool_messages(messages) -> list[ToolMessage]:
    """The ToolMessages that came right after the last model answer."""
    out = []
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            out.append(m)
        else:
            break
    return list(reversed(out))


# ======================================================== 3. LANGGRAPH
class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]  # chat + tool results
    plan: str   # short plan written by the "plan" node
    steps: int  # how many times the model was called (loop protection)


def build_graph(tools):
    llm = make_llm()
    llm_with_tools = llm.bind_tools(tools)
    tool_names = ", ".join(t.name for t in tools)

    # ---------- NODES: each one is just a function (state in -> updates out)
    async def plan_node(state: AgentState):
        """Step 1: Qwen writes a tiny plan (no tools yet)."""
        question = as_text(state["messages"][-1].content)
        resp = await llm.ainvoke([
            SystemMessage(
                "Write a plan in at most 3 short lines for answering the user. "
                f"Available tools: {tool_names}. "
                "If no tool is needed, write exactly: No tools needed."
            ),
            HumanMessage(question),
        ])
        return {"plan": as_text(resp.content)}

    async def agent_node(state: AgentState):
        """Step 2: Qwen decides -> answer directly OR ask for a tool."""
        system = SystemMessage(
            "You are a helpful assistant with tools. Pick the tool by the job:\n"
            "- any math -> calculate (never do arithmetic yourself)\n"
            "- date or time -> get_current_time (you cannot know it)\n"
            "- remember / save something -> save_note; recall it -> list_notes then read_note\n"
            "- to-do items -> add_task, list_tasks, complete_task\n"
            "- a URL the user gives -> fetch_webpage\n"
            "If no tool fits, just answer. Never invent tool results. "
            "After you get tool results, answer clearly and briefly.\n\n"
            f"Your plan for this question:\n{state['plan']}"
        )
        resp = await llm_with_tools.ainvoke([system] + state["messages"])
        return {"messages": [resp], "steps": state["steps"] + 1}

    tool_node = ToolNode(tools)  # Step 3: actually runs the MCP tool Qwen asked for

    async def summarize_page_node(state: AgentState):
        """Custom step: only runs after `fetch_webpage`. Shrinks long web pages."""
        fixes = []
        for m in latest_tool_messages(state["messages"]):
            if m.name != "fetch_webpage":
                continue
            text = as_text(m.content)
            if len(text) <= 2000:
                continue
            short = await llm.ainvoke([
                SystemMessage("Condense this web page into its key facts, under 150 words."),
                HumanMessage(text[:6000]),
            ])
            # same id => this REPLACES the original tool message in state
            fixes.append(ToolMessage(
                content=as_text(short.content),
                tool_call_id=m.tool_call_id,
                name=m.name,
                id=m.id,
            ))
        return {"messages": fixes}

    async def give_up_node(state: AgentState):
        return {"messages": [AIMessage(
            f"I stopped after {MAX_STEPS} steps without a final answer. "
            "Try asking in a simpler or more specific way."
        )]}

    # ---------- ROUTERS: these decide which EDGE to follow next
    def route_after_agent(state: AgentState):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):        # Qwen asked for a tool
            return "give_up" if state["steps"] >= MAX_STEPS else "tools"
        return "done"                                  # plain answer -> finish

    def route_after_tools(state: AgentState):
        used = {m.name for m in latest_tool_messages(state["messages"])}
        return "summarize_page" if "fetch_webpage" in used else "agent"

    # ---------- WIRING: nodes + edges = the graph
    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("agent", agent_node)
    g.add_node("tools", tool_node)
    g.add_node("summarize_page", summarize_page_node)
    g.add_node("give_up", give_up_node)

    g.add_edge(START, "plan")            # normal edge: always
    g.add_edge("plan", "agent")          # normal edge: always
    g.add_conditional_edges(             # conditional edge: router decides
        "agent", route_after_agent,
        {"tools": "tools", "give_up": "give_up", "done": END},
    )
    g.add_conditional_edges(             # conditional edge: depends on WHICH tool ran
        "tools", route_after_tools,
        {"summarize_page": "summarize_page", "agent": "agent"},
    )
    g.add_edge("summarize_page", "agent")  # the loop back
    g.add_edge("give_up", END)

    return g.compile(checkpointer=MemorySaver())  # MemorySaver = per-chat memory


# Build once, reuse for every chat session in this process.
_graph = None


async def get_graph():
    global _graph
    if _graph is None:
        tools = await load_mcp_tools()
        _graph = build_graph(tools)
    return _graph


# ======================================================== 4. CHAINLIT UI
def describe(node: str, data: dict) -> str:
    """Turn a node's output into short readable text for the UI step."""
    if node == "plan":
        return data.get("plan", "")
    msgs = data.get("messages", [])
    if not msgs:
        return "(no changes)"
    lines = []
    for m in msgs:
        calls = getattr(m, "tool_calls", None)
        if calls:
            for c in calls:
                lines.append(f"Qwen wants tool: {c['name']}({c['args']})")
        elif m.type == "tool":
            lines.append(f"[{m.name}] -> {as_text(m.content)[:500]}")
        else:
            lines.append(as_text(m.content)[:500])
    return "\n".join(lines)


@cl.on_chat_start
async def start():
    await get_graph()  # starts mcp_server.py + loads its tools (a few seconds)
    await cl.Message(
        "Ready. I can do math, tell the time, keep notes and tasks, and read web pages."
    ).send()


@cl.on_message
async def on_message(msg: cl.Message):
    graph = await get_graph()
    config = {"configurable": {"thread_id": cl.context.session.id}}  # per-chat memory slot
    inputs = {"messages": [HumanMessage(msg.content)], "steps": 0}

    final = None
    async for update in graph.astream(inputs, config, stream_mode="updates"):
        for node, data in update.items():
            if not data:
                continue
            print(f"[graph] node ran: {node}")
            async with cl.Step(name=f"node: {node}", type="tool") as step:
                step.output = describe(node, data)

            if node in ("agent", "give_up"):
                last = data["messages"][-1]
                if not getattr(last, "tool_calls", None):
                    final = as_text(last.content)

    await cl.Message(content=final or "(no answer)").send()
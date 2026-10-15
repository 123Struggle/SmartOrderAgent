"""Agent 编排层：模型、工具、会话持久化与流式输出。

注意：langchain 1.x 的 create_agent 把模型节点命名为 "model"，
旧的 create_react_agent 命名为 "agent"。流式过滤依赖节点名，这里自动探测，
避免升级后流式接口静默不输出。
"""

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_deepseek import ChatDeepSeek
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.prompt.system_prompt import SYSTEM_PROMPT
from agent.tools import (
    faq_tool,
    get_featured_dishes,
    make_reservation,
    recommend_dishes_by_preference,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from functools import lru_cache
import atexit
from agent.reservations import current_session


@lru_cache(maxsize=1)
def get_agent():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    llm = ChatDeepSeek(model="deepseek-chat", api_key=api_key, temperature=0.3)
    db_path = os.getenv("APP_CHECKPOINT_DB", str(Path(__file__).resolve().parents[1] / "checkpoints.db"))
    conn = sqlite3.connect(db_path, check_same_thread=False)
    atexit.register(conn.close)
    return create_agent(model=llm, tools=[get_featured_dishes, faq_tool, recommend_dishes_by_preference, make_reservation],
                        system_prompt=SYSTEM_PROMPT, checkpointer=SqliteSaver(conn))


def _text_of(chunk) -> str:
    """兼容 message chunk 的 content 为字符串或内容块列表两种形态。"""
    content = getattr(chunk, "content", chunk)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def chat_with_agent(user_input: str, thread_id: str) -> str:
    token = current_session.set(thread_id)
    try:
        response = get_agent().invoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config={"configurable": {"thread_id": thread_id}},
        )
        return _text_of(response["messages"][-1])
    finally:
        current_session.reset(token)


def stream_chat_with_agent(user_input: str, thread_id: str):
    executor = get_agent()
    nodes = set(executor.get_graph().nodes)
    model_node = "model" if "model" in nodes else "agent"
    iterator = iter(executor.stream(
        {"messages": [{"role": "user", "content": user_input}]},
        config={"configurable": {"thread_id": thread_id}}, stream_mode="messages",
    ))
    try:
        while True:
            # Starlette 可能在不同 worker 线程中迭代；每一步单独绑定/恢复上下文。
            token = current_session.set(thread_id)
            try:
                chunk, metadata = next(iterator)
            except StopIteration:
                break
            finally:
                current_session.reset(token)
            if metadata.get("langgraph_node") == model_node:
                text = _text_of(chunk)
                if text:
                    yield text
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()

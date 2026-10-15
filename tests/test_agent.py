"""不写业务库；外部服务测试使用随机隔离数据库。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4
import json
import os

import pytest
from fastapi.testclient import TestClient

from api import main as api
from agent import session_store, reservations, tools, milvus_data_sync as milvus
from agent.locking import exclusive_lock, OperationBusyError


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("APP_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture
def client():
    return TestClient(api.app)


def session(client):
    result = client.post("/sessions").json()
    return result["session_id"], {"Authorization": "Bearer " + result["session_token"]}


def details():
    return dict(num_people=3, num_children=0,
                arrival_time=(datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d 19:00:00"),
                seat_preference="靠窗", main_dish_preference=None, other_comments="隔离测试")


def test_health_check(client):
    assert client.get("/health").json() == {"status": "ok", "service": "SmartOrderAgent"}


def test_sessions_are_authenticated_and_isolated(client, monkeypatch):
    first, headers = session(client)
    second, _ = session(client)
    monkeypatch.setattr(api, "chat_with_agent", lambda **kw: kw["thread_id"])
    assert client.post("/chat", json={"message":"你好"}).status_code == 401
    assert client.post("/chat", headers=headers, json={"message":"你好", "session_id":second}).status_code == 403
    result = client.post("/chat", headers=headers, json={"message":"你好"})
    assert result.status_code == 200
    assert result.json()["reply"] == first


def test_sse_round_trip_preserves_newlines_and_unicode(client, monkeypatch):
    _, headers = session(client)
    chunks = ["第一行\n\n第二行", "\r\n引号：\" 中文😀", "data: 这也是正文"]
    monkeypatch.setattr(api, "stream_chat_with_agent", lambda **kw: iter(chunks))
    response = client.post("/chat/stream", headers=headers, json={"message":"你好"})
    events = [json.loads(block[6:]) for block in response.text.split("\n\n") if block.startswith("data: ")]
    assert "".join(e["text"] for e in events if e["type"] == "token") == "".join(chunks)
    assert events[-1]["type"] == "done"


def test_stream_failure_is_explicit(client, monkeypatch):
    _, headers = session(client)
    def fail(**kw):
        yield "部分回复"
        raise RuntimeError("internal details")
    monkeypatch.setattr(api, "stream_chat_with_agent", fail)
    response = client.post("/chat/stream", headers=headers, json={"message":"你好"})
    assert '"type": "error"' in response.text
    assert "internal details" not in response.text


def test_admin_requires_config_and_token(client, monkeypatch):
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    assert client.post("/admin/sync-menu").status_code == 503
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-only")
    called = MagicMock(return_value={"status":"success"})
    monkeypatch.setattr(api, "run_menu_sync", called)
    assert client.post("/admin/sync-menu").status_code == 403
    called.assert_not_called()
    assert client.post("/admin/sync-menu", headers={"X-Admin-Token":"test-only"}).status_code == 200


def test_file_lock_blocks_second_process(tmp_path):
    import subprocess, sys
    with exclusive_lock("same"):
        script = "from agent.locking import exclusive_lock; with_block = exclusive_lock('same'); with_block.__enter__()"
        child = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=30)
        assert child.returncode != 0
        assert b"OperationBusyError" in child.stderr
    with exclusive_lock("same"):
        pass


def test_api_reports_sync_busy(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-only")
    def busy(): raise OperationBusyError("busy")
    monkeypatch.setattr(api, "run_menu_sync", busy)
    assert client.post("/admin/sync-menu", headers={"X-Admin-Token":"test-only"}).status_code == 409


def test_sync_lock_covers_cli_and_api(monkeypatch):
    collection = "lock_test"
    key = "menu:" + os.getenv("MILVUS_URI", "http://localhost:19530") + ":" + os.getenv("MILVUS_DB_NAME", "smartorder") + ":" + collection
    fetch = MagicMock()
    monkeypatch.setattr(milvus, "fetch_dishes", fetch)
    with exclusive_lock(key):
        with pytest.raises(OperationBusyError):
            milvus.sync_dishes(MagicMock(), collection)
    fetch.assert_not_called()


def test_sync_config_and_clients_close(monkeypatch):
    admin, business = MagicMock(), MagicMock()
    admin.list_databases.return_value = ["smartorder"]
    monkeypatch.setenv("MILVUS_DB_NAME", "smartorder")
    monkeypatch.setenv("MILVUS_COLLECTION_NAME", "custom_menu")
    monkeypatch.setattr(milvus, "MilvusClient", MagicMock(side_effect=[admin, business]))
    sync = MagicMock(return_value="custom_menu_live")
    monkeypatch.setattr(milvus, "sync_dishes", sync)
    assert milvus.run_menu_sync()["alias"] == "custom_menu_live"
    sync.assert_called_once_with(business, "custom_menu")
    admin.close.assert_called_once()
    business.close.assert_called_once()


def test_sync_clients_close_on_failure(monkeypatch):
    admin, business = MagicMock(), MagicMock()
    admin.list_databases.return_value = [os.getenv("MILVUS_DB_NAME", "smartorder")]
    monkeypatch.setattr(milvus, "MilvusClient", MagicMock(side_effect=[admin, business]))
    monkeypatch.setattr(milvus, "sync_dishes", MagicMock(side_effect=RuntimeError("failed")))
    with pytest.raises(RuntimeError): milvus.run_menu_sync()
    admin.close.assert_called_once()
    business.close.assert_called_once()


def test_model_can_only_create_draft(client, monkeypatch):
    sid, headers = session(client)
    connect = MagicMock(side_effect=AssertionError("草稿不应访问 MySQL"))
    monkeypatch.setattr(tools, "get_mysql_connection", connect)
    token = reservations.current_session.set(sid)
    try:
        result = tools.make_reservation.invoke(details())
        assert "尚未提交" in result
    finally:
        reservations.current_session.reset(token)
    draft = session_store.pending_draft(sid)
    assert draft and draft["order_id"] is None
    connect.assert_not_called()
    other, other_headers = session(client)
    assert client.post(f"/reservations/{draft['draft_id']}/confirm", headers=other_headers, json={"confirm":True}).status_code == 404
    assert client.post(f"/reservations/{draft['draft_id']}/confirm", headers=headers, json={"confirm":False}).status_code == 400
    connect.assert_not_called()


def test_repeated_draft_is_stable():
    sid = session_store.create_session()["session_id"]
    first = session_store.create_draft(sid, reservations.normalize_details(details()))
    second = session_store.create_draft(sid, reservations.normalize_details(details()))
    assert first["draft_id"] == second["draft_id"]


def test_real_agent_tool_execution_binds_session(client, monkeypatch):
    import sqlite3
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.sqlite import SqliteSaver
    from agent import order_agent

    class Model(BaseChatModel):
        @property
        def _llm_type(self): return "deterministic-test"
        def bind_tools(self, tools, **kwargs): return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if any(isinstance(m, ToolMessage) for m in messages):
                message = AIMessage(content="请点击确认按钮")
            else:
                message = AIMessage(content="", tool_calls=[{"name":"make_reservation", "args":details(), "id":"draft_call", "type":"tool_call"}])
            return ChatResult(generations=[ChatGeneration(message=message)])

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        executor = create_agent(model=Model(), tools=[tools.make_reservation], checkpointer=SqliteSaver(conn))
        monkeypatch.setattr(order_agent, "get_agent", lambda: executor)
        first, headers = session(client)
        result = client.post("/chat", headers=headers, json={"message":"预订"})
        assert result.status_code == 200
        assert result.json()["reservation_draft"]
        second, headers2 = session(client)
        result = client.post("/chat/stream", headers=headers2, json={"message":"预订"})
        assert '"type": "error"' not in result.text
        assert session_store.pending_draft(second)
        assert session_store.pending_draft(first)["draft_id"] != session_store.pending_draft(second)["draft_id"]
        assert reservations.current_session.get() is None
    finally:
        conn.close()


def test_committed_submission_retries_without_reinsert(monkeypatch):
    sid = session_store.create_session()["session_id"]
    draft = session_store.create_draft(sid, reservations.normalize_details(details()))
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {"order_id": 42}
    monkeypatch.setattr(tools, "get_mysql_connection", lambda: conn)
    result = reservations.submit_reservation(sid, draft["draft_id"])
    assert result["order_id"] == 42
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert not any("INSERT INTO reservation_order" in sql for sql in statements)
    assert reservations.submit_reservation(sid, draft["draft_id"])["order_id"] == 42
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def test_submission_failure_rolls_back(monkeypatch):
    sid = session_store.create_session()["session_id"]
    draft = session_store.create_draft(sid, reservations.normalize_details(details()))
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("write failed")
    monkeypatch.setattr(tools, "get_mysql_connection", lambda: conn)
    with pytest.raises(RuntimeError): reservations.submit_reservation(sid, draft["draft_id"])
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    conn.close.assert_called_once()
    assert session_store.get_draft(sid, draft["draft_id"])["order_id"] is None


def test_expired_session_and_draft_are_rejected(client):
    sid, headers = session(client)
    draft = session_store.create_draft(sid, reservations.normalize_details(details()))
    with session_store.connection() as conn:
        conn.execute("UPDATE reservation_drafts SET expires=0")
    assert client.post(f"/reservations/{draft['draft_id']}/confirm", headers=headers, json={"confirm":True}).status_code == 400
    with session_store.connection() as conn:
        conn.execute("UPDATE sessions SET expires=0")
    assert client.get("/reservations/pending", headers=headers).status_code == 401


@pytest.fixture
def reservation_database(monkeypatch):
    """只创建、删除本次随机测试数据库，绝不按业务订单 id 范围清理。"""
    import pymysql
    original = tools.get_mysql_connection
    name = "smartorder_test_" + uuid4().hex
    conn = original()
    try:
        with conn.cursor() as cursor:
            sql = (Path(__file__).resolve().parents[1] / "menu.sql").read_text(encoding="utf-8").replace("`menu`", f"`{name}`")
            for statement in sql.split(";"):
                if statement.strip(): cursor.execute(statement)
        conn.commit()
        monkeypatch.setenv("MYSQL_DB_NAME", name)
        yield name
    finally:
        assert name.startswith("smartorder_test_") and len(name) == 48
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
        conn.close()


@pytest.mark.integration
def test_confirm_concurrent_and_response_retry(client, reservation_database, monkeypatch):
    sid, headers = session(client)
    draft = session_store.create_draft(sid, reservations.normalize_details(details()))
    draft_id = draft["draft_id"]
    # 模拟 MySQL 已提交，但写本地提交状态/返回响应失败。
    original = session_store.mark_submitted
    monkeypatch.setattr(session_store, "mark_submitted", MagicMock(side_effect=RuntimeError("response lost")))
    assert client.post(f"/reservations/{draft_id}/confirm", headers=headers, json={"confirm":True}).status_code == 500
    monkeypatch.setattr(session_store, "mark_submitted", original)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: reservations.submit_reservation(sid, draft_id)["order_id"], range(4)))
    assert len(set(ids)) == 1
    again = client.post(f"/reservations/{draft_id}/confirm", headers=headers, json={"confirm":True})
    assert again.json()["order_id"] == ids[0]
    conn = tools.get_mysql_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM reservation_order")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT COUNT(*) AS n FROM reservation_submission")
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()


@pytest.mark.integration
def test_fresh_concurrent_confirmation_creates_one_order(reservation_database):
    sid = session_store.create_session()["session_id"]
    draft = session_store.create_draft(sid, reservations.normalize_details(details()))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reservations.submit_reservation(sid, draft["draft_id"]), range(4)))
    assert len({result["order_id"] for result in results}) == 1
    conn = tools.get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM reservation_order")
            assert cursor.fetchone()["n"] == 1
            cursor.execute("SELECT COUNT(*) AS n FROM reservation_submission")
            assert cursor.fetchone()["n"] == 1
    finally:
        conn.close()

"""手动运行：验证实际服务数据及隔离的 Redis/SQL 场景。"""
import asyncio
import os
from pathlib import Path
import sys
from uuid import uuid4
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymysql
from pymilvus import MilvusClient
from agent import milvus_data_sync
from agent.milvus_data_sync import fetch_dishes, test_query_data
from agent.redis_data_sync import _get_redis_client, sync_faq_to_redis


async def check_redis():
    r = await _get_redis_client()
    namespace = "smartorder_test_" + uuid4().hex
    try:
        item = dict(id="a", question="问题", answer="回答")
        await sync_faq_to_redis(r, namespace, [item, dict(id="b", question="问题2", answer="回答2")])
        item["answer"] = "更新回答"
        count, _ = await sync_faq_to_redis(r, namespace, [item])
        assert count == 1
        assert not await r.exists(f"{namespace}:item:b")
        assert await r.hget(f"{namespace}:item:a", "answer") == "更新回答".encode()
        await sync_faq_to_redis(r, namespace, [])
        assert not await r.exists(f"{namespace}:ids", f"{namespace}:item:a")
        assert await r.hget(f"{namespace}:meta", "count") == b"0"
        actual = os.getenv("FAQ_REDIS_NAMESPACE", "faq")
        ids = await r.smembers(f"{actual}:ids")
        assert await r.hget(f"{actual}:meta", "count") == str(len(ids)).encode()
        print(f"Redis 实际 FAQ: {len(ids)} 条；隔离的更新、删除、清空验证通过")
    finally:
        await r.delete(f"{namespace}:ids", f"{namespace}:meta", f"{namespace}:item:a", f"{namespace}:item:b")
        await r.aclose()


def check_sql():
    # 仅在随机隔离库内执行初始化脚本；不触碰业务数据库。
    database = "smartorder_test_" + uuid4().hex
    conn = pymysql.connect(host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")), user=os.getenv("MYSQL_USER_NAME", "root"),
        password=os.environ["MYSQL_USER_PASSWORD"], charset="utf8mb4", connect_timeout=10)
    try:
        sql = (Path(__file__).resolve().parents[1] / "menu.sql").read_text(encoding="utf-8")
        sql = sql.replace("`menu`", f"`{database}`")
        with conn.cursor() as cursor:
            for run in range(2):
                for statement in sql.split(";"):
                    if statement.strip(): cursor.execute(statement)
                if run == 0:
                    cursor.execute("INSERT INTO reservation_order (num_people,num_children,arrival_time) VALUES (2,0,'2026-10-14 12:00:00')")
                    conn.commit()
            cursor.execute("SELECT COUNT(*) FROM menu_items")
            assert cursor.fetchone()[0] == 5
            cursor.execute("SELECT COUNT(*) FROM reservation_order")
            assert cursor.fetchone()[0] == 1
        print("SQL 隔离库验证通过：重复初始化后仍为 5 道菜，已有预订保留")
    finally:
        assert database.startswith("smartorder_test_") and len(database) == 48
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        conn.close()


def check_milvus():
    client = MilvusClient(uri=os.getenv("MILVUS_URI", "http://localhost:19530"),
        token=os.getenv("MILVUS_TOKEN", ""), db_name=os.getenv("MILVUS_DB_NAME", "smartorder"), timeout=30)
    try:
        rows = []
        iterator = client.query_iterator(collection_name="menu_items_live", output_fields=["dish_id"], consistency_level="Strong")
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                rows.extend(batch)
        finally:
            iterator.close()
        assert {r["dish_id"] for r in rows} == {r["id"] for r in fetch_dishes()}
        print(f"Milvus 菜品 ID 与 MySQL 一致：{len(rows)} 条")
        name = "smartorder_test_" + uuid4().hex
        alias = name + "_live"
        try:
            with patch.object(milvus_data_sync, "fetch_dishes", return_value=[]):
                milvus_data_sync.sync_dishes(client, name)
                first = client.describe_alias(alias)["collection_name"]
                milvus_data_sync.sync_dishes(client, name)
                second = client.describe_alias(alias)["collection_name"]
                assert first != second
                milvus_data_sync.sync_dishes(client, name)
                third = client.describe_alias(alias)["collection_name"]
                assert third != second
                assert not client.has_collection(first)
                remaining = [c for c in client.list_collections() if c.startswith(name + "_v_")]
                assert len(remaining) == 2
                assert client.get_load_state(collection_name=second)["state"].name == "NotLoad"
            count = client.query(collection_name=alias, filter="", output_fields=["count(*)"], consistency_level="Strong")
            assert count[0]["count(*)"] == 0
            print("Milvus 隔离验证通过：连续发布三次、旧版本回收、备用版本释放内存、空菜单")
        finally:
            if alias in milvus_data_sync.list_alias_names(client):
                client.drop_alias(alias)
            for collection in client.list_collections():
                if collection.startswith(name + "_v_"):
                    client.release_collection(collection_name=collection)
                    client.drop_collection(collection)
            assert alias not in milvus_data_sync.list_alias_names(client)
            assert not any(c.startswith(name + "_v_") for c in client.list_collections())
        test_query_data(client, "menu_items_live", "我想吃微辣且酸甜的肉")
    finally:
        client.close()


if __name__ == "__main__":
    check_sql()
    asyncio.run(check_redis())
    check_milvus()

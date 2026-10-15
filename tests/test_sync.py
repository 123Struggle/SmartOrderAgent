import unittest
from unittest.mock import MagicMock, patch

from agent import milvus_data_sync as milvus
from agent import redis_data_sync as faq


def dish(description="正常描述"):
    return dict(id=1, dish_name="宫保鸡丁", price="28.00", description=description,
                category="川菜", spice_level=1, flavor="酸甜微辣", main_ingredients="鸡肉,花生",
                cooking_method="爆炒", is_vegetarian=0, allergens="花生")


class MilvusSyncTests(unittest.TestCase):
    def model(self):
        model = MagicMock()
        model.embed_documents.return_value = [[0.0] * 1024]
        return model

    def client(self, count=1):
        client = MagicMock()
        client.insert.return_value = {"insert_count": count}
        client.query.return_value = [{"count(*)": count}]
        client.list_aliases.side_effect = lambda collection_name="": {"aliases": [] if collection_name else ["menu_items_live"], "db_name": "smartorder"}
        client.list_collections.return_value = []
        client.has_collection.return_value = True
        return client

    def test_success_switches_only_after_validation(self):
        client = self.client()
        with patch.object(milvus, "fetch_dishes", return_value=[dish()]):
            alias = milvus.sync_dishes(client, model=self.model())
        self.assertEqual(alias, "menu_items_live")
        names = [c[0] for c in client.method_calls]
        self.assertLess(names.index("query"), names.index("alter_alias"))
        client.drop_collection.assert_not_called()
        client.create_alias.assert_not_called()
        payload = client.insert.call_args.kwargs["data"][0]
        self.assertEqual(payload["dish_id"], 1)
        self.assertIn("过敏原:花生", payload["text"])

    def test_insert_failure_preserves_old_collection(self):
        client = self.client()
        client.insert.side_effect = RuntimeError("insertion failed")
        with patch.object(milvus, "fetch_dishes", return_value=[dish()]):
            with self.assertRaises(RuntimeError):
                milvus.sync_dishes(client, model=self.model())
        client.alter_alias.assert_not_called()
        client.create_alias.assert_not_called()
        client.drop_collection.assert_called_once()
        client.release_collection.assert_called_once()

    def test_count_mismatch_does_not_publish(self):
        client = self.client()
        client.query.return_value = [{"count(*)": 0}]
        with patch.object(milvus, "fetch_dishes", return_value=[dish()]):
            with self.assertRaises(RuntimeError):
                milvus.sync_dishes(client, model=self.model())
        client.alter_alias.assert_not_called()

    def test_empty_menu_publishes_empty_collection(self):
        client = self.client(count=0)
        model = self.model()
        with patch.object(milvus, "fetch_dishes", return_value=[]):
            milvus.sync_dishes(client, model=model)
        client.insert.assert_not_called()
        model.embed_documents.assert_not_called()
        client.alter_alias.assert_called_once()

    def test_utf8_limit_and_allergens_are_preserved(self):
        model = self.model()
        data = milvus.prepare_dishes([dish("菜" * 4000)], model=model)
        self.assertIn("过敏原:花生", data[0]["text"])
        with self.assertRaises(ValueError):
            milvus.prepare_dishes([dish("菜" * 22000)], model=model)

    def test_invalid_vector_dimension_rejected(self):
        model = self.model()
        model.embed_documents.return_value = [[1.0]]
        with self.assertRaises(ValueError):
            milvus.prepare_dishes([dish()], model=model)

    def test_second_sync_uses_alter_alias(self):
        client = self.client()
        with patch.object(milvus, "fetch_dishes", return_value=[dish()]):
            milvus.sync_dishes(client, model=self.model())
            milvus.sync_dishes(client, model=self.model())
        self.assertEqual(client.alter_alias.call_count, 2)
        client.create_alias.assert_not_called()

    def test_first_sync_creates_alias(self):
        client = self.client()
        client.list_aliases.return_value = {"aliases": [], "db_name": "smartorder"}
        client.list_aliases.side_effect = None
        with patch.object(milvus, "fetch_dishes", return_value=[dish()]):
            milvus.sync_dishes(client, model=self.model())
        client.create_alias.assert_called_once()
        client.alter_alias.assert_not_called()

    def test_cleanup_preserves_possibly_published_collection(self):
        client = self.client()
        client.list_aliases.side_effect = None
        client.list_aliases.return_value = {"aliases": ["menu_items_live"]}
        milvus.cleanup_unpublished(client, "menu_items_v_" + "a" * 32)
        client.release_collection.assert_not_called()
        client.drop_collection.assert_not_called()

    def test_unknown_alias_contract_refuses_cleanup(self):
        client = self.client()
        client.list_aliases.side_effect = None
        client.list_aliases.return_value = {"unexpected": []}
        milvus.cleanup_unpublished(client, "menu_items_v_" + "a" * 32)
        client.drop_collection.assert_not_called()

    def test_gc_uses_timestamp_and_protects_all_aliases(self):
        client = self.client()
        active = "menu_items_v_" + "0" * 32
        other = "menu_items_v_" + "1" * 32
        newest_backup = "menu_items_v_" + "2" * 32
        oldest = "menu_items_v_" + "f" * 32
        client.list_collections.return_value = [oldest, newest_backup, active, other, "menu_items"]
        timestamps = {oldest: 1, newest_backup: 4, active: 3, other: 2}
        client.describe_collection.side_effect = lambda n: {"created_timestamp": timestamps[n]}
        client.describe_alias.side_effect = lambda a: {"collection_name": active if a == "menu_items_live" else other}
        client.list_aliases.side_effect = lambda collection_name="": {"aliases": [] if collection_name else ["menu_items_live", "other_live"]}
        milvus.gc_old_versions(client, "menu_items", keep=3)
        client.drop_collection.assert_called_once_with(collection_name=oldest)
        self.assertEqual({call.kwargs["collection_name"] for call in client.release_collection.call_args_list}, {oldest, newest_backup})

    def test_gc_without_timestamp_refuses_deletion(self):
        client = self.client()
        client.list_collections.return_value = ["menu_items_v_" + "a" * 32]
        client.describe_collection.return_value = {}
        with self.assertRaises(ValueError):
            milvus.gc_old_versions(client, "menu_items")
        client.release_collection.assert_not_called()
        client.drop_collection.assert_not_called()


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}
    def pipeline(self, **kwargs):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def watch(self, *keys): pass
    async def smembers(self, key): return self.redis.sets.get(key, set()).copy()
    def multi(self): pass
    def delete(self, key): self.ops.append(("delete", key, None))
    def hset(self, key, mapping): self.ops.append(("hset", key, mapping))
    def sadd(self, key, value): self.ops.append(("sadd", key, value))
    async def execute(self):
        for op, key, value in self.ops:
            if op == "delete":
                self.redis.hashes.pop(key, None)
                self.redis.sets.pop(key, None)
            elif op == "hset": self.redis.hashes.setdefault(key, {}).update(value)
            else: self.redis.sets.setdefault(key, set()).add(value)


class FAQSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_update_and_clear(self):
        r = FakeRedis()
        a = dict(id="a", question="问题", answer="回答")
        b = dict(id="b", question="问题2", answer="回答2")
        self.assertEqual((await faq.sync_faq_to_redis(r, items=[a, b]))[0], 2)
        a["answer"] = "新回答"
        self.assertEqual((await faq.sync_faq_to_redis(r, items=[a]))[0], 1)
        self.assertNotIn("faq:item:b", r.hashes)
        self.assertEqual(r.hashes["faq:item:a"]["answer"], "新回答")
        await faq.sync_faq_to_redis(r, items=[])
        self.assertNotIn("faq:ids", r.sets)
        self.assertNotIn("faq:item:a", r.hashes)
        self.assertEqual(r.hashes["faq:meta"]["count"], "0")

    async def test_invalid_input_does_not_modify_data(self):
        r = FakeRedis()
        with self.assertRaises(ValueError):
            await faq.sync_faq_to_redis(r, items=[dict(id="bad")])
        self.assertFalse(r.hashes)

    async def test_duplicate_id_rejected(self):
        r = FakeRedis()
        item = dict(id="a", question="问题", answer="回答")
        with self.assertRaises(ValueError):
            await faq.sync_faq_to_redis(r, items=[item, item])


if __name__ == "__main__":
    unittest.main()

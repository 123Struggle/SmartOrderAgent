import os
from pathlib import Path
from uuid import uuid4
from functools import wraps
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.locking import exclusive_lock

import pymysql
from dotenv import load_dotenv
from langchain_huggingface import HuggingFaceEmbeddings
from pymilvus import DataType, MilvusClient

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
MODEL_PATH = str(Path(__file__).resolve().parents[1] / "models" / "bge-m3")
TEXT_MAX_BYTES = 65535
VECTOR_DIM = 1024
# 集合名与检索别名的唯一来源；消费端必须用 live_alias()，不要硬编码集合名。
DEFAULT_COLLECTION_NAME = os.getenv("MILVUS_COLLECTION_NAME", "menu_items")
_embedding_model = None


def live_alias(collection_name: str = DEFAULT_COLLECTION_NAME) -> str:
    """检索统一入口别名。

    每次同步会新建 <name>_v_<uuid> 版本集合，外部只应通过别名访问，
    否则会读到被回收的旧版本或已不存在的集合。
    """
    return f"{collection_name}_live"


def get_embedding_model():
    """仅在需要向量计算时加载模型，导入模块不加载权重。"""
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = HuggingFaceEmbeddings(model_name=MODEL_PATH)
    return _embedding_model


def build_collection(client: MilvusClient, collection_name: str):
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="dish_id", is_primary=True, datatype=DataType.INT64)
    schema.add_field(field_name="text_vector", datatype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
    schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=TEXT_MAX_BYTES)
    index_params = MilvusClient.prepare_index_params()
    index_params.add_index(
        field_name="text_vector", index_type="HNSW", metric_type="L2",
        params={"M": 16, "efConstruction": 64},
    )
    client.create_collection(
        collection_name=collection_name, schema=schema, index_params=index_params,
    )


def fetch_dishes():
    password = os.getenv("MYSQL_USER_PASSWORD")
    if not password:
        raise ValueError("缺少 MYSQL_USER_PASSWORD，请在 .env 中配置")
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER_NAME", "root"),
        password=password,
        database=os.getenv("MYSQL_DB_NAME", "menu"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        charset="utf8mb4", connect_timeout=10, read_timeout=30, write_timeout=30,
    )
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("""
                SELECT id, dish_name, price, description, category, spice_level,
                       flavor, main_ingredients, cooking_method, is_vegetarian, allergens
                FROM menu_items WHERE is_available = 1 ORDER BY id
            """)
            return cursor.fetchall()
    finally:
        conn.close()


def prepare_dishes(rows, model=None):
    labels = {
        "dish_name": "菜品名称", "price": "价格", "description": "描述",
        "category": "分类", "spice_level": "辣度", "flavor": "口味",
        "main_ingredients": "主要食材", "cooking_method": "烹饪方法",
        "is_vegetarian": "是否为素食", "allergens": "过敏原",
    }
    data = []
    ids = set()
    for row in rows:
        dish_id = row["id"]
        if not isinstance(dish_id, int) or dish_id in ids:
            raise ValueError(f"菜品 ID 无效或重复: {dish_id}")
        ids.add(dish_id)
        text = "".join(
            f"{label}:{row[key] if row[key] is not None else '未提供'}\n"
            for key, label in labels.items()
        )
        if len(text.encode("utf-8")) > TEXT_MAX_BYTES:
            raise ValueError(f"菜品 {dish_id} 文本超过 {TEXT_MAX_BYTES} 字节；未修改线上数据")
        data.append({"dish_id": dish_id, "text": text})
    if data:
        model = model if model is not None else get_embedding_model()
        print(f"正在计算 {len(data)} 道菜品的向量...", flush=True)
        vectors = model.embed_documents([item["text"] for item in data])
        if len(vectors) != len(data) or any(len(v) != VECTOR_DIM for v in vectors):
            raise ValueError("模型返回的向量数量或维度不符合 schema")
        for item, vector in zip(data, vectors):
            item["text_vector"] = vector
    return data


def list_alias_names(client, collection_name=""):
    """兼容 PyMilvus 的字典返回值与旧的列表返回值；未知契约拒绝回收。"""
    raw = client.list_aliases(collection_name=collection_name)
    aliases = raw.get("aliases") if isinstance(raw, dict) else raw
    if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
        raise TypeError("Milvus list_aliases 返回值不符合预期")
    return aliases


def cleanup_unpublished(client, collection_name):
    """失败时先核对所有别名；状态不确定时保留集合，避免误删已发布数据。"""
    try:
        if client.has_collection(collection_name) and not list_alias_names(client, collection_name):
            client.release_collection(collection_name=collection_name)
            client.drop_collection(collection_name=collection_name)
            print(f"已清理未发布集合：{collection_name}", flush=True)
    except Exception as exc:
        print(f"警告：无法确认或清理失败集合 {collection_name}，已保留：{exc}", flush=True)


def gc_old_versions(client, prefix, keep=2):
    """按服务端创建时间保留版本；保护所有别名引用，释放备用版本内存。

    仅管理严格匹配本脚本 UUID 命名规则的集合，要求同步任务串行执行。
    """
    if not isinstance(keep, int) or keep < 1:
        raise ValueError("keep 必须至少为 1")
    version_prefix = prefix + "_v_"
    versions = []
    for name in client.list_collections():
        suffix = name.removeprefix(version_prefix)
        if name.startswith(version_prefix) and len(suffix) == 32 and all(c in "0123456789abcdef" for c in suffix):
            info = client.describe_collection(name)
            timestamp = info.get("created_timestamp")
            if not isinstance(timestamp, int) or timestamp <= 0:
                raise ValueError(f"集合 {name} 缺少创建时间，取消版本回收")
            versions.append((timestamp, name))
    protected = {
        client.describe_alias(alias)["collection_name"]
        for alias in list_alias_names(client)
    }
    protected_versions = {name for _, name in versions if name in protected}
    candidates = sorted((v for v in versions if v[1] not in protected), reverse=True)
    backups = {name for _, name in candidates[:max(0, keep - len(protected_versions))]}
    for _, name in candidates:
        # 删除前再次检查，防止误处理其他别名引用的集合。
        if list_alias_names(client, name):
            continue
        client.release_collection(collection_name=name)
        if name not in backups:
            client.drop_collection(collection_name=name)
            print(f"已回收旧版本集合：{name}", flush=True)


def serialized_sync(function):
    @wraps(function)
    def wrapped(client, collection_name=DEFAULT_COLLECTION_NAME, **kwargs):
        key = "menu:" + os.getenv("MILVUS_URI", "http://localhost:19530") + ":" + os.getenv("MILVUS_DB_NAME", "smartorder") + ":" + collection_name
        with exclusive_lock(key):
            return function(client, collection_name, **kwargs)
    return wrapped


@serialized_sync
def sync_dishes(
    client: MilvusClient,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    *,
    model=None,
):
    """写入版本集合、验证后切换别名；旧集合保留用于恢复。

    检索统一使用 live_alias(collection_name) 返回的别名，
    不与已有同名实体集合冲突。
    """
    data = prepare_dishes(fetch_dishes(), model=model)
    alias = live_alias(collection_name)
    staging = f"{collection_name}_v_{uuid4().hex}"
    try:
        build_collection(client, staging)
        if data:
            result = client.insert(collection_name=staging, data=data)
            if result.get("insert_count") != len(data):
                raise RuntimeError("Milvus 插入数量不匹配，未切换检索别名")
        client.flush(collection_name=staging)
        client.load_collection(collection_name=staging)
        count_result = client.query(
            collection_name=staging, filter="", output_fields=["count(*)"],
            consistency_level="Strong",
        )
        if not count_result or count_result[0].get("count(*)") != len(data):
            raise RuntimeError("Milvus 数据校验失败，未切换检索别名")
        if alias in list_alias_names(client):
            client.alter_alias(collection_name=staging, alias=alias)
        else:
            client.create_alias(collection_name=staging, alias=alias)
    except Exception:
        cleanup_unpublished(client, staging)
        raise
    print(f"同步成功：{len(data)} 道菜品；检索入口：{alias}；版本集合：{staging}", flush=True)
    try:
        gc_old_versions(client, collection_name)
    except Exception as exc:
        # 发布已成功，回收失败必须可见，但不把发布误报为失败。
        print(f"警告：版本已发布，但回收未完成：{exc}", flush=True)
    return alias


def test_query_data(client: MilvusClient, collection_name: str, query: str, *, model=None):
    print(f"语义搜索: {query}", flush=True)
    model = model if model is not None else get_embedding_model()
    results = client.search(
        collection_name=collection_name, data=[model.embed_query(query)], limit=1,
        output_fields=["dish_id", "text"], consistency_level="Strong",
        search_params={"metric_type": "L2", "params": {"ef": 64}},
    )
    if results and results[0]:
        hit = results[0][0]
        entity = hit["entity"]
        print(f"最佳匹配菜品 ID: {entity['dish_id']}\n{entity['text']}", flush=True)
    else:
        print("没有可用菜品", flush=True)
    return results

def run_menu_sync() -> dict:
    uri = os.getenv("MILVUS_URI", "http://localhost:19530")
    token = os.getenv("MILVUS_TOKEN", "")
    target_db = os.getenv("MILVUS_DB_NAME", "smartorder")
    collection = os.getenv("MILVUS_COLLECTION_NAME", DEFAULT_COLLECTION_NAME)
    admin_client = MilvusClient(uri=uri, token=token, timeout=30)
    try:
        if target_db not in admin_client.list_databases():
            admin_client.create_database(target_db)
    finally:
        admin_client.close()
    client = MilvusClient(uri=uri, token=token, db_name=target_db, timeout=30)
    try:
        alias = sync_dishes(client, collection)
        return {"status": "success", "collection": collection, "alias": alias}
    finally:
        client.close()


def main():
    uri = os.getenv("MILVUS_URI", "http://localhost:19530")
    token = os.getenv("MILVUS_TOKEN", "")
    target_db = os.getenv("MILVUS_DB_NAME", "smartorder")
    admin_client = MilvusClient(uri=uri, token=token, timeout=30)
    try:
        if target_db not in admin_client.list_databases():
            admin_client.create_database(target_db)
    finally:
        admin_client.close()
    client = MilvusClient(uri=uri, token=token, db_name=target_db, timeout=30)
    try:
        alias = sync_dishes(client)
        test_query_data(client, alias, "我想吃微辣且酸甜的肉")
    finally:
        client.close()


if __name__ == "__main__":
    main()

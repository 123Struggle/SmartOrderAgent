import os
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from redis.asyncio import Redis
from redis.exceptions import WatchError
from dotenv import load_dotenv

# 加载 .env 中的环境变量
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# 预设的静态常见问题对
FAQ_ITEMS: List[Dict[str, str]] = [
    {"id": "business_hours", "question": "营业时间是什么时候？", "answer": "我们的营业时间是每天早9点至晚10点。"},
    {"id": "address", "question": "位置在哪里？", "answer": "我们位于北京市中关村1号。"},
    {"id": "phone", "question": "联系方式是什么？", "answer": "您可以添加官方微信 atguiguaurant_123，或拨打电话 010-10293847。"},
    {"id": "parking", "question": "停车方便吗？", "answer": "可优先使用附近公共停车场或路边划线车位；车位实时情况建议电话确认：010-10293847。"},
    {"id": "promotion", "question": "有什么优惠活动？", "answer": "优惠活动：单次消费满200九折，满500八折。"},
]

async def _get_redis_client() -> Redis:
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    db = int(os.getenv("REDIS_DB", "0"))
    password = os.getenv("REDIS_PASSWORD") or None
    return Redis(host=host, port=port, db=db, password=password, decode_responses=False, socket_connect_timeout=10, socket_timeout=30)

async def sync_faq_to_redis(
    redis_client=None,
    namespace: str = "faq",
    items: Optional[List[Dict[str, str]]] = None,
) -> Tuple[int, str]:
    """全量替换命名空间中的 FAQ；返回实际 FAQ 条数，空列表表示清空。"""
    if not namespace:
        raise ValueError("namespace 不能为空")
    faq_items = FAQ_ITEMS if items is None else items
    valid_items = {}
    for item in faq_items:
        if any(not isinstance(item.get(k), str) or not item[k].strip()
               for k in ("id", "question", "answer")):
            raise ValueError("FAQ 必须包含非空字符串 id、question、answer")
        if item["id"] in valid_items:
            raise ValueError(f"FAQ ID 重复: {item['id']}")
        valid_items[item["id"]] = item
    owned = redis_client is None
    r = await _get_redis_client() if owned else redis_client
    ids_key = f"{namespace}:ids"
    meta_key = f"{namespace}:meta"
    try:
        for attempt in range(5):
            try:
                async with r.pipeline(transaction=True) as pipe:
                    await pipe.watch(ids_key, meta_key)
                    old_ids = await pipe.smembers(ids_key)
                    old_ids = {x.decode("utf-8") if isinstance(x, bytes) else x for x in old_ids}
                    pipe.multi()
                    for faq_id in old_ids - valid_items.keys():
                        pipe.delete(f"{namespace}:item:{faq_id}")
                    pipe.delete(ids_key)
                    for faq_id, item in valid_items.items():
                        key = f"{namespace}:item:{faq_id}"
                        pipe.delete(key)
                        pipe.hset(key, mapping={k: item[k] for k in ("id", "question", "answer")})
                        pipe.sadd(ids_key, faq_id)
                    pipe.hset(meta_key, mapping={"count": str(len(valid_items))})
                    await pipe.execute()
                    return len(valid_items), ids_key
            except WatchError:
                if attempt == 4:
                    raise
    finally:
        if owned:
            await r.aclose()

if __name__ == "__main__":
    import asyncio
    async def main():
        r = await _get_redis_client()
        try:
            written, ids_key = await sync_faq_to_redis(redis_client=r, namespace=os.getenv("FAQ_REDIS_NAMESPACE", "faq"))
            print(f"FAQ 数据同步至 Redis 成功！FAQ 条数: {written}, 键名: {ids_key}")
        finally:
            await r.aclose()
        
    asyncio.run(main())
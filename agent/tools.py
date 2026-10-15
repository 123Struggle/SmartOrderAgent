import logging
import os
from pathlib import Path
from typing import List, Optional

import pymysql
from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_huggingface import HuggingFaceEmbeddings
from pydantic import BaseModel, Field
from pymilvus import MilvusClient
from redis import Redis

from agent.milvus_data_sync import live_alias

# 与其余模块保持一致的显式路径加载，不依赖当前工作目录
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger(__name__)

# ================================
# 1. 初始化模型与持久化客户端
# ================================
MODEL_PATH = str(Path(__file__).resolve().parents[1] / "models" / "bge-m3")
# 检索必须走别名：版本集合名 <name>_v_<uuid> 会被回收，不能直接引用
MILVUS_COLLECTION_ALIAS = live_alias()

_milvus_client = None


def get_milvus_client():
    global _milvus_client
    if _milvus_client is None:
        _milvus_client = MilvusClient(
            uri=os.getenv("MILVUS_URI", "http://localhost:19530"),
            token=os.getenv("MILVUS_TOKEN", ""),
            db_name=os.getenv("MILVUS_DB_NAME", "smartorder"), timeout=30,
        )
    return _milvus_client


# 懒加载：导入本模块不加载权重，首次向量计算时才载入
_embedding_model: Optional[HuggingFaceEmbeddings] = None
_redis_client: Optional[Redis] = None


def get_embedding_model() -> HuggingFaceEmbeddings:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = HuggingFaceEmbeddings(model_name=MODEL_PATH)
    return _embedding_model


def get_mysql_connection():
    """获取 MySQL 连接。

    显式指定 utf8mb4：不能依赖客户端默认值，否则容器内 LANG 为空时
    可能协商成 latin1，导致中文被替换为 "?"。
    """
    password = os.getenv("MYSQL_USER_PASSWORD")
    if not password:
        raise ValueError("缺少 MYSQL_USER_PASSWORD，请在 .env 中配置")
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER_NAME", "root"),
        password=password,
        database=os.getenv("MYSQL_DB_NAME", "menu"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def get_redis_client() -> Redis:
    """获取同步 Redis 客户端。

    进程内复用同一个连接池；每次调用都新建 Redis() 会持续泄漏连接。
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=30,
        )
    return _redis_client


# ================================
# 2. 工具 1：查询特色菜品（MySQL）
# ================================
@tool
def get_featured_dishes() -> str:
    """
    当顾客询问餐厅有什么特色菜、招牌菜、主推菜品时调用此工具。
    返回所有标记为特色的菜品列表及详细信息。
    """
    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT dish_name, price, description, category, flavor, spice_level
                FROM menu_items
                WHERE is_featured = 1 AND is_available = 1
                ORDER BY id
            """
            cursor.execute(sql)
            dishes = cursor.fetchall()
            if not dishes:
                return "暂无特色菜品信息。"

            result = "本店特色菜品推荐：\n"
            for d in dishes:
                result += (
                    f"- {d['dish_name']}（{d['price']}元，{d['category']}）："
                    f"{d['description']}，口味：{d['flavor']}\n"
                )
            return result
    finally:
        conn.close()


# ================================
# 3. 工具 2：常见问题解答（Redis FAQ）
# ================================
@tool
def faq_tool(query: str) -> str:
    """
    当顾客询问关于餐厅的基本信息（如营业时间、具体地址、联系电话、停车情况、优惠活动等）时调用此工具。
    参数 query: 顾客询问的核心问题。
    """
    r = get_redis_client()
    namespace = os.getenv("FAQ_REDIS_NAMESPACE", "faq")
    ids_key = f"{namespace}:ids"
    faq_ids = r.smembers(ids_key)

    if not faq_ids:
        return "未能检索到相关常见问题解答。"

    # 简单语义关键词匹配
    matched_answer = None
    for faq_id in faq_ids:
        item = r.hgetall(f"{namespace}:item:{faq_id}")
        q = item.get("question", "")
        # 如果问题关键词重合
        if any(keyword in query for keyword in ["时间", "营业", "几点"]) and "时间" in q:
            matched_answer = item.get("answer")
            break
        elif any(keyword in query for keyword in ["地址", "位置", "在哪"]) and ("位置" in q or "地址" in q):
            matched_answer = item.get("answer")
            break
        elif any(keyword in query for keyword in ["电话", "微信", "联系"]) and "联系方式" in q:
            matched_answer = item.get("answer")
            break
        elif any(keyword in query for keyword in ["停车", "车位"]) and "停车" in q:
            matched_answer = item.get("answer")
            break
        elif any(keyword in query for keyword in ["优惠", "打折", "活动"]) and "优惠" in q:
            matched_answer = item.get("answer")
            break

    if matched_answer:
        return matched_answer
    return "抱歉，未找到关于该问题的确切信息，建议致电联系餐厅人工确认。"


# ================================
# 4. 工具 3：菜品个性化偏好推荐（Milvus 语义检索）
# ================================
@tool
def recommend_dishes_by_preference(user_preference: str) -> str:
    """
    当顾客提出具体的口味、食材偏好、忌口或特殊需求时调用（例如：想吃酸甜不辣的素菜、想吃清淡减脂的菜品等）。
    参数 user_preference: 顾客的具体偏好描述。
    """
    query_vector = get_embedding_model().embed_query(user_preference)
    try:
        search_res = get_milvus_client().search(
            collection_name=MILVUS_COLLECTION_ALIAS,
            data=[query_vector],
            limit=2,
            output_fields=["text"],
            consistency_level="Strong",
            search_params={"metric_type": "L2", "params": {"ef": 64}},
        )
    except Exception as exc:
        # 记录真实原因，同时让对话能继续（不要把异常吞成静默成功）
        logger.exception("Milvus 菜品检索失败 (alias=%s)", MILVUS_COLLECTION_ALIAS)
        return f"菜品检索暂时不可用，请稍后再试或改问本店特色菜。（错误类型：{type(exc).__name__}）"

    if not search_res or not search_res[0]:
        return "未找到完全匹配的菜品，建议查看本店特色菜。"

    recommendations = "根据您的偏好，为您推荐以下菜品：\n\n"
    for match in search_res[0]:
        entity = match.get("entity", {}) if isinstance(match, dict) else getattr(match, "entity", {})
        text = entity.get("text", "") if isinstance(entity, dict) else getattr(entity, "text", "")
        recommendations += f"{text}\n---\n"
    return recommendations


# ================================
# 5. 工具 4：处理预订桌位（MySQL）
# ================================
class ReservationInput(BaseModel):
    num_people: int = Field(description="客人人数（整数）")
    num_children: int = Field(default=0, description="儿童人数（整数，默认0）")
    arrival_time: str = Field(description="到达时间，格式：YYYY-MM-DD HH:MM:SS")
    seat_preference: Optional[str] = Field(default=None, description="座位偏好（例如：靠窗、包间、大桌、安静区域等）")
    main_dish_preference: Optional[str] = Field(default=None, description="主菜偏好或已点菜品")
    other_comments: Optional[str] = Field(default=None, description="其他额外需求或备注")


@tool(args_schema=ReservationInput)
def make_reservation(
    num_people: int,
    arrival_time: str,
    num_children: int = 0,
    seat_preference: Optional[str] = None,
    main_dish_preference: Optional[str] = None,
    other_comments: Optional[str] = None,
) -> str:
    """
    收集完整的预订信息后调用此工具创建待确认草稿。
    本工具不会写入正式预订单，顾客必须在页面核对明细并点击确认预订。
    """
    from agent.reservations import prepare_reservation
    try:
        draft = prepare_reservation(dict(
            num_people=num_people, num_children=num_children, arrival_time=arrival_time,
            seat_preference=seat_preference, main_dish_preference=main_dish_preference,
            other_comments=other_comments,
        ))
    except ValueError as exc:
        return f"预订信息有误，未提交：{exc}"
    import json
    return "预订草稿已生成，尚未提交或锁定席位。请顾客核对页面上的预订明细并点击确认预订。明细：" + json.dumps(draft, ensure_ascii=False)

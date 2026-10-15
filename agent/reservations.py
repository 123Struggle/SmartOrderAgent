"""模型只能建立草稿；明确确认由 API 完成，MySQL 事务保证重试不重复下单。"""
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo

from agent import session_store

current_session = ContextVar("reservation_session", default=None)
FIELDS = ("num_people", "num_children", "arrival_time", "seat_preference", "main_dish_preference", "other_comments")


def normalize_details(details):
    data = dict(details)
    if data["num_people"] <= 0 or not 0 <= data.get("num_children", 0) <= data["num_people"]:
        raise ValueError("用餐人数或儿童人数不正确")
    raw = data["arrival_time"].strip()
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            break
        except ValueError:
            continue
    if parsed is None or parsed <= datetime.now(ZoneInfo("Asia/Shanghai")):
        raise ValueError("请填写未来的明确日期时间，格式 YYYY-MM-DD HH:MM:SS（北京时间）")
    data["arrival_time"] = parsed.strftime("%Y-%m-%d %H:%M:%S")
    data.setdefault("num_children", 0)
    for key, limit in (("seat_preference", 100), ("main_dish_preference", 100), ("other_comments", 10000)):
        value = data.setdefault(key, None)
        if value is not None and len(value) > limit:
            raise ValueError(f"{key} 超过 {limit} 个字符")
    return {key: data[key] for key in FIELDS}


def prepare_reservation(details):
    session_id = current_session.get()
    if not session_id:
        raise ValueError("缺少已认证会话，不能创建预订草稿")
    return session_store.create_draft(session_id, normalize_details(details))


def submit_reservation(session_id, draft_id):
    draft = session_store.get_draft(session_id, draft_id)
    if draft["order_id"] is not None:
        return draft
    # 懒导入，避免 API 探针/会话管理依赖外部服务。
    from agent.tools import get_mysql_connection
    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            # 相同草稿的并发确认在此等待同一行锁；同一事务写订单和幂等记录。
            cursor.execute("INSERT INTO reservation_submission (request_id) VALUES (%s) ON DUPLICATE KEY UPDATE request_id=request_id", (draft_id,))
            cursor.execute("SELECT order_id FROM reservation_submission WHERE request_id=%s FOR UPDATE", (draft_id,))
            order_id = cursor.fetchone()["order_id"]
            if order_id is None:
                data = normalize_details(draft["details"])
                cursor.execute("""INSERT INTO reservation_order
                    (num_people, num_children, arrival_time, seat_preference, main_dish_preference, other_comments)
                    VALUES (%s, %s, %s, %s, %s, %s)""", tuple(data[k] for k in FIELDS))
                order_id = cursor.lastrowid
                cursor.execute("UPDATE reservation_submission SET order_id=%s WHERE request_id=%s", (order_id, draft_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    session_store.mark_submitted(session_id, draft_id, order_id)
    return session_store.get_draft(session_id, draft_id)

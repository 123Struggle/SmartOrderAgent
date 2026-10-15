"""为已存在的业务库添加预订幂等表，不重跑种子数据。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.tools import get_mysql_connection


def main():
    conn = get_mysql_connection()
    try:
        sql = (Path(__file__).resolve().parents[1] / "migrations" / "001_reservation_submission.sql").read_text(encoding="utf-8")
        with conn.cursor() as cursor:
            for statement in sql.split(";"):
                if statement.strip(): cursor.execute(statement)
        conn.commit()
        print("预订幂等表迁移完成（已有订单保持不变）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

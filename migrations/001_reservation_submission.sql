-- 新增幂等提交记录；与正式订单在同一 MySQL 事务中写入。
CREATE TABLE IF NOT EXISTS reservation_submission (
    request_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    order_id INT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_submission_order FOREIGN KEY (order_id) REFERENCES reservation_order(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

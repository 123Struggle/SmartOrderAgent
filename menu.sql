SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS `menu` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `menu`;

CREATE TABLE IF NOT EXISTS `menu_items`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '菜品ID，主键自增',
  `dish_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '菜品名称',
  `price` decimal(8, 2) NOT NULL COMMENT '价格（元）',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '菜品描述',
  `category` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '菜品分类',
  `spice_level` tinyint NULL DEFAULT 0 COMMENT '辣度等级：0-不辣，1-微辣，2-中辣，3-重辣',
  `flavor` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '口味特点',
  `main_ingredients` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '主要食材，多个食材用逗号分隔',
  `cooking_method` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '烹饪方法',
  `is_vegetarian` tinyint(1) NULL DEFAULT 0 COMMENT '是否素食：0-否，1-是',
  `allergens` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '过敏原信息，多个过敏原用逗号分隔',
  `is_available` tinyint(1) NULL DEFAULT 1 COMMENT '是否可供应：0-不可用，1-可用',
  `is_featured` tinyint(1) NULL DEFAULT 0 COMMENT '是否特色菜：0-否，1-是',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_category`(`category`) USING BTREE,
  INDEX `idx_is_available`(`is_available`) USING BTREE,
  INDEX `idx_is_vegetarian`(`is_vegetarian`) USING BTREE,
  INDEX `idx_price`(`price`) USING BTREE,
  INDEX `idx_spice_level`(`spice_level`) USING BTREE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '菜单表';

CREATE TABLE IF NOT EXISTS `reservation_order` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '预订单ID，主键自增',
  `num_people` int NOT NULL COMMENT '客人人数',
  `num_children` int NOT NULL COMMENT '儿童人数',
  `arrival_time` datetime NOT NULL COMMENT '到达时间',
  `seat_preference` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '座位偏好',
  `main_dish_preference` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '主菜偏好',
  `other_comments` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '其他备注需求',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='预订单表';

-- 插入系统运行所需的初始菜品测试数据
INSERT INTO `menu_items` (`dish_name`, `price`, `description`, `category`, `spice_level`, `flavor`, `main_ingredients`, `cooking_method`, `is_vegetarian`, `allergens`, `is_available`, `is_featured`)
SELECT '宫保鸡丁', 28.00, '经典川菜，鸡肉丁配花生米，酸甜微辣，口感丰富', '川菜', 1, '酸甜微辣', '鸡肉,花生,干辣椒', '爆炒', 0, '花生', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM `menu_items` WHERE `dish_name` = '宫保鸡丁');

INSERT INTO `menu_items` (`dish_name`, `price`, `description`, `category`, `spice_level`, `flavor`, `main_ingredients`, `cooking_method`, `is_vegetarian`, `allergens`, `is_available`, `is_featured`)
SELECT '麻婆豆腐', 18.00, '四川传统名菜，嫩滑豆腐配麻辣汤汁，下饭神器', '川菜', 3, '麻辣浓郁', '豆腐,牛肉碎,花椒,豆瓣酱', '烧制', 0, '无', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM `menu_items` WHERE `dish_name` = '麻婆豆腐');

INSERT INTO `menu_items` (`dish_name`, `price`, `description`, `category`, `spice_level`, `flavor`, `main_ingredients`, `cooking_method`, `is_vegetarian`, `allergens`, `is_available`, `is_featured`)
SELECT '清炒时蔬', 15.00, '新鲜时令蔬菜清炒，营养健康，口感清爽', '素菜', 0, '清淡爽口', '时令青菜,大蒜', '清炒', 1, '无', 1, 0
WHERE NOT EXISTS (SELECT 1 FROM `menu_items` WHERE `dish_name` = '清炒时蔬');

INSERT INTO `menu_items` (`dish_name`, `price`, `description`, `category`, `spice_level`, `flavor`, `main_ingredients`, `cooking_method`, `is_vegetarian`, `allergens`, `is_available`, `is_featured`)
SELECT '蒜蓉西兰花', 12.00, '新鲜西兰花配蒜蓉，营养丰富，适合减肥人群', '素菜', 0, '蒜香清脆', '西兰花,大蒜', '清炒', 1, '无', 1, 0
WHERE NOT EXISTS (SELECT 1 FROM `menu_items` WHERE `dish_name` = '蒜蓉西兰花');

INSERT INTO `menu_items` (`dish_name`, `price`, `description`, `category`, `spice_level`, `flavor`, `main_ingredients`, `cooking_method`, `is_vegetarian`, `allergens`, `is_available`, `is_featured`)
SELECT '红烧鲈鱼', 45.00, '新鲜鲈鱼红烧制作，肉质鲜美，营养丰富', '鲁菜', 0, '鲜香浓郁', '新鲜鲈鱼,葱姜蒜,生抽', '红烧', 0, '鱼类', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM `menu_items` WHERE `dish_name` = '红烧鲈鱼');

-- 新增幂等提交记录；与正式订单在同一 MySQL 事务中写入。
CREATE TABLE IF NOT EXISTS reservation_submission (
    request_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    order_id INT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_submission_order FOREIGN KEY (order_id) REFERENCES reservation_order(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

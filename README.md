# SmartOrderAgent

当前实现：

- **数据层**：MySQL 菜单读取、BGE-M3 向量化、Milvus 菜品检索、Redis FAQ 全量同步
- **对话层**：LangGraph ReAct Agent（特色菜推荐、偏好推荐、FAQ、餐桌预订写库），
  会话状态持久化到 `checkpoints.db`
- **服务层**：FastAPI（`api/main.py`）+ SSE 流式接口 + 原生 Web 客户端（`web/index.html`）

`[project.scripts]` 里的 `smartorderagent` 命令仍是项目初始化占位入口 —— 业务代码位于仓库根的
`agent/`、`api/`、`web/`，不在 `src/` 打包范围内，需从项目根目录运行。

## 本地运行

使用 Python 3.12 和 `pyproject.toml` / `uv.lock` 安装依赖。Windows 项目虚拟环境示例：

```powershell
# 已有数据库先应用增量迁移（仅创建幂等记录表，不删除订单）
.\.venv\Scripts\python.exe -X utf8 -m agent.migrate

# 启动 API 服务（Web 客户端在 http://127.0.0.1:8000/）
.\.venv\Scripts\python.exe -X utf8 -m uvicorn api.main:app --host 127.0.0.1 --port 8000

# 数据同步脚本
.\.venv\Scripts\python.exe -X utf8 agent/milvus_data_sync.py
.\.venv\Scripts\python.exe -X utf8 agent/redis_data_sync.py

# 测试
.\.venv\Scripts\python.exe -X utf8 -m pytest tests -v
.\.venv\Scripts\python.exe -X utf8 -m pytest tests -m "not integration" -v   # 只跑不需要外部服务的用例
.\.venv\Scripts\python.exe -X utf8 -u tests/integration_check.py             # 端到端集成检查
node tests/web_smoke.cjs                                                   # 前端分块接收及确认流程
```

用 `uv` 时把上面的 `.\.venv\Scripts\python.exe -X utf8` 换成 `uv run python -X utf8`（或直接 `uv run pytest`）。

`tests/test_agent.py` 的 `integration` 测试只需要 MySQL，使用随机隔离数据库验证
并发预订、响应丢失后重试和幂等性，结束只删除本次测试数据库，不按业务订单 ID 范围清理。
`tests/integration_check.py` 另外需要 Redis、Milvus 和本地模型，会验证同步及实际检索。

Windows 控制台默认是 GBK(936) 代码页：无法编码 Emoji，且在 UTF-8 终端下会把中文显示成
乱码或问号。`agent/console.py` 的 `force_utf8_console()` 负责统一切到 UTF-8，已在
`api/main.py` 和 `tests/conftest.py` 接入；直接运行其他脚本时建议统一加 `-X utf8`。

用 `mysql` 命令行查看中文数据时务必显式指定字符集：

```bash
docker exec -it my-mysql mysql --default-character-set=utf8mb4 -uroot -p -e "USE menu; SELECT * FROM reservation_order;"
```

容器内 `LANG` 为空时客户端会协商成 `latin1`，中文只显示为 `?`；**数据本身没有损坏**
（可用 `SELECT HEX(col)` 验证）。

将 BGE-M3 模型放在 `models/bge-m3`（模型文件不提交到 Git）。
集成检查连接真实服务，并创建、清理随机测试数据库和命名空间；需要测试数据库创建权限。
在项目根目录配置 `.env`，变量示例见 `.env.example`。
先启动 MySQL、Redis、Milvus，并用 `menu.sql` 初始化 MySQL 的 `menu` 数据库。
如果修改 `MYSQL_DB_NAME`，初始化 SQL 中的数据库名也需要对应调整。

`menu.sql` 不再删除已有表，按菜品名称补充缺失的示例数据，不覆盖现有菜品。
该脚本不是 schema 迁移工具；已有表结构的变更需要单独迁移。

## 同步约定

- Milvus 每次创建一个版本集合，插入、flush、load、核对记录数量成功后才发布。
- 检索通过 `milvus_data_sync.live_alias()` 返回的别名（默认 `menu_items_live`）；
  代码中禁止硬编码集合名。旧实体集合 `menu_items` 已人工清理。
- 默认按服务端创建时间保留 2 个版本（当前版本及一个备用版本）；备用版本 release 后保留磁盘数据，回滚前需重新 load。
- 所有别名引用的版本都受保护，必要时可超过 2 个；未被别名引用的更旧版本会 release 后删除。
- 失败集合确认未被任何别名引用后会清理；若网络或返回值异常导致状态不确定，则保留并输出警告，需人工核对。
- 回收仅处理 `<collection_name>_v_<32位十六进制UUID>` 集合。直接按版本集合名访问的其他程序不受别名保护，请统一使用别名。
- 旧实体集合 `menu_items` 不在回收范围内（已于改用别名后人工删除）。
- MySQL 没有可用菜品时，发布空集合；不会继续返回旧菜单。
- 菜品使用 MySQL `id` 作为 Milvus `dish_id` 主键。超出 UTF-8 字节上限的文本会报错，不会截断过敏原信息。
- FAQ 为全量替换，`items=None` 使用默认 FAQ，`items=[]` 清空指定命名空间；无效或重复 ID 在写入前拒绝。
- FAQ 写入使用 WATCH 和事务，避免并发同步覆盖删除逻辑；失败会抛出异常。

CLI 和 API 的同步入口共用跨进程文件锁；同机多个 worker 不会同时同步同一个集合，忙时 API 返回 409。
所有进程必须使用同一项目目录或同一个 `APP_LOCK_DIR`。该锁不支持不同机器的分布式部署；多机部署需另加共享锁。
语义相似度只能提供候选菜品；价格、库存、过敏原等下单约束仍需由业务层核对 MySQL。

## 会话与预订接口

- Web 自动通过 `POST /sessions` 获取服务端会话 ID 和独立随机凭证，并保存在当前标签页的 sessionStorage。
- 调用 `/chat`、`/chat/stream`、预订接口必须发送 `Authorization: Bearer <session_token>`。
  仅提供 session_id 不能访问会话，指定其他会话 ID 返回 403。凭证有效期为 7 天。
- SSE 的 data 为 JSON 对象：`token` 携带 text，`reservation` 携带 draft，`done` 表示完成，`error` 表示失败。
  不再把原始文本直接作为 SSE 帧，正文中的空行、引号和 Unicode 可完整传输。
- 模型的 `make_reservation` 工具只创建有效期 30 分钟的草稿，不写正式订单。
  页面展示草稿明细，用户核对后点击“确认预订”，调用 `POST /reservations/{draft_id}/confirm`，请求体为 `{"confirm": true}`。
- API 客户端也可从 `/chat` 响应的 reservation_draft 或 `GET /reservations/pending` 获取待确认草稿。
  文本中说“确认”不触发写库，必须调用独立确认接口。
- 确认接口校验草稿归属，在同一个 MySQL 事务中写订单和 reservation_submission 幂等记录。
  同一草稿重复点击、并发确认或响应丢失后重试只返回同一个订单号。
- 会话凭证哈希和草稿存储于 `.runtime/state.db`；文件锁位于 `.runtime/locks/`，均已被 Git 忽略。
  `APP_STATE_DB`、`APP_CHECKPOINT_DB`、`APP_LOCK_DIR` 可以指定部署或测试路径。

## 管理同步

在 `.env` 设置强随机 `ADMIN_API_TOKEN`，调用 `POST /admin/sync-menu` 时发送 `X-Admin-Token`。
未配置时返回 503，凭证不匹配返回 403。密钥不得放入网页或提交到 Git。
通过 `MILVUS_COLLECTION_NAME` 配置集合前缀，CLI、API 与检索使用相同配置。
Web 和 API 同源，不开放通配跨域凭证访问。

应用更新后需要重启 API；浏览器刷新即可使用新的会话和预订确认流程。

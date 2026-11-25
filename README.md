# 跨境多租户客服工作台（crossborder-cs）

面向 ToB 跨境电商客服的本地可运行交付：工单入库、翻译与草稿、MCP 工具、审核恢复、发送对账、工单诊断、员工培训、附件与 OCR、知识索引发布/回滚。真实外部集成按店铺启用，默认全部关闭，本地使用确定性适配器保证可复现。

## 技术栈

- Python 3.12 + uv（`.venv`）
- FastAPI + Pydantic 2 + Jinja2 模板
- SQLAlchemy 2 异步 + psycopg 3；Alembic 迁移
- PostgreSQL 16（远端实例，经 SSH 隧道访问）
- LangGraph + Postgres checkpointer
- MCP Python SDK（业务只读服务 / 训练只读服务）
- Windows 使用 SelectorEventLoop（`--loop app:selector_loop_factory`）

## 目录

```
app/
  adapters/      Amazon SP-API、渠道、17TRACK、ERP、翻译、OCR、向量索引、本地假实现
  agents/        主流程、业务子图、审核工作流、培训
  api/           入库、认证、工单、诊断、培训、附件、集成状态、知识索引
  mcp/           MCP 服务端、工具与签名上下文
  rag/           过滤、BM25、RRF、证据
  services/      入库、草稿、审核、发送队列、诊断、培训、附件、索引发布
  templates/     工作台 Jinja2 页面
docs/
  contracts/     真实集成合同
  integration-status.md
migrations/      Alembic
scripts/         初始化、播种、各阶段验收、样例回放
tests/           unit / integration / e2e
```

## 首次启动（PowerShell）

```powershell
Copy-Item .env.example .env
# 编辑 .env：数据库连接、密钥、隧道参数。不要提交 .env。
uv sync

# 建立到远端 PostgreSQL 的本地隧道（示例）
ssh -i $HOME\.ssh\crossborder_cs_ed25519 -N `
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
  -L 55432:127.0.0.1:5433 <remote-user>@<remote-host>

# 另开一个终端
uv run python scripts/migrate.py
uv run python scripts/seed.py
uv run python -m app
```

访问 `http://127.0.0.1:8000/`，用种子账号登录；密码取 `.env` 的 `SEED_PASSWORD`。

种子账号：

| 邮箱 | 角色 | 说明 |
| --- | --- | --- |
| `cs.a@example.com` | agent | A 租户日本站客服，有退款权限 |
| `sup.a@example.com` | supervisor | A 租户主管 |
| `kb.a@example.com` | knowledge_admin | A 租户知识管理员 |
| `admin.a@example.com` | system_admin | A 租户系统管理员 |
| `cs.b@example.com` | agent | B 租户客服（用于跨租户验证） |

系统管理员可在 `/employees` 管理**本租户**员工：修改角色、启用/停用、切换退款权限、授予/回收店铺授权，变更写入 `admin_audit` 审计表。为避免失联，不能修改自己的角色或停用自己；也不能移除/停用最后一个系统管理员。

## 可重复的日本站样例

```powershell
uv run python scripts/replay_sample.py --external-message-id JP-DEMO-001
# 并发重复投递，验证幂等去重
uv run python scripts/replay_sample.py --external-message-id JP-DEMO-001 --concurrency 3
```

## 验收

每个阶段先跑对应验收，再进入下一阶段。

```powershell
# 阶段 1：工作台
uv run python scripts/verify_workbench.py

# 阶段 2：翻译/分流/MCP/草稿
uv run python scripts/verify_stage2.py

# 阶段 3：审核恢复、发送对账、结案
uv run python scripts/verify_stage3.py

# 阶段 4：诊断与培训
uv run python scripts/verify_stage4.py

# 阶段 5：附件/OCR/集成状态 + 合同测试
uv run python scripts/verify_stage5.py
uv run pytest tests/unit/test_amazon_spapi_contract.py `
  tests/unit/test_channel_contract.py `
  tests/unit/test_tracking_erp_translate_contract.py -q
uv run pytest tests/integration/test_index_publish_rollback.py `
  tests/integration/test_attachment_ocr.py -q

# 工作台页面：附件上传/OCR、知识索引、集成状态、员工权限与审计
uv run pytest tests/e2e/test_workbench_html.py -q
uv run pytest tests/integration/test_employee_admin.py -q

# 检索评估（生成 docs/retrieval-eval.md）
uv run python scripts/eval_retrieval.py

# 全量回归
uv run pytest -q
```

测试库名必须包含 `_test`，测试会拒绝在其它库上运行。

## 真实集成开关

真实集成默认全部关闭。启用时需要同时满足：

1. 在 `.env` 填写对应 URL/密钥（凭据只来自环境或受控配置）；
2. 将店铺 UUID 写入 `INTEGRATION_ENABLED_STORE_IDS`（逗号分隔，空表示全关）；
3. 打开对应 `*_ENABLED` 开关。

登录后访问 `GET /api/integrations/status` 查看每项的 `enabled / configured / active`；
系统管理员也可直接打开工作台 `/integrations` 页面查看开关、凭据、生效与范围。
各适配器在未启用、缺凭据或店铺未授权时会明确抛 `NotConfigured`，不会静默使用假数据。

工作台内置入口：

- 工单详情「附件与 OCR」卡片：上传附件、下载、按需 OCR；
- `/knowledge`（知识管理员/系统管理员）：发布已批准版本为索引快照、失败保留旧版本、回滚到历史快照；
- `/integrations`（仅系统管理员）：集成开关与生效状态一览。

合同与本地/真实状态：

- [Amazon SP-API](docs/contracts/amazon_sp_api.md)
- [试点渠道](docs/contracts/channel_pilot.md)
- [物流/ERP/翻译](docs/contracts/logistics_erp_translation.md)
- [附件与 OCR](docs/contracts/attachments_ocr.md)
- [知识索引发布/回滚](docs/contracts/vector_index.md)
- [阶段 5 集成状态](docs/integration-status.md)

## 安全与数据边界

- `.env` 与 `.env.*` 已被 `.gitignore` 排除；附件目录 `data/attachments/*` 不入库。
- 禁止把远端 root 密码或数据库密码写入仓库、README、日志或计划文档。
- 数据库只通过本地隧道访问，远端 5432 不对公网开放。
- 附件按租户/店铺/工单授权访问，跨租户/跨工单返回 404，对象路径做存储根目录越界校验。
- 不回写外部业务系统；Amazon/物流/ERP 适配器只读。


# 知识索引发布、切换与回滚

实现：`app/services/index_publish.py`、`app/adapters/vector_index.py`；
API：`app/api/knowledge.py`；测试：`tests/integration/test_index_publish_rollback.py`。

## 发布流程

1. 只接受同一租户、状态为 `approved` 或 `published` 的知识版本；缺失、跨租户或未审核直接拒绝。
2. 读取这些版本的 `knowledge_chunk`，生成文档列表（chunk、版本、文档、校验和、元数据）。
3. 新建 `index_snapshot`：
   - `status=building`
   - `collection={基础名}__{snapshot_id 前 12 位}`（每次发布独立集合，旧集合保留）
   - `previous_snapshot_id` 指向当前 `active` 快照
4. 调用向量索引 `build(collection, documents)`。
5. 成功：快照置 `active`、写 `activated_at`，旧 active 置 `superseded`，完成指针切换。
6. 失败：快照置 `failed` 并记录错误，**不修改当前 active 指针**，上一可用版本继续服务。

## 回滚

`rollback_index(tenant_id, snapshot_id)` 只做指针切换：

- 目标必须属于当前租户且不是 `failed`；
- 目标置 `active`，当前 active 置 `superseded`；
- 目标已是当前 active 时幂等返回 `rolled_back=false`。

因为每次发布使用独立集合，回滚不需要重建索引，也不会受后续文档改动影响。

## API

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| GET | `/api/knowledge/index` | 任意登录员工，按租户隔离 |
| POST | `/api/knowledge/index/publish` | `knowledge_admin` / `system_admin`，需 CSRF |
| POST | `/api/knowledge/index/rollback` | `knowledge_admin` / `system_admin`，需 CSRF |

## 向量索引实现

- 未启用 Milvus 或未配置 `MILVUS_URI`：使用进程内 `FakeVectorIndex`，发布/回滚语义完整可测。
- `MilvusVectorIndex` 合同：`build(collection, documents) -> {"collection", "count"}`；
  当前环境未安装 `pymilvus`，调用会明确抛出 `NotConfigured`，不会静默假装成功。

## 本地验证范围

集成测试覆盖：首次发布 → 二次发布切换并保留旧集合 → 构建失败保留上一 active → 回滚切回旧快照 → 跨租户/未知快照拒绝 → 未审核版本拒绝 → Milvus 未配置时 `NotConfigured` 且回退假索引。

**真实 Milvus 未连接**：本阶段以假索引验证发布与回滚语义。
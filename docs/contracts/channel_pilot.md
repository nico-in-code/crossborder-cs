# 试点客服渠道合同

实现：`app/adapters/channel_http.py`；测试：`tests/unit/test_channel_contract.py`。

## 启用条件

1. `CHANNEL_ENABLED=true`；
2. `CHANNEL_BASE_URL` 与 `CHANNEL_API_KEY` 均有值；
3. 渠道账号解析出的店铺 UUID 在 `INTEGRATION_ENABLED_STORE_IDS` 中。

不满足时抛出 `NotConfigured`。默认应用仍使用 `FakeChannelAdapter`，真实渠道不会自动接管发送。

## 发送

```
POST {CHANNEL_BASE_URL}/messages
X-API-Key: <CHANNEL_API_KEY>
Idempotency-Key: <send_task.idempotency_key>
Content-Type: application/json
```

请求体：

```json
{
  "tenant_id": "uuid",
  "channel_account_id": "uuid",
  "idempotency_key": "ticket:.../review:...",
  "body_hash": "sha256",
  "message": {
    "ticket_id": "uuid",
    "ticket_number": "JP-...",
    "draft_id": "uuid",
    "draft_version": 1,
    "body": "日语回复正文",
    "buyer_external_id": "..."
  }
}
```

成功响应（200/201）：

```json
{"message_id": "外部消息ID", "status": "accepted"}
```

**幂等声明**：同一 `Idempotency-Key` 重复发送时，渠道必须返回同一个 `message_id`，不得创建第二条消息。

## 回执查询

```
GET {CHANNEL_BASE_URL}/messages/{idempotency_key}
X-API-Key: <CHANNEL_API_KEY>
```

- 200：`{"message_id": "...", "status": "accepted|delivered|..."}`
- 404：表示未受理，返回 `None`，由发送队列继续对账。

## 错误映射

| 上游情况 | 异常 / 行为 |
| --- | --- |
| 未启用 / 缺凭据 / 店铺未启用 / 账号无店铺 | `NotConfigured` |
| 401/403 | `ChannelRejected`，发送任务记 `failed` |
| 其它 4xx | `ChannelRejected` |
| 5xx | `UpstreamError`，发送任务进入未知状态并重试 |
| 超时 | `ChannelTimeout`，发送任务记 `unknown`，先查回执再决定重发 |

发送队列（`app/services/send_queue.py`）在超时后按同一幂等键查询回执；查到即 `acked`，查不到则保留未知状态等待对账，绝不盲目重发。

## 本地验证范围

MockTransport 覆盖：成功发送与幂等头、回执查询 200/404、403、4xx、5xx、超时、未启用、缺凭据、店铺未启用。

**真实试点渠道未调用**：需要店铺授权、渠道账号与生产/沙箱凭据，默认关闭。
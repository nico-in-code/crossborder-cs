# Amazon SP-API 订单读取合同（订单与包裹关联）

实现：`app/adapters/amazon.py`；测试：`tests/unit/test_amazon_spapi_contract.py`。

## 启用条件

真实调用必须同时满足：

1. `AMAZON_SP_API_ENABLED=true`；
2. LWA 与 AWS 凭据全部来自环境变量或受控配置（不得写入仓库、README、日志）；
3. 请求涉及的店铺 UUID 出现在 `INTEGRATION_ENABLED_STORE_IDS` 中（逗号分隔，空表示全部关闭）。

任一条件不满足时抛出 `NotConfigured`，不会发出网络请求。

## 鉴权

1. LWA 刷新令牌：`POST {AMAZON_LWA_ENDPOINT}`，表单
   `grant_type=refresh_token`、`refresh_token`、`client_id`、`client_secret`；
   返回的 `access_token` 缓存到过期前约 60 秒。
2. SP-API 请求携带：
   - `x-amz-access-token: <LWA access token>`
   - `x-amz-date: <YYYYMMDDTHHMMSSZ>`
   - `x-amz-security-token`（配置了临时凭据时）
   - `Authorization: AWS4-HMAC-SHA256 Credential=.../execute-api/aws4_request, SignedHeaders=..., Signature=...`
   - 签名服务名 `execute-api`，区域取 `AMAZON_REGION`。
3. 查询参数固定包含 `MarketplaceIds`（取 `AMAZON_MARKETPLACE_IDS`）。

## 调用的端点

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/orders/v0/orders` | 按站点与 `CreatedAfter` 搜索订单，`MaxResultsPerPage` 默认 20 |
| GET | `/orders/v0/orders/{AmazonOrderId}` | 读取单笔订单 |
| GET | `/orders/v0/orders/{AmazonOrderId}/orderItems` | 读取订单明细 |
| GET | `/orders/v0/orders/{AmazonOrderId}/shipment` | 试点包裹关联端点；真实 Amazon 常返回 404 |

## 归一化字段

| 归一化字段 | 来源 |
| --- | --- |
| `order_no` | `AmazonOrderId` |
| `buyer_external_id` | `BuyerInfo.BuyerExternalId`，否则 `BuyerInfo.BuyerEmail` |
| `status` | `OrderStatus` |
| `currency` | `OrderTotal.CurrencyCode` |
| `total_amount` | `OrderTotal.Amount` |
| `placed_at` | `PurchaseDate` |
| `marketplace_ids` | `MarketplaceId` |
| `items[]` | `SellerSKU`、`ASIN`、`Title`、`QuantityOrdered`、`ItemPrice.Amount` |
| `shipping_address` | 仅保留 `country`、`prefecture`、`city`，其余标记 `masked: "***"` |
| `shipment` | `shipmentId`、`carrier`、`trackingNumber`、`status`、`events` |

## 关联状态

`association.status` 只有两种：

- `linked` / `已关联`：订单明细非空、地址包含国家（日本站还要求都道府县）、包裹端点返回运单号。
- `pending_verification` / `关联待核实`：响应不足，`reason` 取值：
  - `order_items_missing`：明细为空或 404；
  - `order_items_forbidden`：明细 403；
  - `shipping_address_incomplete`：缺少国家或日本站缺少都道府县；
  - `shipment_not_exposed`：包裹端点 404/403 或没有运单号。

真实接入时应由 ERP/17TRACK 或运营复核把 `pending_verification` 推进为已核实，不得默认为已关联。

## 错误映射

| 上游情况 | 异常 |
| --- | --- |
| 未启用 / 缺凭据 / 店铺未启用 / 未配置站点 | `NotConfigured` |
| LWA 400/401/403 或 SP-API 401/403 | `PermissionDenied` |
| 5xx 或非预期 4xx | `UpstreamError` |
| 连接或读取超时 | `UpstreamTimeout` |

## 本地验证范围

`tests/unit/test_amazon_spapi_contract.py` 用 `httpx.MockTransport` 覆盖：LWA 成功、SigV4 头、站点参数、正常订单、明细缺失、包裹缺失、买家过滤、403、500、超时，以及未启用/缺凭据时的 `NotConfigured`。

**真实 Amazon 调用未执行**：当前没有卖家授权与真实 LWA/AWS 凭据。
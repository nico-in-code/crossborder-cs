# 物流 / ERP / 翻译适配器合同

实现：`app/adapters/tracking.py`、`app/adapters/erp.py`、`app/adapters/translate_http.py`；
测试：`tests/unit/test_tracking_erp_translate_contract.py`。

三者都按店铺启用：店铺 UUID 必须出现在 `INTEGRATION_ENABLED_STORE_IDS`，并同时满足各自开关与凭据要求，否则抛出 `NotConfigured`。

## 17TRACK 物流

配置：`TRACK17_ENABLED`、`TRACK17_API_KEY`、`TRACK17_BASE_URL`。

1. `POST {BASE}/register`，头 `17token`，体 `{"data":[{"number":"运单号","carrier":"承运商"}]}`
2. `POST {BASE}/gettrackinfo`，同样的体
3. 归一化：

```json
{
  "tracking_no": "YMT-A-0001",
  "carrier": "yamato",
  "status": "Delivered",
  "events": [{"at": "...", "status": "delivered", "description": "配達完了"}],
  "synced_at": "ISO8601"
}
```

无匹配记录时返回 `status=not_found`。错误：401/403 → `PermissionDenied`；5xx/4xx → `UpstreamError`；超时 → `UpstreamTimeout`。

## ERP 库存

配置：`ERP_ENABLED`、`ERP_BASE_URL`、`ERP_API_KEY`。

```
GET {BASE}/inventory/{sku}?store_id={store_uuid}
X-API-Key: <ERP_API_KEY>
```

成功返回 `{"sku": "...", "quantity": 42, "updated_at": "..."}`；返回体缺少 `quantity` 视为 `UpstreamError`；404 返回 `None`；错误映射同上。

## 翻译网关

配置：`TRANSLATION_ENABLED`、`TRANSLATION_BASE_URL`、`TRANSLATION_API_KEY`（可选）。

```
POST {BASE}/translate
X-API-Key: <可选>
{"text": "...", "source_language": "ja", "target_language": "zh"}
```

成功返回 `{"translated_text": "..."}`。调用必须带店铺作用域；启用列表非空而调用未提供店铺时抛 `NotConfigured`，避免跨店误用。错误映射同上。

## 本地验证范围

MockTransport 覆盖成功、401/403、5xx、超时、未启用、缺凭据、店铺未启用。

**真实 17TRACK / ERP / 翻译服务未调用**：接口合同已固定，凭据与店铺授权尚未配置。
# 附件本地对象存储与按需 OCR

实现：`app/services/attachments.py`、`app/api/attachments.py`、`app/adapters/ocr.py`；
测试：`tests/integration/test_attachment_ocr.py`。

## 存储布局

- 根目录：`ATTACHMENT_ROOT`（默认 `data/attachments`）。
- 对象键：`{tenant_id}/{store_id}/{ticket_id}/{uuid}{ext}`。
- 文件名：只保留最后一段，去掉路径分隔符、控制字符与引号；扩展名白名单
  `.jpg/.jpeg/.png/.gif/.webp/.pdf/.txt`，其它落为 `.bin`。
- 写入：同目录临时文件 + `os.replace` 原子替换；记录 SHA-256、字节数与内容类型。
- 单文件上限：`ATTACHMENT_MAX_BYTES`（默认 10 MiB）。

## 授权与越权防护

1. 上传、下载、OCR 前都调用 `get_ticket_for_employee(..., "read")`：
   跨租户/跨店返回 404，无工单角色返回 403。
2. 附件必须属于 URL 中的工单，否则 404。
3. 解析对象路径后校验必须位于存储根目录之内（`Path.is_relative_to`），任何 `..` 越界都拒绝。
4. 下载只在授权后由 `FileResponse` 返回文件，不暴露存储根目录。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tickets/{ticket_id}/attachments` | multipart 字段 `file`；需登录与 CSRF |
| GET | `/api/tickets/{ticket_id}/attachments/{attachment_id}` | 授权下载；`?download=1` 使用 attachment disposition |
| POST | `/api/tickets/{ticket_id}/attachments/{attachment_id}/ocr` | 按需 OCR，需登录与 CSRF |

OCR 结果**不落库**，每次按需读取文件后返回 `provider`、`text`、`chars`；
本地默认 `FakeOcrAdapter` 输出可复现的占位文本。HTTP OCR 网关未配置时返回 503。

## 本地验证范围

集成测试覆盖：上传 → 下载内容一致 → 按需 OCR；文件名 `../../etc/passwd.png` 被净化为 `passwd.png` 且对象键不含 `..`；跨工单与跨租户下载 404；未登录 401；超过上限 413；OCR 未配置 503。

**真实 OCR 网关未调用**：默认使用本地确定性 OCR。
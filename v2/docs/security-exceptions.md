# 依赖安全状态

最后复核：2026-07-16

当前没有启用任何漏洞忽略规则。质量门禁直接执行 `pip-audit -r apps/api/requirements.txt`，任何已知漏洞都会导致失败。

2026 年公告对应的依赖已升级并通过 Python 3.10、3.12 全量回归：

- FastAPI 0.139.0、Starlette 1.3.1。
- Uvicorn 0.51.0、Click 8.4.2、python-dotenv 1.2.2。
- python-multipart 0.0.32。

FastAPI 当前仍通过 Starlette 的旧 `httpx` 测试客户端导出 `TestClient`，测试时会产生迁移到 `httpx2` 的弃用提示。该提示不是安全例外，也不影响运行时；待 FastAPI 官方切换后应迁移测试依赖并删除此记录。

上传接口继续在应用层限制扩展名与 50 MiB 大小，反向代理也必须保留请求体限制。

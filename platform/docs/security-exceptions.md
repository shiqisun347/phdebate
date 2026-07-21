# 依赖安全状态

最后复核：2026-07-16

当前没有启用任何漏洞忽略规则。质量门禁直接执行 `pip-audit -r apps/api/requirements.txt`，任何已知漏洞都会导致失败。

2026 年公告对应的依赖已升级并通过 Python 3.10、3.12 全量回归：

- FastAPI 0.139.0、Starlette 1.3.1。
- Uvicorn 0.51.0、Click 8.4.2、python-dotenv 1.2.2。
- python-multipart 0.0.32。

FastAPI 当前仍通过 Starlette 的旧 `httpx` 测试客户端导出 `TestClient`，测试时会产生迁移到 `httpx2` 的弃用提示。该提示不是安全例外，也不影响运行时；待 FastAPI 官方切换后应迁移测试依赖并删除此记录。

上传接口继续在应用层限制扩展名与 50 MiB 大小，反向代理也必须保留请求体限制。

## 公网监听端口审计

平台仓库不直接管理宿主机防火墙或云安全组。每次生产发布前应先做只读盘点：

```bash
python deploy/audit-public-listeners.py \
  --allow-wildcard 80,443 \
  --strict --json
```

审计始终把明文 Docker Remote API `2375`、Jupyter `8888`、File Browser `8889`
以及当前用途未确认的 `9191` 视为风险项。排查期间可以使用 `--warn-only`，但不能为了
让门禁通过而长期豁免高风险端口。确认服务归属与回滚方法后，应在宿主端口映射、云安全组
或服务监听地址中限制访问。

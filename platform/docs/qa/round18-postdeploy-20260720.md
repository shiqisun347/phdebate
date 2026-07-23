# Round 18 生产发布与恢复闭环

日期：2026-07-20

## 发布

- Release：`round18-product-recovery-20260720`
- 应用提交：`95242c81f25106e1a88d3d75e2c0925d9405c2ec`
- 源码 tree：`829d3ea20ecdde818f7e87eb7b469f9710dd5fe5`
- API 双 Worker 滚动发布成功，Web 原子切换成功，上一版保留用于回滚。

Readiness 在发布后保持通过：PostgreSQL、schema、Redis、Engine、Worker、MOSS、FunASR、备份和
release provenance 全部正常；活动比赛为 0。

## 生产验收

- 浏览器双用户完整 1v1：建房、认领、准备、开赛、真人文字发言、自由辩论、暂停恢复、断线 AI 接替、
  房主移交、真人恢复、终止和结果归档均通过。
- 观战门禁：20 人连接成功，第 21 人返回 `4429`，释放一个名额后新观众进入成功。
- 发布后的 WebSocket fanout：20 connected / 20 caught up / 0 errors，慢客户端与 32 个事件突发通过。
- QA 房间 `220192` 已终止并标记测试数据；两个 QA 账号均标记为测试账号、停用且会话数为 0。

## 全量门禁

- API：`443 passed, 1 xfailed`
- Web：41 files，`284 passed`
- deploy + scripts：`102 passed`
- Ruff、TypeScript、Next.js build：通过
- ESLint：0 error；13 个既有 warning 位于冻结舞台/音频或 PostCSS

## 备份

- 平台数据库：`auto-20260720T161223Z.dump`
- SHA-256：`44a0dcef2abf6b57ce9be78e89c049533c5445ed729733fae6e7844477693788`
- 实际恢复验证：25 张表，Alembic `0027_speech_data_disposition`
- 恢复清单：`recovery-set-20260720T1613Z-round18.manifest`
- 六项恢复文件逐项校验；可靠语音基线仍为 82 个文件，指纹未变。

服务器只保留一个平台根目录；`/v2` 返回 404。安全清理旧源码归档和旧 release 后，磁盘可用空间由约
16.7% 提升至约 18%，没有删除数据库、学生音频、MOSS、私密配置或恢复清单引用的文件。

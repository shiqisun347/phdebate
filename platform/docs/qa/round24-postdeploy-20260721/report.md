# Round 24 生产发布与恢复集验收

日期：2026-07-21  
生产站：`https://117.50.192.216`  
代码分支：`backup/production-20260720-round12`

## 发布结果

- API：`round24-final-20260721`
- Web：`round24-room-search-hotfix-20260721`
- 已部署平台源码：`56bf9abf4992d96dcecd863a0b60c43aef216eb0`
- 完整备份分支头：`11e1750370cf744826ce4d4e0df6f0bbfb70a1a3`
- Transcript collaboration：`round24-recovery-node22-20260721`
- MOSS、Engine 和 Worker 未因 Web/API 滚动发布而重启。

## 修复与验证

### 比赛开始与异常恢复

- 生产使用 MOSS 时，0 个暖机 endpoint 会让开赛返回 503 和 `Retry-After: 5`。
- 开赛失败不会锁定席位、自动填充 AI 或创建 Match。
- 服务异常重试使用同一门禁；未暖机时保持 paused、failure reason 和 failed speech 不变。
- 重复开始仍为幂等操作；双设备/四房间并发回归通过。

### 观战文字隐私

匿名及登录非参赛观众的生产响应均满足：

```text
caption_segments=0
active_speech.content=""
speeches[*].content 非空数=0
recent_events payload content/text 字段数=0
can_view_transcript=false
```

观战页面已移除逐句字幕投影和文字记录抽屉；参赛页面继续保留参赛者所需功能。

### 浏览器回归

- 不存在房间显示：`没有找到房间 #123456。请核对房间号，或向房主确认比赛是否已关闭。`
- 不会为不存在房间错误生成“返回当前比赛”链接。
- 390×844 观战顶部完整辩题按两行显示：计算样式 `white-space: normal`、`line-clamp: 2`、高度 30px、行高 15px。
- 页面横向溢出为 0，浏览器 console 与 runtime errors 为空。

证据：

- [不存在房间最终提示](screenshots/nonexistent-room-final.png)
- [手机观战两行辩题](screenshots/mobile-watch-topic-two-lines.png)

### Transcript collaboration 恢复

- 清理审计发现 current symlink 和 release 目录丢失，旧进程仅靠已打开文件继续运行，重启必然失败。
- 修复构建脚本，使 npm lifecycle 全程使用托管 Node 22，并在构建失败时删除不完整 release。
- 修复源码同步脚本，`--delete` 不再删除 `.transcript-collab-current` 与 `.transcript-collab-releases/`。
- 重建后 14 项服务测试通过；Supervisor 重启成功，内部 `/health` 与 `/ready` 均为 200。

## 自动化回归

```text
API:    478 passed, 1 xfailed
Web:    327 passed; Next.js production build passed
Deploy: 124 passed
```

可靠音频冻结基线未变化：

```text
audio_baseline_verified files=82 fingerprint=3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

## 完整恢复集

恢复入口：

```text
runtime/deploy-backups/recovery-set-20260721T-round24-final-current.manifest
runtime/deploy-backups/recovery-set-20260721T-round24-final-current.manifest.sha256
```

包含并验证：

- 当前平台数据库 `auto-20260721T110122Z.dump`，已真实恢复到临时数据库并核对 30 张表与 Alembic `0031_caption_segments`。
- 当前 Debate Agent 数据库 `agent-20260721T111727Z.dump`，checksum 与 pg_restore catalog 通过。
- 当前数据卷 `20260721T-round24-data-volumes.tar.gz`，468 个文件，完整解包/哈希验证通过。
- 私密配置、可靠语音运行时和 11.8GB OpenMOSS 离线包沿用未变化的已验证不可变副本。
- 六类 artifact 全量 SHA-256 复核通过，绑定完整代码提交 `11e1750...` 与已部署平台提交 `56bf9ab...`。
- Debate Agent 备份脚本固定把健康状态文件发布为 `root:ubuntu 0640`；再次实际备份后平台聚合健康检查保持全绿。

最终磁盘使用 60%，约 31GB 可用；没有删除当前 MOSS、FunASR、比赛音频或完整恢复材料。

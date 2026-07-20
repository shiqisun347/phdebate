# 2026-07-20 生产服务器握手异常

## 影响

- 生产入口：`https://117.50.192.216`
- 约 01:43（Asia/Shanghai）起，浏览器出现 `ERR_CONNECTION_CLOSED`，命令行 HTTPS 出现
  `SSL_ERROR_SYSCALL` 或握手超时。
- SSH 23 端口仍可能完成 TCP connect，但无法收到 SSH banner，连接随后超时或被远端关闭。
- Round 8 本地修复尚未部署，因此此次中断不是新 API/Web release 切换造成的。

## 中断前最后确认状态

- Round 7 的 API、Web、Engine、Worker、PostgreSQL、Redis、FunASR、MOSS-Realtime readiness
  均为健康状态。
- 数据库 schema 为 `0025_speech_result_pagination`，没有活动比赛处理。
- 本轮服务器操作只有：
  - 执行 `prune-releases.sh dry-run`，仅打印候选，未删除任何 release；
  - 在 `/tmp` 隔离解包并逐文件校验服务器数据卷备份，380 个文件全部通过，临时目录由 trap 清理；
  - 未重启 Supervisor、Nginx、数据库、Redis、MOSS 或主机。

## 已采集现象

- ICMP 无响应（服务器此前也不保证开放 ICMP，因此不能单独据此判定宕机）。
- TCP 23/443 间歇可建立，但应用协议在 banner/TLS 握手前终止。
- 多次 SSH 均停在 `kex_exchange_identification` 之前，无法进入主机采集进程、内存、磁盘或日志。
- agent-browser 已停止重试，避免在异常期间增加连接压力。

## 恢复后必须检查

1. 从云控制台确认实例运行、电源、带宽、防火墙和安全组状态；若仍无法 SSH，使用云厂商
   VNC/串行控制台，而不是反复重试密码登录。
2. 记录 `uptime`、`last -x`、`journalctl --since`、内核 OOM、磁盘和 inode，判断是否发生重启、
   OOM、连接耗尽或云网络异常。
3. 检查 sshd、Nginx、Supervisor、系统连接数和文件描述符；不得先无证据重启全部服务。
4. 依次验证本机 12340/12342/12341/8890、公开 HTTPS、WebSocket 与 LiveKit。
5. readiness 全绿且无活动比赛后再部署 Round 8；部署后执行生产浏览器回归。

## 数据安全

中断前已存在服务器侧 Round 7 恢复集和 GitHub orphan 快照。Mac 未保存生产数据库、数据卷、
模型或私密配置。本事件期间没有执行数据库恢复、release 删除或配置覆盖。

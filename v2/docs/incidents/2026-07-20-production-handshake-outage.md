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

## 恢复结果

- SSH 恢复后确认主机 `uptime` 仅约 10 分钟，说明握手异常期间发生了整机重启；持久化
  journal 未启用，因此暂时无法从本机证明重启是云维护、人工操作还是其他外部原因。
- 重启暴露了两个此前被运行中进程掩盖的恢复缺口：
  1. Supervisor 的 HTTPS 进程仍引用已被早期源码同步误删、且未纳入 Git 的
     `deploy/nginx-new-server.conf`；
  2. MOSS 配置为 `autostart=false`，并固定云主机重启前的 GPU UUID。重启后 RTX 3090 的
     UUID 发生变化，旧 preflight 因此按设计拒绝启动。
- HTTPS Supervisor 已改为引用受版本控制并通过 `nginx -t` 的
  `deploy/jixia-nginx-v2-root.conf`，公网 80/443 和 `/api/health/live` 已恢复。
- MOSS 的模型、参数、声音和播放实现均未修改。新增受保护目录之外的运维启动器，在每次启动
  时只接受唯一一张名称、显存和 VBIOS 均匹配的 RTX 3090，再为当前易变 UUID 生成 preflight
  指纹；Supervisor 改为开机自动启动。可靠语音 82 文件基线保持不变。
- MOSS 完成预热后 authenticated readiness 返回成功；API、Web、Engine 和 Worker 随后完成
  Round 8 发布，生产总 readiness 全绿。
- 新的 Nginx 与 MOSS Supervisor 配置已纳入 GitHub Round 8 快照，源码同步脚本同时改为保护
  `.env`、运行数据、全部虚拟环境和服务链接，并避免覆盖服务器文件所有权。
- 没有为验证自动启动而再次主动重启生产主机；开机恢复的证据目前由 Supervisor 配置、安装
  状态、静态测试和本次意外重启后的实际恢复共同构成。

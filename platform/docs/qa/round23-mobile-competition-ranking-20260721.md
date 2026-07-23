# Round 23 移动端赛事排行榜

日期：2026-07-21  
发布：`round23-mobile-ranking-20260721`  
代码：`ba4568b83b2d840b70548045f541c4f1d4bf0b0c`

## 问题

赛事详情页的排行榜沿用普通桌面表格。390px 手机上六列表头被压成逐字竖排，选手姓名和战绩也缺少
移动端信息层级。数据仍可读取，但不符合学生频繁使用手机参赛、查看排名的真实场景。

## 修复

- 桌面端继续使用带 caption、thead 和标准单元格的语义表格。
- 手机端复用全局排行榜已经验证的响应式卡片模式，不维护第二份数据结构。
- 每行依次展示名次、辩手、赛季积分、战绩、平均评分和有效场次。
- 每个 `td` 增加 `data-label`；CSS 视觉标签与屏幕阅读器的原始表格语义同时保留。
- 空榜状态增加原因和下一步说明，不只显示一句占位文字。

## 验证

- 定向测试：`12 passed`。
- Web 全量：`50 files, 325 passed`。
- TypeScript、Next.js build 通过。
- ESLint 0 error；13 个 warning 均为既有冻结音频路径或 PostCSS 配置。
- axe：新增赛事详情排名表无可检测无障碍违规。
- 生产 390×844：CLS `0`，网络请求无 `>=400`，console 无错误。
- 生产可访问树中每个单元格均包含明确标签，例如“战绩 1 胜 · 0 平 · 1 负”。
- 可靠音频：82 个文件，fingerprint 保持
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

视觉证据保存在本地 QA 目录：

```text
docs/qa/round22-public-postdeploy-evidence/competition-ranking-card-round23.png
docs/qa/round22-public-postdeploy-evidence/competition-ranking-card-round23-full.png
```

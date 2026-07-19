# API 完整回归

日期：2026-07-18

## 最终权威完整运行

```text
cd /Users/sunshiqi/code/phdebate/v2
/usr/bin/time -p env PYTHONPATH=apps/api .venv/bin/python -m pytest -q apps/api/tests
```

- 执行方式：单个 pytest 进程，未启用 xdist 或其他并行插件。
- 结果：`353 passed, 22 warnings`。
- pytest 报告耗时：`56.79s`。
- 墙钟耗时：`58.72s`。
- 结论：**PASS**。

本次最终运行包含 `agent_first_readable_delta_at` 统一诊断时间戳改动，以及下面所述短语边界测试修正。

## 首轮失败与修正闭环

### 1. 打断测试语料与正式 10 字首块门槛不一致

```text
apps/api/tests/test_platform.py::test_realtime_voice_pause_interrupts_agent_clears_tts_and_keeps_unplayed_text_out_of_history
```

- 首轮症状：测试只产生 9 个字符的弱标点短语“已经进入语音队列，”，而正式配置规定首块为 10–16 个中文字符；assembler 正确等待更多正文，测试却错误期待立即提交。
- 修正：把测试语料改成 10 字以上的稳定短语“这段文字已经进入语音队列，”，没有降低产品最小块长，也没有修改生产逻辑。
- 定向复验：完整打断测试与后续 engine recovery 测试 `2 passed`。
- 分类：测试夹具与新正式参数不一致，已修正并由全量回归关闭。

### 2. 首轮级联污染，不是独立失败

```text
apps/api/tests/test_platform.py::test_engine_restart_interrupts_only_engine_owned_inflight_tasks
```

- 完整套件症状：期望恢复 1 个 speech，实际恢复 2 个。
- 原因判断：前一实时语音测试在等待事件超时后未走到测试自身的任务清理，遗留额外 inflight speech，随后被恢复测试计数。
- 独立串行复验：`1 passed`，pytest `0.71s`，墙钟 `2.71s`。
- 分类：第一项失败导致的级联状态污染；不是独立的 engine restart 回归。

修正首项测试后，最终完整套件中该项和全部其他测试均通过。

## 启动命令兼容性记录

在权威运行前，两种 pytest 控制台/执行目录调用均停在收集阶段：

1. 从 `apps/api` 运行 `../../.venv/bin/python -m pytest -q`：`scripts` 不在模块路径，收集错误；未执行测试。
2. 从仓库根运行现有 `quality.sh` 等价的 `PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests`：pytest 控制台入口仍无法导入根目录 `scripts`，收集错误；未执行测试。

最终使用同一项目 `.venv`、现有 pytest 配置和 `PYTHONPATH=apps/api`，通过 `python -m pytest` 保留仓库根目录模块路径后完成全部测试。上述两个收集错误属于测试启动路径兼容性问题，与 `voice_runtime` 产品逻辑无关。

本轮只修改了本地诊断时间戳与不再符合正式块长的测试语料；未修改数据库迁移、部署配置或生产环境。

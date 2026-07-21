# Round 22：schema 1 恢复清单非破坏性升级

日期：2026-07-21  
范围：恢复清单解析、恢复索引校验和清理门禁。  
约束：不删除、移动或覆盖任何现有恢复文件；不部署；不修改冻结音频、TTS、LiveKit 或浏览器播放路径。

## 结论

生产上阻塞恢复集清理的 7 份 schema 1 清单并非空清单。其真实格式为：版本和 release 元数据之后，
依次保存六行 `<sha256>  <absolute-path>`，覆盖 平台 数据库、Debate Agent 数据库、数据卷、私密配置、
可靠语音运行时和 OpenMOSS 离线包。旧解析器只识别 `artifact.<role>.*`，所以正确地失败关闭，但无法
证明这些旧清单对应的归档是否仍完整。

本轮新增只生成伴随清单的升级路径。原 schema 1 文件保持逐字节不变；清理器只有在原文件绑定、六件
归档结构和完整 recovery index 三层校验全部通过时，才把它识别为受保护的历史恢复集。它不会把旧清单
或其数据卷加入自动删除候选。

## 实现

### `deploy/upgrade-schema1-recovery-manifests.py`

- 默认 `audit`，不会创建目录或文件。
- `write` 必须设置 `PHDEBATE_ALLOW_SCHEMA1_MANIFEST_UPGRADE=yes`。
- 仅扫描备份目录顶层的 `recovery-set-*.manifest`，只处理 `schema_version=1`。
- 严格要求六条合法 checksum 记录，按文件类型和历史目录语义映射六个唯一角色；重复、缺少或无法分类
  均拒绝整批升级。
- 不使用旧绝对路径访问文件，只取安全 basename，并在显式批准的 artifact roots 中要求恰好一个真实
  普通文件；符号链接、重复 basename、缺失文件和 checksum 不一致全部拒绝。
- 输出到独立 `manifest-upgrades/`，使用 `O_EXCL`、`fsync` 和 `0600`，绝不覆盖同名文件。
- 伴随清单为 schema 3，记录实际 filename、bytes、SHA-256，并绑定原清单 filename、bytes、SHA-256。
- 多份清单先全部完成解析和归档校验，任何一份失败时不写任何伴随清单。
- 可选 JSON 报告同样排他创建、权限 `0600`；目标已存在时在写伴随清单前拒绝。
- 重复执行时，只有现有伴随清单与重新核验得到的确定性内容完全一致才报告 `verified-existing`。

### `deploy/prune-recovery-sets.sh`

- schema 1 原清单仍在顶层；缺少伴随清单、伴随清单被修改、原清单被修改或绑定不一致时继续
  `unsafe-manifests-present`，不删除任何恢复集。
- 合法伴随清单加入与 schema 2/3 相同的 recovery index 校验；六件归档的唯一性、大小和 SHA-256
  任一失败都会停止全部清理。
- 校验通过后输出 `kind=legacy_manifest reason=verified-schema1-upgrade`，永久保护对应 data volume。
- schema 1 历史恢复集不计入最近恢复集的 rollback floor，避免用旧、不完整元数据替代当前 schema 3
  恢复保障。

## 测试覆盖

`deploy/tests/test_prune_recovery_sets.py` 新增：

1. audit 不创建目录、清单或报告，且原文件 inode、mtime、大小和内容不变；
2. write 只创建 `0600` 伴随清单和报告，所有原清单、数据库及归档哈希不变；
3. 重复 write 验证现有伴随清单，不覆盖；
4. 缺少显式确认时拒绝 write；
5. 任意归档缺失时整批失败且零输出；
6. 已存在报告时在写入前拒绝，保留操作者文件；
7. 验证过的 schema 1 不再阻塞其他旧 schema 3 候选，但自身和全部旧归档继续保留；
8. 原 schema 1 清单生成伴随后发生任何变化，清理器立即恢复失败关闭。

本地针对性结果：`20 passed`；Python compile 和 `bash -n` 通过。测试仅使用临时目录，不接触生产恢复文件。

## 生产只读审计

只读检查确认需处理的 7 份清单：

- `recovery-set-20260719T1015Z.manifest`
- `recovery-set-20260719T1057Z-round6.manifest`
- `recovery-set-20260719T1357Z-round7.manifest`
- `recovery-set-20260720T072642Z.manifest`
- `recovery-set-20260720T084642Z.manifest`
- `recovery-set-20260720T0932Z-round10.manifest`
- `recovery-set-20260720T1013Z-round11.manifest`

它们均仍为原 schema 1 格式。本轮没有把新工具复制到服务器，没有运行 write、quarantine 或 prune，
也没有创建、移动、改写或删除任何生产文件。

随后使用一次性只读检查按安全 basename 在三个批准目录中定位归档，并对共享文件做缓存哈希。结果为
`7/7` 清单通过，每份均有六类归档且 checksum 全部一致；跨清单合计 `22` 个唯一实际文件。该检查只
读取清单、文件元数据和文件内容，没有在服务器创建报告或伴随清单。正式执行仍必须使用本轮新增工具
重新 audit，不能把一次性检查输出替代版本化伴随清单。

## 操作顺序与边界

未来生产执行必须按以下顺序：

1. 先备份并验证最新 schema 3 恢复集；
2. 运行 schema 1 `audit`，保存终端输出供人工核对；
3. 显式确认后运行 `write --report <全新文件名>`；
4. 运行完整 recovery index 和 `prune-recovery-sets.sh dry-run`；
5. 人工确认 7 个旧 data volume 均显示为 `protects=`，且候选只包含预期的 schema 2/3 旧批次；
6. 另行获得删除授权后，才可执行 prune apply。

该升级只解除“不认识旧格式”的全局阻塞，不释放 7 份旧数据卷空间。若要淘汰它们，必须先把对应恢复
材料迁出生产盘、在隔离环境完成恢复验证，并建立不可篡改的退役记录；本工具不会自动执行这一步。

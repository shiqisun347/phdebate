# MOSS-Realtime 固定音色资产

该目录保存新服务器 MOSS-Realtime 部署所需的 8 个固定普通话音色。

```text
audio/                24 kHz、单声道、PCM16 WAV
transcripts/          每个音色的普通文本与历史 prompt 文本
source-manifest.json  来源、处理参数、逐字稿和 SHA-256
```

来源为 AISHELL-3，数据集版本、来源 URL、说话人、原始片段、许可证和处理过程记录在 `source-manifest.json`。AISHELL-3 以 Apache License 2.0 发布；部署或再分发时仍应保留来源说明，并按项目隐私政策管理音色使用。

部署清单位于 `deploy/openmoss/deployment-manifest.json`。两份清单中的 8 个 WAV SHA-256 必须完全一致。任何音频或逐字稿修改都必须：

1. 生成新的来源与处理清单。
2. 更新部署清单哈希。
3. 重新运行静态门、8 音色 TTS→ASR、吞字/重复、漂移和人工听感验收。
4. 在验收通过前保持生产 MOSS readiness 关闭。

这些文件目前是候选正式音色，不代表已经通过最终 CER、MOS 和长期音色稳定性门槛。

# LightTTS 拼接边界审计

- 样本数：8
- 数字零平台范围：120.0–131.8 ms
- 拼接低能量间隔中位数/最大值：255.0 / 750.0 ms
- 相邻分段整体 RMS 差最大值：2.2 dB
- 插零前最后样本高于 -40dBFS 的硬阶跃样本：2

| 文件 | 零平台 ms | 低能量间隔 ms | 前 10ms RMS | 后 10ms RMS | 相邻分段 RMS 差 dB |
|---|---:|---:|---:|---:|---:|
| AUDIO-CN_STANDARD-debate_voice_1.wav | 123.1 | 280 | -108.09 | -50.95 | 1.37 |
| AUDIO-CN_STANDARD-debate_voice_2.wav | 131.8 | 750 | -102.65 | -105.66 | 2.2 |
| AUDIO-CN_STANDARD-debate_voice_3.wav | 126.9 | 250 | -105.66 | -50.98 | 1.09 |
| AUDIO-CN_STANDARD-debate_voice_4.wav | 122.1 | 230 | -108.09 | -47.74 | 1.19 |
| AUDIO-NAMES_PUNCTUATION-debate_voice_1.wav | 121.3 | 260 | -106.33 | -50.29 | 0.23 |
| AUDIO-NAMES_PUNCTUATION-debate_voice_2.wav | 124.8 | 550 | -8.2 | -108.09 | 0.62 |
| AUDIO-NAMES_PUNCTUATION-debate_voice_3.wav | 120.0 | 120 | -14.37 | -48.96 | 0.94 |
| AUDIO-NAMES_PUNCTUATION-debate_voice_4.wav | 121.9 | 240 | -102.97 | -47.79 | 0.08 |

说明：零平台是合并器插入的数字零标记；低能量间隔按 10ms 窗口、RMS≤-60dBFS 且 peak≤-50dBFS 计算。该指标只描述拼接边界，不把句内自然停顿混入统计。

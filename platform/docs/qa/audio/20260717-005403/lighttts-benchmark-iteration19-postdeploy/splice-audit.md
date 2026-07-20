# LightTTS 拼接边界审计

- 样本数：8
- 数字零平台范围：85.0–119.0 ms
- 拼接低能量间隔中位数/最大值：130.0 / 130.0 ms
- 相邻分段整体 RMS 差最大值：8.28 dB
- 插零前最后样本高于 -40dBFS 的硬阶跃样本：0

| 文件 | 零平台 ms | 低能量间隔 ms | 前 10ms RMS | 后 10ms RMS | 相邻分段 RMS 差 dB |
|---|---:|---:|---:|---:|---:|
| AUDIO-CN_STANDARD-debate_voice_1.wav | 117.0 | 130 | -55.06 | -50.73 | 1.66 |
| AUDIO-CN_STANDARD-debate_voice_2.wav | 85.0 | 120 | -94.72 | -82.94 | 0.4 |
| AUDIO-CN_STANDARD-debate_voice_3.wav | 104.0 | 130 | -101.1 | -52.07 | 1.33 |
| AUDIO-CN_STANDARD-debate_voice_4.wav | 103.3 | 130 | -95.03 | -50.07 | 0.53 |
| AUDIO-NAMES_PUNCTUATION-debate_voice_1.wav | 119.0 | 130 | -50.14 | -56.1 | 0.09 |
| AUDIO-NAMES_PUNCTUATION-debate_voice_2.wav | 102.1 | 130 | -14.17 | -84.07 | 8.28 |
| AUDIO-NAMES_PUNCTUATION-debate_voice_3.wav | 103.0 | 130 | -104.57 | -55.52 | 0.62 |
| AUDIO-NAMES_PUNCTUATION-debate_voice_4.wav | 101.5 | 130 | -69.09 | -49.55 | 0.65 |

说明：零平台是合并器插入的数字零标记；低能量间隔按 10ms 窗口、RMS≤-60dBFS 且 peak≤-50dBFS 计算。该指标只描述拼接边界，不把句内自然停顿混入统计。

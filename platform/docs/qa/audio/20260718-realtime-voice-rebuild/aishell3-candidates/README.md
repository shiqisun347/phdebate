# AISHELL-3 neutral Mandarin CosyVoice prompt candidates

- Generated: 2026-07-17T20:13:40.480241+00:00
- Dataset commit: `f20d5db4a31fe779ef07bb1af4ea92da5c786622`
- License: Apache License 2.0 (https://www.openslr.org/93/)
- Source: official AISHELL Hugging Face dataset repository; eight distinct real speaker IDs.
- Processing: edge-silence trim, resample/downmix/PCM16 conversion, same-speaker concatenation and peak normalization only.
- Candidate transcripts exclude first/second-person dialogue and a conservative list of emotional/violent/colloquial terms.
- No synthesis, pitch shifting, voice conversion or cross-speaker mixing was used.
- Static gate: **PASS**

| Candidate | Speaker | Sex | Source clips | Duration s | Peak/RMS dBFS | Max silence ms | Converted SHA-256 | Transcript confidence | Gate |
|---|---|---|---:|---:|---:|---:|---|---|---|
| candidate_voice_1 | SSB0273 | male | 4 | 11.306875 | -3.5/-21.549 | 210 | `67db118696aa8f0c637344871d1343374f262db87cf9451a9939773f688b053a` | high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim) | PASS |
| candidate_voice_2 | SSB0241 | male | 3 | 10.56925 | -3.5/-23.452 | 480 | `2e05e8ab9919e5fd9d6d765b62ada6891afffdfb82b5173789a19ee4e9d5e67f` | high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim) | PASS |
| candidate_voice_3 | SSB0073 | male | 3 | 10.470167 | -3.5/-27.484 | 520 | `7531485676b9fe9ed4af46c523f16e0f7faf0173dc8bcf417747a4f17c7ac883` | high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim) | PASS |
| candidate_voice_4 | SSB0629 | male | 3 | 11.106583 | -3.5/-20.286 | 340 | `1d32c88e4bd1427e3834180b329e9fb887a915802f62d68c014b087f21977a1b` | high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim) | PASS |
| candidate_voice_5 | SSB0016 | female | 4 | 10.446375 | -3.5/-18.725 | 110 | `41105727db43a32b8289783f00915a0f1c199fb406b1ff90542404f446cc4e1a` | high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim) | PASS |
| candidate_voice_6 | SSB0534 | female | 4 | 10.542833 | -3.5/-17.85 | 110 | `74335f280e30f127ab4bb7e7df8fa4852bf1cd5a45354ef07b4f929244b2d7ea` | high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim) | PASS |
| candidate_voice_7 | SSB0380 | female | 4 | 10.564458 | -3.5/-22.688 | 120 | `fee596cfbc031eb52abec47fa128d0f71298cb226dc9d0a87bf29c4394141dac` | high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim) | PASS |
| candidate_voice_8 | SSB0200 | female | 4 | 10.480708 | -3.5/-24.591 | 550 | `aec2efcfb1aec4acfd5a5a62baa68fb8ebdb387dc1d2bce2d348182d09cde523` | high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim) | PASS |

## Remaining release blockers

- Static compliance does not prove naturalness or suitability for debate. Human listening must reject dialect, role-play tone, noise and unstable delivery.
- Each exact transcript must be listened against the converted composite before it can become a production prompt.
- Run CosyVoice3 TTS→ASR CER, 20-round voice-drift, 8-way blind distinction and MOS gates before assigning debate_voice_1..8.
- Apache-2.0 attribution and the AISHELL-3 notice/source link must be preserved in any redistributed bundle.

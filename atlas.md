# Music Atlas 项目总结

> 更新日期：2026-09-04 ｜ 仓库：https://gitee.com/Hawkiethehawk/music-atlas ｜ 当前 67 个测试全绿

## 一、项目是什么

Music Atlas 是一个**按需触发、不绑定时间**的歌单推荐工作流：对本次输入的**整个歌单做全量解析**，每次固定输出 **10 首**带公开证据的推荐歌曲，与歌单规模无关。

```
Step 1 歌单快照 ──▶ Step 2 确定性分析 ──▶ Step 3 Agent 推荐 ──▶ 渠道文本
PlaylistSnapshot      MusicianAnalysisPacket      RecommendationBundle      (微信/飞书/TG)
        └────────── 各步骤之间只用 JSON 契约连接，互不回头读取 ──────────┘
```

## 二、核心设计边界（代码强制，非文档自觉）

1. **输入隔离**：Step 2 只读本次 `PlaylistSnapshot`，Step 3 只读本次 `MusicianAnalysisPacket`；平台登录态、个性化推荐、历史运行结果不进入管线。
2. **评分独占**：七维评分（风格/听感轴/关系/频率/新鲜度/证据质量/公开关联）、召回配额、去重、MMR 多样性、能量弧排序全部由 `recommender.py` 确定性计算；Agent 只提交带证据的候选，禁止自评分（契约层拦截）。
3. **证据契约**：每首候选必须携带逐条证据（claim_type + URL），来源黑名单屏蔽个性化页面，证据 URL ⊆ sources，A/B/C 等级按 claim_type × source_class 审计。
4. **反馈只读**：用户反馈仅进入离线评估（`evaluate`）与人工批准的调优建议（`tune`，`approval_required: true`），绝不自动改变排序策略。
5. **数量契约**：`declared_track_count == track_count == len(tracks)` 三方核对，数量永远来自本次 Step 1。

## 三、歌单来源（Step 1 能力矩阵）

| 来源 | Reader | 登录要求 | 状态 |
|------|--------|---------|------|
| 本地导出 JSON | `local_json` / `apple_music_json` / `netease_json` | 无 | ✅ |
| 本地 CSV | `csv` | 无 | ✅（已适配 TuneMyMusic 表头归一化） |
| 网易云公开歌单 | `netease_public` | 完全匿名 | ✅ 真实链路验证（热歌榜 200 首） |
| QQ 音乐公开歌单 | `qq_public` | 完全匿名 | ✅ 真实链路验证（`hasmore` 分页、`mid` 稳定 ID） |
| Apple Music 个人歌单 | TuneMyMusic 中转 → `csv` | **免登录** | ✅ 全自动导出 + 115 首真实验证 |
| Spotify | — | 需 Premium（2025 政策硬绑定） | ⏸️ 调研归档 |
| Apple Music 直连 | MusicKit | 需 Developer Program（$99/年） | ⏸️ 调研归档 |

### Apple Music 导出链路（本次固化）

```
workflow.py export-apple-playlist --url <分享链接>
    └─▶ tools/export_apple_playlist.mjs（Playwright 无头）
        TuneMyMusic URL 加载 → Export to file → CSV → 下载校验（表头+行数）
    └─▶ input/apple_favorite_songs.csv
然后：workflow.py snapshot --reader csv ...
```

- 全程无需 TuneMyMusic 注册/登录，也不需要 Apple ID 授权——公开分享链接即可
- 已知限制：直接抓取 Apple 网页只能拿到约 40 首预渲染曲目（SPA 懒加载），因此必须经 TuneMyMusic 解析；TuneMyMusic 页面改版时脚本会显式报错而非产出坏数据
- 依赖：Node.js + `npm install playwright`（仅此工具需要；主工作流保持纯 Python 零依赖）

## 四、代码结构（约 6,000 行 Python）

| 模块 | 职责 |
|------|------|
| `contracts.py` | 全部 JSON 契约与校验（配额算法也在此，保证校验器与排序器共用同一实现） |
| `source_adapters.py` | 各平台歌单 Reader（本地 JSON/CSV、网易云公开、QQ 公开） |
| `musician_analyzer.py` | Step 2 确定性分析：艺人分布、主唱/前乐队/side project、逐曲风格画像与八轴听感 |
| `recommender.py` | 七维评分 + 硬配额 + MMR + 能量弧排序 |
| `agent_prompt.py` / `agent_runner.py` | 隔离 Agent 上下文（插槽化提示词、预算截断）与外部 Agent 执行 |
| `evidence.py` | 来源分类、稳定标识符验证（MusicBrainz UUID/Wikidata QID 等）、A/B/C 等级规则 |
| `evaluation.py` / `feedback.py` / `tune.py` | 反馈契约、六类离线指标、人工批准调优建议 |
| `workflow.py` | CLI 入口：`snapshot / analyze / prepare-agent / agent / validate / evaluate / tune / archive-schema1 / export-apple-playlist` |
| `tools/` | 维护性辅助工具（Apple 歌单导出），不属于纯 Python 工作流本体 |

## 五、质量与验证状态

- **测试**：67 个单元测试（契约、评分、证据、反馈、评估、稳健性、各 Reader），`compileall` 无错
- **真实验证记录**：网易云热歌榜 200 首、QQ 30 首、Apple Music 115 首三条链路均跑通 snapshot → analyze → prepare-agent
- **生产就绪前置条件**（唯一未完成项）：一次使用外部可验证证据的**实时代理试运行**——本地夹具链路已全部验证，但尚未接入真实 Agent 执行与消息发送

## 六、运维要点

- `input/`、`runtime/`、`secrets/`、`styles/artist_style_profiles.json` 均为本地个人数据，Git 忽略
- `analysis_id` 为内容哈希，同一输入可复现；Schema 1 历史产物只能归档不可复用
- 版本流程：无版本对象仓库，推送必须带 CHANGELOG 更新 + `patch-YYYYMMDD-HHMMSS` 维护标签
- 文档：`README.md`（用法）、`AGENTS.md`（仓库规则）、`CHANGELOG.md`（变更史）、`P0_P1_remaining.md`（遗留项）

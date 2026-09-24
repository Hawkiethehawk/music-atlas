# Music Atlas 项目总结

> 本地机制说明更新：2026-09-24 ｜ 仓库：https://github.com/Hawkiethehawk/music-atlas ｜ 本地隔离副本的修改尚未发布；测试结果以本次运行输出为准

## 一、项目是什么

Music Atlas 按需触发、不绑定时间。网页读取原始歌单后允许选择本次处理比例，并同时保留原始规模和处理数。默认推荐目标 10 首，候选不足时按可用数量收缩。来源记录不等于独立事实核验，结果保留资料层级和缺口。

```
Step 1 歌单快照 ──▶ Step 2 公开标签采集 + 分层聚合 ──▶ Step 3 候选 Skill + 程序选曲 ──▶ 网页视图
PlaylistSnapshot          MusicianAnalysisPacket                 RecommendationBundle
        └────────── 各步骤之间只用 JSON 契约连接，互不回头读取 ──────────┘
```

## 二、核心设计边界（代码强制，非文档自觉）

1. **输入隔离**：Step 2 仅以本次 `PlaylistSnapshot` 为偏好输入，分析 Skill 可研究公开资料；Step 3 只读本次 `MusicianAnalysisPacket`。默认 Skill 模式不加载本地画像、关系或偏好名单，平台登录态、个性化推荐和历史运行结果不进入管线。外部 Skill 执行器的文件/工具访问权限由执行环境约束，本地协议执行器不是沙箱。
2. **评分独占**：来源模型按风格、关系、频率、新鲜度、证据质量和公开关联六项维度计算评分；召回复核、去重、MMR 多样性与顺序均由程序确定。Skill 只提交候选事实，不提交八轴或分数。显式历史 catalog 包沿用旧字段校验；已有排序包仍要经过事实一致性检查与确定性重算。
3. **证据契约**：每首候选必须携带逐条证据（claim_type + URL），来源黑名单屏蔽个性化页面，证据 URL ⊆ sources；来源等级与公开关联分数按真实 URL 和事实类型推导，不信任 Skill 自报等级。不可用证据在排序前阻断，输出状态固定为程序拥有的 `publication_status: "draft"`。
4. **反馈只读**：评估与调优统一匹配当前分析并按真实时间取最新反馈，未反馈歌曲不作为拒绝；无有效对照时不建议调整。策略仅经 `analyze/run --policy-file` 显式加载，摘要纳入分析身份与 manifest，绝不自动应用建议。
5. **数量契约**：快照中 `declared_track_count == track_count == len(tracks)`。网页分位处理后，`source_playlist_track_count` 保存原歌单规模，`source_track_count` 保存本次处理数；千首模式分界看前者。

### 偏好与推荐能力

- 默认 Skill 请求未携带已采集风格来源时，逐曲保持未知且不填八轴或自找 URL；公开标签由程序采集，再按单曲、专辑、艺人分别核对。只有曲目级证据能使单曲已分类。原歌单千首及以上只做有来源的歌手层级分析，逐曲分类数为 0；无分析执行器的 CLI 返回兼容状态 `analysis_agent_required`。
- 研究请求默认每批 20 首、每批 100000 字符，所有批次共享 600 秒执行预算。全曲目、全批次校验后才编译分析包；完整快照/词表/提示词摘要绑定，已完成批次可复用，失败留诊断，续跑用 analyze 而不是重新 run。
- 少于千首时要求曲目或专辑来源覆盖至少 50%，专辑标签仍只作背景；千首及以上要求有来源歌手的主艺人曲目权重至少 30%。资料不足时保留分析和补全清单，阻止作为完成结果发布。关系研究保持待独立核验。
- 最多 3 个确定性兴趣组，保留代表收藏并降低同一艺人批量曲目的影响。候选匹配最近兴趣组，不只依赖总平均；目录端点匹配防止无关候选靠引用获得关系加分。
- 默认至多 2 轮候选研究，可设 1 至 3 轮及候选/时间/字符预算，缺额按本次约束补充。只对入选的最多 10 首生成说明；候选池最低门槛为 1，不再因缺少召回类型而强制补齐，研究报告记录轮次、字符量、耗时和结果摘要；失败不输出无法通过契约的推荐。
- prepare-benchmark/benchmark 支持人工试听对照，未知标签不作拒绝，缺标签不下优劣结论，评分不当喜欢概率，反馈仍不自动进入排序。功能已用夹具验收，真实喜欢率与成本收益尚未测得。

## 三、歌单来源（Step 1 能力矩阵）

| 来源 | Reader | 登录要求 | 状态 |
|------|--------|---------|------|
| 本地导出 JSON | `local_json` / `apple_music_json` / `netease_json` | 无 | ✅ |
| 本地 CSV | `csv` | 无 | ✅（兼容标准导出表头） |
| 网易云公开歌单 | `netease_public` | 完全匿名 | ✅ 真实链路验证（热歌榜 200 首） |
| QQ 音乐公开歌单 | `qq_public` | 完全匿名 | ✅ 真实链路验证（`hasmore` 分页、`mid` 稳定 ID） |
| Apple Music 公开歌单 | Apple 官方网页服务 → `csv` | **免登录** | ✅ 官方分页直读 + 123 首真实验证 |
| Spotify | — | 需 Premium（2025 政策硬绑定） | ⏸️ 调研归档 |
| Apple Music 开发者 API | MusicKit / Apple Music API | 需开发者令牌 | ⏸️ 非当前路径 |

### Apple Music 导出链路

```
workflow.py export-apple-playlist --url <分享链接> --expected-count <独立确认的歌曲数>
    └─▶ tools/export_apple_playlist.mjs（Playwright 无头）
        Apple 官方嵌入页 → 官方 API 分页 → CSV → 暂存 → 编码/字段/数量校验 → 替换目标文件
    └─▶ input/apple_favorite_songs.csv
然后：workflow.py snapshot --reader csv ...
```

- 全程不需要 Apple ID 登录；只读取公开分享歌单，不保存网页临时访问令牌
- 不解析 Apple HTML 中的预渲染列表；以官方嵌入播放器发出的分页 JSON 为事实来源，分页或字段异常时显式失败
- 依赖：Node.js、Playwright 与 `csv-parse`，在 `tools/` 下执行 `npm ci` 和 `npx playwright install chromium --only-shell`；主工作流不依赖 Node.js。
- 官方分页完整结束时标记 `completeness_status: "confirmed"`；如提供独立数量则额外核对。失败保留原 CSV，输出支持 BOM、转义引号和跨行字段。
- 2026-09-18 使用用户当前公开分享链接完成线上验收：Apple 官方接口返回两页共 123 首，歌单名为 `Favorite Songs`，全部曲目经字段校验后写入 CSV；该结果只证明本次链接与当前官方页面可达。

## 四、代码结构

| 模块 | 职责 |
|------|------|
| `contracts.py` | 全部 JSON 契约与校验（配额算法也在此，保证校验器与排序器共用同一实现） |
| `source_adapters.py` | 各平台歌单 Reader（本地 JSON/CSV、网易云公开、QQ 公开） |
| `analysis_contracts.py` / `analysis_agent.py` | Step 2 研究请求与结果协议、精确快照绑定、分批/预算、执行/导入、断点续跑 |
| `musician_analyzer.py` | Step 2 程序聚合：校验 Skill 研究、统计艺人/风格分布、编译画像与关系；兼容显式目录模式 |
| `recommender.py` | 来源模型六项有据评分 + 配额 + MMR + 公开标签顺序衔接；旧 catalog 单独兼容 |
| `preference_model.py` / `candidate_routes.py` | 多兴趣画像、当前目录绑定的关系路线 |
| `agent_prompt.py` / `agent_runner.py` / `skill_runner.py` | 隔离 Skill 上下文、硬预算、准备工件摘要校验与原样复用、通用外部执行 |
| `skills/` | 第二步研究 Skill 与第三步候选 Skill 的模型/供应商无关约束和契约参考 |
| `research.py` / `explanations.py` | 有界分轮候选研究、诊断遥测、入选后程序说明 |
| `evidence.py` | 来源分类、稳定标识符格式检查、A/B/C 等级与事实核验状态分离；不信任 Skill 自报 verified |
| `evaluation.py` / `feedback.py` / `tune.py` | 反馈契约、六类离线指标、人工批准调优建议 |
| `benchmark.py` | 隐藏方案信息的试听清单、同输入的只读方案对照 |
| `workflow.py` | CLI 入口：`snapshot / analyze / prepare-skill / skill / validate / evaluate / tune / prepare-benchmark / benchmark / archive-schema1 / export-apple-playlist / web-export`；`prepare-agent / agent` 为兼容别名 |
| `tools/` | 维护性辅助工具（Apple 歌单导出），不属于纯 Python 工作流本体 |

## 五、质量与验证状态

- **测试**：本地改动需分别核对来源模式与显式 catalog 兼容模式的契约测试。此文档旧版记载的 191 项 Python、11 项 Node 和 1 项 Chromium 成绩属于 2026-09-09 的历史快照，不代表当前隔离副本或线上状态。
- **本次夹具链路**：`runtime/analysis-agent-acceptance-20260906-a49b1f/` 中 3 首输入经过 2 批 fake 分析 Skill，再由 fake 候选 Skill 生成 20 首候选、程序选出 10 首草稿，validate 重算文件逐字节一致。严格审计仍为 0/10 核验通过、10 首待核验，不写成功审计产物。两个 fake 执行器都不是实际音乐研究或真实质量测试。
- **真实快照准备**：复用 115 首快照，在 `runtime/apple-agent-analysis-20260906-7c83e1/analysis_research/` 准备 6 批，大小为 20/20/20/20/20/15，115 个位置恰好覆盖一次。单批 prompt 为 16320 至 18273 字符，总计 102104 字符。没有真实 Skill 结果，也没有为此歌单伪造画像或推荐。
- **历史真实验证记录（2026-09-04）**：网易云热歌榜 200 首、QQ 30 首、Apple Music 115 首三条链路曾跑通 snapshot → analyze → prepare-agent；本次未联网复测。
- **历史在线试运行（2026-09-11）**：当时用真实网易云歌单、网页档位和 OpenAI 兼容执行器产出 10 首推荐；所用模型当时记录为 `deepseek-v4.1-flash`。来源未经过可信在线事实核验，结果保持 `draft`。该记录不代表本次重构已在线复验或当前生产配置。

## 六、运维要点

- `input/`、`runtime/`、`secrets/`、`styles/artist_style_profiles.json` 均为本地个人数据，Git 忽略
- `analyze/run --as-of-date YYYY-MM-DD` 固定评分参考日期，默认取快照的 UTC 日期；参考日期、平台歌曲 ID、来源信息与策略进入 `analysis_id`。QQ 快照摘要覆盖全分页响应。
- `skill` 复用准备时的 prompt、预算和目录；自定义清单用 `prepare-skill/skill --manifest` 指定。配置冲突、缺失或摘要不匹配时重新准备，预算不足不调用 Skill；`agent` 仍为兼容入口。
- 默认不再要求私有画像；未配置分析执行器时只准备研究任务。续跑继承批次/字符预算，调整研究需新目录。公共示例只用于显式 catalog 兼容测试，不能代表个人完整画像。研究报告的字符量不是精确 token 或费用，Skill 关系也不是独立事实核验结果。
- 服务器入口 hermes_weekly.sh 透传 CLI 参数，可显式配置 --analysis-command 和每次独立的 --runtime-dir；没有执行器命令时不会擅自启用任何模型。
- 消息发送途径（微信 / OpenClaw / Pi wechatbot）已从仓库移除，流程只在网页端展示与产出内部文本报告。
- Schema 2.0 旧分析、上下文和排序工件需重新生成，不应手工补字段或改身份；Schema 1 历史产物只能归档不可复用。具体重建步骤见 README。
- 版本流程：无版本对象仓库，推送必须带 CHANGELOG 更新 + `patch-YYYYMMDD-HHMMSS` 维护标签
- 文档：`README.md`（用法）、`AGENTS.md`（仓库规则）、`CHANGELOG.md`（变更史）、`P0_P1_remaining.md`（遗留项）

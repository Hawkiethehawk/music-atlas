# Music Atlas 项目总结

> 更新日期：2026-09-09 ｜ 仓库：https://gitee.com/Hawkiethehawk/music-atlas ｜ 191 项 Python、11 项 Node、1 项 Chromium 测试通过（本地未发布重构）

## 一、项目是什么

Music Atlas 是一个**按需触发、不绑定时间**的歌单推荐工作流：对本次输入的**整个歌单做全量解析**，推荐数量固定为 **10 首**，与歌单规模无关。当前结果仅为带公开证据引用的**研究草稿**，不代表事实已核验或正式推荐。

```
Step 1 歌单快照 ──▶ Step 2 分析 Skill 研究 + 程序聚合 ──▶ Step 3 候选 Skill + 程序选曲 ──▶ 渠道文本
PlaylistSnapshot      MusicianResearchBundle / MusicianAnalysisPacket       RecommendationBundle
        └────────── 各步骤之间只用 JSON 契约连接，互不回头读取 ──────────┘
```

## 二、核心设计边界（代码强制，非文档自觉）

1. **输入隔离**：Step 2 仅以本次 `PlaylistSnapshot` 为偏好输入，分析 Skill 可研究公开资料；Step 3 只读本次 `MusicianAnalysisPacket`。默认 Skill 模式不加载本地画像、关系或偏好名单，平台登录态、个性化推荐和历史运行结果不进入管线。外部 Skill 执行器的文件/工具访问权限由执行环境约束，本地协议执行器不是沙箱。
2. **评分独占**：七维评分（风格/听感轴/关系/频率/新鲜度/证据质量/公开关联）、召回配额、去重、MMR 多样性、能量弧排序全部由 `recommender.py` 确定性计算；Skill 只能提交候选事实，关系路线、兴趣匹配和最终说明由程序拥有。已有排序包必须经过事实一致性检查与确定性重算，约束冲突采用有界回溯，预算耗尽不冒充无解。
3. **证据契约**：每首候选必须携带逐条证据（claim_type + URL），来源黑名单屏蔽个性化页面，证据 URL ⊆ sources；来源等级与公开关联分数按真实 URL 和事实类型推导，不信任 Skill 自报等级。不可用证据在排序前阻断，输出状态固定为程序拥有的 `publication_status: "draft"`。
4. **反馈只读**：评估与调优统一匹配当前分析并按真实时间取最新反馈，未反馈歌曲不作为拒绝；无有效对照时不建议调整。策略仅经 `analyze/run --policy-file` 显式加载，摘要纳入分析身份与 manifest，绝不自动应用建议。
5. **数量契约**：`declared_track_count == track_count == len(tracks)` 三方核对，数量永远来自本次 Step 1。

### 偏好与推荐能力

- 默认分析 Skill 逐批研究每首歌的风格混合、八轴描述、艺人/发行/单曲作用范围及主唱/关联项目，不再要求预先维护艺人目录。无真实执行器时先生成研究任务，返回兼容状态 analysis_agent_required；支持任意外部执行器或逐批 JSON 导入。
- 研究请求默认每批 20 首、每批 100000 字符，所有批次共享 600 秒执行预算。全曲目、全批次校验后才编译分析包；完整快照/词表/提示词摘要绑定，已完成批次可复用，失败留诊断，续跑用 analyze 而不是重新 run。
- 缺失画像使用 null，研究完成后分类覆盖不到 50% 时保留分析和补全清单，但阻止推荐 Skill 准备和排序。字段来源、置信度与作用范围可审阅；Skill 关系为 researched，全部公开事实继续待独立核验。
- 最多 3 个确定性兴趣组，保留代表收藏并降低同一艺人批量曲目的影响。候选匹配最近兴趣组，不只依赖总平均；目录端点匹配防止无关候选靠引用获得关系加分。
- 默认至多 2 轮候选研究，可设 1 至 3 轮及候选/时间/字符预算，缺额按本次约束补充。只对入选的 10 首生成说明，研究报告记录轮次、字符量、耗时和结果摘要；失败不输出不完整推荐。
- prepare-benchmark/benchmark 支持人工试听对照，未知标签不作拒绝，缺标签不下优劣结论，评分不当喜欢概率，反馈仍不自动进入排序。功能已用夹具验收，真实喜欢率与成本收益尚未测得。

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

### Apple Music 导出链路

```
workflow.py export-apple-playlist --url <分享链接> --expected-count <独立确认的歌曲数>
    └─▶ tools/export_apple_playlist.mjs（Playwright 无头）
        TuneMyMusic URL 加载 → Export to file → CSV → 暂存 → 编码/CSV/数量校验 → 替换目标文件
    └─▶ input/apple_favorite_songs.csv
然后：workflow.py snapshot --reader csv ...
```

- 全程无需 TuneMyMusic 注册/登录，也不需要 Apple ID 授权——公开分享链接即可
- 已知限制：直接抓取 Apple 网页只能拿到约 40 首预渲染曲目（SPA 懒加载），因此必须经 TuneMyMusic 解析；TuneMyMusic 页面改版时脚本会显式报错而非产出坏数据
- 依赖：Node.js、Playwright 与 `csv-parse`，在 `tools/` 下执行 `npm ci` 和 `npx playwright install chromium --only-shell`；主工作流不依赖 Node.js。
- 未提供独立数量时明确标记 `completeness_status: "unconfirmed"`；失败保留原 CSV，支持带 BOM、转义引号和跨行字段的标准 CSV。
- 上表平台能力的历史验证与本次工作流重构分开记录。2026-09-06 用户提供的 Apple Music 歌单已在本次对话前段导出为 115 首，页面总数与 CSV 数量一致，115 个非空且唯一的 Apple ID。此次重构只复用已保存快照，没有再次联网导出；Chromium 回归只覆盖本地下载辅助函数，不是线上页面全量兼容性验收。

## 四、代码结构

| 模块 | 职责 |
|------|------|
| `contracts.py` | 全部 JSON 契约与校验（配额算法也在此，保证校验器与排序器共用同一实现） |
| `source_adapters.py` | 各平台歌单 Reader（本地 JSON/CSV、网易云公开、QQ 公开） |
| `analysis_contracts.py` / `analysis_agent.py` | Step 2 研究请求与结果协议、精确快照绑定、分批/预算、执行/导入、断点续跑 |
| `musician_analyzer.py` | Step 2 程序聚合：校验 Skill 研究、统计艺人/风格分布、编译画像与关系；兼容显式目录模式 |
| `recommender.py` | 七维评分 + 硬配额 + MMR + 能量弧排序 |
| `preference_model.py` / `candidate_routes.py` | 多兴趣画像、当前目录绑定的关系路线 |
| `agent_prompt.py` / `agent_runner.py` / `skill_runner.py` | 隔离 Skill 上下文、硬预算、准备工件摘要校验与原样复用、通用外部执行 |
| `skills/` | 第二步研究 Skill 与第三步候选 Skill 的模型/供应商无关约束和契约参考 |
| `research.py` / `explanations.py` | 有界分轮候选研究、诊断遥测、入选后程序说明 |
| `evidence.py` | 来源分类、稳定标识符格式检查、A/B/C 等级与事实核验状态分离；不信任 Skill 自报 verified |
| `evaluation.py` / `feedback.py` / `tune.py` | 反馈契约、六类离线指标、人工批准调优建议 |
| `benchmark.py` | 隐藏方案信息的试听清单、同输入的只读方案对照 |
| `workflow.py` | CLI 入口：`snapshot / analyze / prepare-skill / skill / validate / send-weixin / send-weixin-pi / evaluate / tune / prepare-benchmark / benchmark / archive-schema1 / export-apple-playlist`；`prepare-agent / agent` 为兼容别名 |
| `channel_delivery.py` | OpenClaw 与 Pi wechatbot 微信交付：默认发送计划、SSH 云服务器调用、本地包装器调用与返回值校验 |
| `tools/` | 维护性辅助工具（Apple 歌单导出、OpenClaw 微信包装器），不属于纯 Python 工作流本体 |

## 五、质量与验证状态

- **测试**：191 项 Python、11 项 Node 单元测试与 1 项真实 Chromium 本地下载测试通过，`compileall` 和两个 Node 模块语法检查通过。新增分析 Skill、OpenClaw、Pi wechatbot 交付和微信报告格式测试，包括跨批计时、证据与身份约束、断点续跑、输入保护、完整双 Skill 夹具流程、微信目标校验、SSH 参数隔离、默认不发送和用户文案隔离；旧目录路径显式 catalog 后继续通过。
- **本次夹具链路**：`runtime/analysis-agent-acceptance-20260906-a49b1f/` 中 3 首输入经过 2 批 fake 分析 Skill，再由 fake 候选 Skill 生成 20 首候选、程序选出 10 首草稿，validate 重算文件逐字节一致。严格审计仍为 0/10 核验通过、10 首待核验，不写成功审计产物。两个 fake 执行器都不是实际音乐研究或真实质量测试。
- **真实快照准备**：复用 115 首快照，在 `runtime/apple-agent-analysis-20260906-7c83e1/analysis_research/` 准备 6 批，大小为 20/20/20/20/20/15，115 个位置恰好覆盖一次。单批 prompt 为 16320 至 18273 字符，总计 102104 字符。没有真实 Skill 结果，也没有为此歌单伪造画像或推荐。
- **历史真实验证记录（2026-09-04）**：网易云热歌榜 200 首、QQ 30 首、Apple Music 115 首三条链路曾跑通 snapshot → analyze → prepare-agent；本次未联网复测。
- **生产就绪前置条件**：补齐可信在线事实核验通道，并完成一次使用外部可验证证据的实时 Skill 试运行。当前离线格式检查不能证明事实真实性；本次未调用真实 Skill、发送微信消息或部署，仅新增并验证了显式交付入口。

## 六、运维要点

- `input/`、`runtime/`、`secrets/`、`styles/artist_style_profiles.json` 均为本地个人数据，Git 忽略
- `analyze/run --as-of-date YYYY-MM-DD` 固定评分参考日期，默认取快照的 UTC 日期；参考日期、平台歌曲 ID、来源信息与策略进入 `analysis_id`。QQ 快照摘要覆盖全分页响应。
- `skill` 复用准备时的 prompt、预算和目录；自定义清单用 `prepare-skill/skill --manifest` 指定。配置冲突、缺失或摘要不匹配时重新准备，预算不足不调用 Skill；`agent` 仍为兼容入口。
- 默认不再要求私有画像；未配置分析执行器时只准备研究任务。续跑继承批次/字符预算，调整研究需新目录。公共示例只用于显式 catalog 兼容测试，不能代表个人完整画像。研究报告的字符量不是精确 token 或费用，Skill 关系也不是独立事实核验结果。
- 服务器入口 hermes_weekly.sh 透传 CLI 参数，可显式配置 --analysis-command 和每次独立的 --runtime-dir；没有执行器命令时不会擅自启用任何模型。
- 微信交付入口 `workflow.py send-weixin` 和 `workflow.py send-weixin-pi` 默认只输出 `delivery_plan`；OpenClaw 方式的 `--dry-run` 调用 OpenClaw 试运行，Pi wechatbot 方式的 `--dry-run` 检查已保存凭据与 `context_token`，两者都只有 `--send` 才实际发送。两种云服务器方式均使用 SSH，且要求显式提供以 `@im.wechat` 结尾的直接用户目标。
- Schema 2.0 旧分析、上下文和排序工件需重新生成，不应手工补字段或改身份；Schema 1 历史产物只能归档不可复用。具体重建步骤见 README。
- 版本流程：无版本对象仓库，推送必须带 CHANGELOG 更新 + `patch-YYYYMMDD-HHMMSS` 维护标签
- 文档：`README.md`（用法）、`AGENTS.md`（仓库规则）、`CHANGELOG.md`（变更史）、`P0_P1_remaining.md`（遗留项）

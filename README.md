# Music Atlas

Music Atlas 是一个可扩展的歌单推荐工作流：**随时手动触发，不绑定时间；对本次输入的整个歌单做全量解析**。它把歌单读取、音乐人关系分析和 Skill 推荐拆成三个独立步骤，并通过 JSON 契约连接每一步。默认推荐目标为 10 首；候选不足时按实际可用数量输出 1–10 首，候选充足时仍输出 10 首。当前输出仅为**研究草稿**，不代表外部事实已核验或正式推荐。所有产物都供网页端展示；仓库不含任何消息发送途径。

## 设计边界

1. Step 1 将 Apple Music、网易云或本地 JSON/CSV 歌单统一为 `PlaylistSnapshot`。
2. Step 2 只以本次 `PlaylistSnapshot` 作为偏好输入，由分析 Skill 研究公开音乐资料，返回逐曲画像及主唱、前乐队和 side project 关系；程序独占校验、统计和多兴趣聚合。
3. Step 3 只接收本次 `MusicianAnalysisPacket`，由推荐 Skill 分轮研究候选事实；程序最多选出 10 首（候选不足时按可用数量收缩）后，生成与当前偏好、关系路径和实际评分一致的说明。

Step 3 使用混合音乐发现策略：Skill 只提交艺人延伸、音乐人关系、细分风格邻近和探索四类候选的结构化事实与逐项证据；固定代码计算风格、听感轴、关系、频率、新鲜度、证据质量和公开关联七项分数，按可用候选自适应召回配额，再执行去重、项目覆盖、MMR 多样性控制和能量弧线排序。当前不调用平台个性化接口；反馈契约与离线评估已就位，但行为反馈只用于评估与人工批准的调优建议，不参与候选发现或自动排序。

Apple Music 只用于歌单快照和最终跳转链接。Apple Music 或网易云的个性化推荐、登录状态和历史运行结果不参与候选发现、排序或说明生成。

## 安装（一键部署）

```powershell
pwsh -File install.ps1        # Windows / PowerShell
```

```bash
./install.sh                  # Linux / macOS
```

安装脚本本身只做一件事：定位 Python 并执行内置的 `atlas setup`。`setup` 会：

1. 检查 Python ≥ 3.10、Node.js ≥ 18、npm、仓库配置、系统密钥库（keyring）与核心模块；
2. 安装缺失依赖：`pip install keyring`（网页保存 AI API Key 用）、`tools/` 与 `web/` 的 `npm ci`、Playwright Chromium（Apple 歌单导出需要）；
3. 注册 `atlas` 命令：在用户级 bin 目录生成只调用本仓库的包装脚本（Windows 追加用户 PATH 并广播设置变更；Linux/macOS 写 `~/.local/bin/atlas`，目录不在 PATH 时只提示，不改 shell 配置）。

注册是幂等的：重复执行不会重复追加 PATH 条目，也不会覆盖已有 `atlas`。可选参数：

| 参数 | 说明 |
|---|---|
| `--check-only` | 只检查环境，不做任何安装或注册 |
| `--skip-node-deps` | 跳过 `npm ci` |
| `--skip-browser` | 跳过 Playwright Chromium 下载 |
| `--skip-register` | 跳过 `atlas` 命令注册 |
| `--json` | 机器可读输出 |

```bash
python atlas.py setup --check-only     # 与 atlas setup --check-only 等价
```

安装完成后新开一个终端即可：

```bash
atlas status
atlas start
```

纯 Python 工作流（Step 1–3、CLI）不需要 Node.js；Node 与 Playwright 只用于网页面板和 Apple 歌单导出。

## 快速开始

仓库不包含任何个人歌单或运行产物。使用公开测试夹具可以完整运行本地链路：

```bash
python workflow.py run \
  --input tests/fixtures/playlist_sample.json \
  --reader local_json \
  --platform apple_music \
  --playlist-id sample \
  --playlist-name '示例歌单' \
  --analysis-command 'python tests/fixtures/fake_analysis_agent.py' \
  --analysis-batch-size 2 \
  --runtime-dir runtime/local-run
```

该命令执行 Step 1、测试分析 Skill、程序聚合和 Step 3 上下文准备。两个 fake 执行器都只生成明确标识的合成夹具，不联网、不调用真实模型、不发送消息；不要用于真实歌单画像或质量评估。每次新测试使用新的 runtime 目录。

使用仓库内的测试执行器验证 Step 3 合同：

```bash
python workflow.py skill \
  --analysis runtime/local-run/musician_analysis.json \
  --prompt runtime/local-run/agent_prompt.md \
  --output runtime/local-run/recommendation_bundle.json \
  --channel-output runtime/local-run/channel_text.txt \
  --command 'python tests/fixtures/fake_agent.py'
```

`--command` 指向的进程从标准输入读取 Skill 任务，并向标准输出写入 Schema 2.0 的候选池 `RecommendationBundle`。候选阶段的 `explanation` 可省略；程序选曲后写入独占的 `program_explanation`，不采纳 Skill 说明作为排序依据。写入内部文本报告前校验推荐数量、项目覆盖、重复歌曲、关系路径和逐事实公开来源。

Skill 的 `ready` 输出只能是 `candidate_pool`，不能预填评分、选曲或排序字段，也不能提交 `ranked` 包。已有 `ranked` 文件在 `validate`、`evaluate` 和 `tune` 中会核对曲目信息，并从原候选池重新计算评分、入选歌曲和顺序；不一致时拒绝处理。结构链路通过不代表外部事实已经核验。

来源等级和公开关联分数从证据 URL 与事实类型推导，不使用 Skill 自报的等级、来源类别或 `verified` 来加分。已知矛盾、不可访问、过期或受支持来源标识符格式错误的证据，会在排序前被拒绝。排序包的 `publication_status: "draft"` 由程序写入，Skill 不得提交；网页端与内部报告均标注“研究草稿”和“不是正式推荐”。

选曲先按原有贪心偏好尝试；发生召回类型、艺人名额或项目覆盖冲突时确定性回溯。搜索最多访问 50,000 个状态；“约束无解”与“搜索预算耗尽、尚不能判定是否有解”分别报错。

`--command` 保持 `shell=False`。Windows 内部命令行遵循 Windows 引号规则，含空格的可执行路径使用双引号；POSIX 使用 shell 风格分词，但不启动 shell。标准输入输出使用 UTF-8。

## 分析 Skill 工作流

`run/analyze` 默认 `--analysis-mode skill`，不再依赖预置的四位艺人示例。流程为：

```text
PlaylistSnapshot
  -> 按艺人聚集、按曲目数分批的研究请求
  -> 分析 Skill 的 MusicianResearchResult
  -> 全批次校验的 MusicianResearchBundle
  -> 程序统计、画像聚合、多兴趣分组与覆盖率检查
  -> MusicianAnalysisPacket -> 推荐 Skill -> 程序评分与 10 首草稿
```

分析 Skill 负责公开资料研究、每首曲目的风格混合/八轴描述、艺人/发行/单曲作用范围、置信度、来源与关系事实。它不能提交统计数、兴趣分组、评分或策略；未知也必须返回，保留 null，不允许遗漏、重复或替换曲目。每条已分类画像需要 style 证据，每项关系需要 relation 证据；证据必须有事实描述、URL 和实际检索时间。未来时间、过期、已知不可用证据和个性化来源会被拒绝。画像八轴是描述性估计，不是音频实测。

没有配置 `--analysis-command` 时只准备研究任务，返回兼容状态 `analysis_agent_required`，不生成伪分析或推荐。以已有快照为例：

```bash
python workflow.py analyze --snapshot runtime/current/snapshot.json \
  --output runtime/current/musician_analysis.json
```

执行研究可使用配置好公开检索工具的外部 Skill 执行器。执行器从 stdin 读取每批完整任务，从 stdout 返回一个 JSON 对象；具体字段和示例见每批 prompt。也可以在其他环境完成这些任务，将 JSON 放到 manifest 指定的 `batch-NNN.result.json`，再离线导入：

```bash
python workflow.py analyze --snapshot runtime/current/snapshot.json \
  --output runtime/current/musician_analysis.json --import-analysis-results
python workflow.py prepare-skill --analysis runtime/current/musician_analysis.json \
  --output runtime/current/agent_prompt.md
```

`--analysis-command`、`--import-analysis-results`、`--research-bundle <完整研究包路径>` 三者互斥。程序没有内置某个模型、模型供应商或检索服务；执行器可以使用任意可用模型和工具，Music Atlas 只校验 JSON 契约。工具配置、授权与费用由外部执行器管理；本代码是协议执行器而不是进程沙箱，文件/工具权限须由外部环境约束；默认不会启动任何模型。

| 分析参数 | 默认值与行为 |
|---------|-------------|
| `--analysis-research-dir` | 分析输出旁的 `analysis_research/` |
| `--analysis-batch-size` | 新任务 20 首，范围 1 到 50；续跑继承 manifest |
| `--analysis-context-budget` | 新任务每批 100000 字符；续跑继承 manifest，超预算不截断曲目 |
| `--analysis-timeout` | 每次执行所有分析批次共用 600 秒，不按批重置 |
| `--analysis-parallelism` | 分析研究并行任务数；网页默认 5，范围 1 到 16 |

每批结果先校验再保存，已完成批次续跑时重新校验并复用，不重复调用命令。提示词、配置、快照或词表不匹配时拒绝复用；研究包绑定完整快照摘要，不能因 snapshot_id 相同而复用另一时间或内容的快照。失败写入 `research_report.json`，保留已完成批次及原有产物，不生成新的分析或推荐。需要重新研究时使用新的研究目录；不要直接修改 prompt/manifest。

`run` 是新快照入口。已有研究任务后用 `analyze --snapshot` 继续，重复 `run` 不会覆盖已绑定的快照。`analyze` 的 Markdown、manifest 和覆盖报告默认与 JSON 输出放在同一目录，输出不能覆盖输入或互相覆盖。

分析与推荐的并行边界：Step 2 内部默认 5 个任务并行；只有 Step 2 的全部批次、校验、聚合和覆盖率检查成功后才启动 Step 3。Step 3 内部默认 4 个候选研究任务并行（可选 3），所有候选池返回后才由程序统一去重、评分和排序；并行任务不会改变两个步骤之间的先后关系。

Skill 模式不读取本地画像、关系目录或 `preferred_artists.txt`，显式传入这些参数也会拒绝。`agent` 是旧兼容别名。离线兼容可使用 `--analysis-mode catalog --style-profiles styles/artist_style_profiles.example.json`；直接调用不带 `research_bundle_path` 的 Python 分析 API 仍保持原目录模式。目录模式与 Skill 模式不会默默混合。

所有研究事实继续标记待独立核验：关系状态为 `researched`，不能冒充 `confirmed`；自报 `verified` 在编译时降为 `unverified`。推荐 Skill 只消费当前分析包，包含画像来源及当前研究的关系，不读取研究目录或原始快照。

## 数量契约

歌曲数量始终来自本次 Step 1 输出：

```text
declared_track_count == track_count == len(tracks)
```

代码不固定某个歌曲总数。Step 2 不重新抓取歌单，但分析 Skill 可以研究公开音乐资料；Step 3 不读取原始快照、登录 profile 或历史推荐，只接收本次分析包及本次候选研究的补充请求。

CSV 的声明数量优先级为 `--declared-count` → `--declared-count-file` → CSV 数据行数，并在 `reader.declared_count_source` 中记录来源。指定的数量文件缺失、损坏或没有合法数量时必须报错，不回退到行数；数量不一致的快照标记为 `incomplete`，阻止 Step 2。

## 风格口径

`style_analysis.style_distribution` 是每首歌只有一个主风格的互斥审计分布；`style_analysis.overlap_style_distribution` 是逐曲多标签覆盖分布。一首歌可以命中多个细分风格，因此后者的覆盖率不要求合计 100%。

## 运行耗时

耗时取决于歌单规模、外部平台和模型响应。2026-09-21 的线上完成任务统计如下，不能把单次理想样本当作稳定承诺：

| 工作流 | 样本数 | 中位耗时 | 最大耗时 |
|---|---:|---:|---:|
| 完整工作流 | 10 | 248.8 秒 | 350.3 秒 |
| 仅重新推荐 | 9 | 127.8 秒 | 216.3 秒 |

关键手段是 `runtime.openai_compat.disable_thinking: true`：实测真实批次单次调用从 65.1s（reasoning 14973 tokens）降到
15.8s（reasoning 0），输出仍是合法 JSON。候选池最低门槛为 1，推荐目标仍为 10；候选不足时允许按实际数量降级，且不再为凑齐类型启动额外补缺轮。上述数据只描述该批次线上观测，后续应按运行历史持续更新。

## 兴趣组命名

网页「音乐版图」的兴趣组名字由程序从本次歌单实际聚合出的主导风格总结生成：取该组 `style_mix` 权重最高的风格，用 `styles/style_taxonomy.json` 的 `label`，去掉英文与分隔符号后保持在 **3–5 个汉字**（超长标签保留结尾核心词，如 `现代另类金属核` → `另类金属核`）。同一期内名字互不重复，素材不足时依次尝试下一个风格，全部不合格才回退为 `兴趣组 NN`。

命名与聚类同层、完全确定，不调用 Agent，也不改变 Step 1–3 契约。人工 `--editorial` 配置里的 `interests[].name` 仍然优先，并会被自动命名避让。

## 偏好分析

- **未知不是低分**：缺失的听感画像使用 JSON `null`，不会当成安静、轻柔或低能量偏好。所有分析研究批次完成后，默认至少 50% 的当前曲目有完整分类，才允许准备推荐 Skill 上下文或排序；低覆盖仍保留分析与补全清单。
- **精确覆盖**：Skill 逐曲声明 `artist/release/track` 判断层级，程序保留逐曲证据与字段来源，再聚合艺人摘要。兼容目录模式的 `release_overrides` 仍支持 `match_titles`、`match_albums`，双条件同时命中，曲目覆盖优先于专辑覆盖；不把艺人级判断冒充逐曲听音。
- **多个兴趣组**：从已分类曲目确定性构建最多 3 个 `interest_profiles`，同时保留代表曲目、风格混合和听感轴。置信度加权并按艺人曲目数的平方根降低重复艺人影响；同一候选的风格与听感评分匹配同一兴趣组，避免把相反偏好平均成未观察到的中间偏好。
- **关系不能靠引用加分**：`candidate_routes.py` 将候选艺人与本次分析包的艺人身份或关系项目端点匹配，关系可来自分析 Skill 或显式兼容目录。关系需要来源和中/高置信度；随意增加 `analysis_refs` 不增加关系或频率分。研究阶段纠正错误的召回类型并记录 `route_corrections`，直接导入的类型不一致候选包会被拒绝。

这些是描述性画像与当前分析包内的关系匹配，不是音频实测、用户喜欢概率或独立事实核验。兴趣分组最多做三轮种子选择，不引入训练任务或新的 Python 依赖。

## 分阶段候选研究

默认首轮研究策略要求的最小候选池；合并去重后未达到候选目标或无法满足选曲约束时，再请求补充缺额和新候选。候选目标默认采用最低门槛 `1`，因此候选不足时可直接按实际数量输出，不会为凑齐召回类型虚构结果。补充请求只携带本次研究的缺额、约束失败原因与排重标识，不引入历史偏好。

推荐 Skill 入口 `workflow.py skill`、`skill_runner.py` 均支持：

| 参数 | 默认值与边界 |
|------|------------|
| `--candidate-target` | 默认采用策略 `candidate_pool_min`（1），不得低于策略最小值 |
| `--max-research-rounds` | 默认 2，包含首轮，允许 1 到 3 |
| `--max-candidates` | 默认 80，最大 200，必须不小于候选目标 |
| `--timeout` | 默认 600 秒，为整个研究循环共用的预算，不按轮重置 |

例如在快速开始生成的上下文上验证两轮候选研究：

```bash
python workflow.py skill --analysis runtime/local-run/musician_analysis.json \
  --prompt runtime/local-run/agent_prompt.md --output runtime/local-run/expanded.json \
  --channel-output runtime/local-run/expanded.txt --candidate-target 40 \
  --max-research-rounds 2 --max-candidates 80 --timeout 600 \
  --command 'python tests/fixtures/fake_agent.py'
```

补充轮仍遵守准备时的字符硬预算，原始 prompt/manifest 保持不变。预算耗尽或 Skill 执行器失败时仅写诊断，不生成或覆盖推荐与内部报告。程序只给最终最多 10 首生成说明，包含代表收藏、目录路径、具体听感差异、相对本次输入的新鲜点及草稿限制。

输出旁的 `<bundle 名>.research.json` 记录轮次、各轮 prompt 摘要、输入/输出字符数、耗时、候选预算和说明数量；成功时绑定排序包的精确摘要。字符量不是计费 token 或真实费用，fake Skill 执行器耗时也不代表真实检索延迟。

## Skill 提示词插槽

`prompts/` 下的 Markdown 文件是可编辑提示词空间，按固定顺序附加到 Skill 任务。策略占位符会由当前分析包渲染，其他空占位符会明确显示“无额外要求”。提示词不能改变 Step 1 数量、Step 2 统计、当前输入隔离、硬性召回配额、去重上限或证据契约。每次运行的提示词文件摘要会写入 Skill context manifest。

## 扩展点

- `source_adapters.py`：本地 JSON、Apple Music JSON、网易云 JSON 和 CSV 读取器，以及 `netease_public` / `qq_public` 公开歌单匿名读取器。
- 网易云公开歌单（匿名，不使用登录态）：

  ```bash
  python workflow.py snapshot --reader netease_public --platform netease \
    --playlist-id 3778678 --playlist-name '热歌榜' --output runtime/snapshot.json
  ```

  `--playlist-id` 接受数字 ID 或 `music.163.com` 分享链接（含 `#/playlist/...` 形式）；网页入口另外接受网易云 `163cn.tv` 短链并在服务端解析为歌单 ID。当前适配器使用 v6 歌单详情接口读取完整 `trackIds`，对超过接口首批 1000 首的歌单再按每批 200 首调用歌曲详情接口，按原顺序重建快照；任一曲目无法解析时保持 `reader_status: "incomplete"`，阻止后续步骤。仅能读取公开可访问歌单，隐私或权限受限来源不会伪造结果。
- QQ 音乐公开歌单（匿名，不使用登录态）：

  ```bash
  python workflow.py snapshot --reader qq_public --platform qq_music \
    --playlist-id 7399480361 --output runtime/snapshot.json
  ```

  `--playlist-id` 接受数字 ID 或 `y.qq.com` 分享链接；自动按接口 `hasmore` 信号分页拉取（每页 100 首）；歌曲以 `mid` 作为稳定 `platform_track_id`；数量来自接口 `total_song_num`，与实际解析数不一致时 `reader_status` 为 `incomplete`。`input_sha256` 与 `snapshot_id` 覆盖按顺序、带长度前缀的全部页面响应，reader 同时记录实际页数与逐页摘要，后续页变化不会被首屏摘要掩盖。
- Apple Music 公开歌单（免登录，官方网页服务）：无头浏览器打开 Apple Music 官方嵌入播放器，取得该页面的临时访问上下文后，从 `amp-api.music.apple.com` 按 `next` 分页读取完整曲目；不保存令牌或浏览器状态。输出 CSV 后继续用 `csv` reader 建立快照，`Apple - id` 作为稳定 `platform_track_id`。官方分页未完整结束、曲目字段缺失或独立数量不匹配时停止后续分析，且不覆盖旧文件。
- `relations/artist_relations.json`：可审计的公开音乐人关系目录。
- `analysis_contracts.py` + `analysis_agent.py`：绑定当前快照的研究协议、分批、上下文硬预算、外部分析 Skill 执行、结果导入与断点续跑。
- `skills/music-atlas-analysis/` + `skills/music-atlas-recommendation/`：两个模型与供应商无关的 Skill 契约说明和约束参考。
- `reports.py`：推荐结果的内部纯文本报告（含排序与证据门禁），不发送任何消息。
- `visualization_interface.py`：预留可视化后端接口，当前不包含实现。
- `hermes_weekly.sh`：服务器侧调度入口模板；个人输入和运行目录应在部署环境中单独配置。
- `feedback.py` + `evaluation.py` + `tune.py`：反馈输入契约、只读离线评估与人工批准的调优建议。
- `evidence.py`：离线来源分类、稳定标识符格式检查与 claim_type × source_class 的 A/B/C 等级规则。格式正确不等于事实已核验；可信在线核验适配器尚未实现，模块不发请求。
- `preference_model.py` + `candidate_routes.py`：多兴趣画像与当前目录绑定的候选路线。
- `research.py` + `explanations.py`：有界分轮研究与程序拥有的入选说明。
- `benchmark.py`：同输入的人工试听清单与只读方案对照。
- `web_view_model.py` + `workflow.py web-export`：将已校验的运行产物转换为网页专用、只读的脱敏数据。
- `config/web.json`：网页服务基础配置；网页右上角「设置」把安全覆盖保存到 Git 忽略的 `runtime/web/settings.json`，不改写基础配置。
- `web/`：Editorial Atlas 网页及零依赖 Node 静态服务；网页通过 `GET /api/atlas` 读取导出数据。网页提交歌单链接后先读取歌单，再让操作者选择处理数量（分位 `25%/50%/100%`，按真实曲目数向上取整），随后继续分析与推荐；生产环境要求普通用户登录，歌单配置、偏好与七天推荐去重缓存按用户隔离。系统设置与 AI API Key 已收拢到独立管理员入口 `/admin`，管理员会话才能访问；密钥只保存到系统密钥库（Windows Credential Manager / macOS Keychain / Linux Secret Service），网页不回显；环境变量 `MUSIC_ATLAS_API_KEY` 仍可作为回退方式。Node 运行时需为 22.5+。
- `secret_store.py`：跨平台系统密钥库封装（基于 keyring），完成密钥库可用性校验，拒绝 keyrings.alt 等不安全后端。
- `atlas.py` + `web_service.py`：统一 CLI 入口与面板服务管理。`atlas start [--port N] [--no-open] [--force]` 启动面板（已运行则复用），`atlas stop` / `atlas restart` / `atlas status [--json]` / `atlas logs`；其余子命令透传 workflow.py（如 `atlas run ...`）。停止与替换有身份防护：`/api/health` 携带 `service/pid/web_root`，只有本项目面板会被停止；端口被其他服务占用时默认拒绝，`--force` 才替换；状态文件 PID 存活但健康端点不可用时拒绝覆盖。日志在 `runtime/web/service.log`。Windows 下可将项目根加入 PATH 后用 `atlas` 直呼（`atlas.bat`）。

Apple 自动导出入口为 `python workflow.py export-apple-playlist --url <分享链接> --expected-count <独立确认的歌曲数>`。下载先进入临时目录，经 UTF-8、标准 CSV 解析与数量检查后才替换目标文件；失败保留原文件。不提供 `--expected-count` 时输出 `completeness_status: "unconfirmed"`，不能仅凭 CSV 行数宣称完整。安装与浏览器验收见 [tools/README.md](tools/README.md)。

## 输出文件

`workflow.py run` 默认生成快照、分析研究 prompt/manifest；配置分析 Skill 并完成研究后才生成分析、Markdown 和推荐上下文。研究工件含每批 result、完整 `research_bundle.json` 和 `research_report.json`；后者记录模式、批次状态、复用、字符数、耗时和研究包摘要。覆盖不足会在准备推荐 Skill 前停止，保留分析及 `coverage_report.json`。随后执行 `workflow.py skill` 才生成已排序的 `recommendation_bundle.json`、`channel_text.txt` 和 `recommendation_bundle.research.json`。网页展示前执行 `workflow.py web-export`，生成运行目录内的 `web_payload.json`，再由 `web/server.js` 按 `config/web.json` 中的稳定发布路径只读提供。网页工作流在 Step 1 与 Step 2 之间多一个等待点：歌单读取完成后暂停等待网页提交前 N 首（`awaiting_limit`），提交后按原顺序截断快照（截断前总数保留在 `reader.source_track_count`，快照 ID 追加 `-limitN`）再进入分析；CLI 直接调用不带该等待参数时行为不变。运行目录和网页数据默认被 Git 忽略，默认配置纳入 Git。

网页导出示例：

```bash
python workflow.py web-export \
  --runtime-dir runtime/apple-link-20260908 \
  --output runtime/apple-link-20260908/web_payload.json
```

默认允许展示当前工作流的研究草稿；若需要只导出正式可发布数据，增加
`--require-publishable`，当发布状态或证据审计未通过时命令会拒绝写出。

如果已有本次运行的 Step 2/Step 3 文件，也可以只校验并生成内部报告：

```bash
python workflow.py validate \
  --analysis runtime/local-run/musician_analysis.json \
  --bundle runtime/local-run/recommendation_bundle.json \
  --ranked-output runtime/local-run/recommendation_bundle.ranked.json \
  --output runtime/local-run/channel_text.txt
```

`validate` 支持附带证据离线审计工件（确定性验证来源类别、稳定公共标识符与 A/B/C 声明等级，不发网络请求）：

```bash
python workflow.py validate \
  --analysis runtime/<run>/musician_analysis.json \
  --bundle runtime/<run>/recommendation_bundle.json \
  --evidence-audit runtime/<run>/evidence_audit.json
```

审计分别报告 `grade_valid`、`identifier_status` 和 `verification_result`，`accepted_count` 仅统计真正核验通过的推荐；来源等级合规、标识符格式正确和 Skill 自报 `verified` 都不构成事实核验。负面判定与过期状态不会被重复 URL 掩盖；检索时间无时区时按 UTC 处理，非法日期返回契约错误。

指定 `--evidence-audit` 会启用严格检查：`rejected` 或 `pending_verification` 时返回退出码 2，仅保留审计报告，不生成或覆盖本次排序文件与内部报告。当前没有可信在线核验适配器，因此有推荐的离线审计不会报告“核验通过”；fake Skill 的 10 首测试数据也为 0 首核验通过。不带此选项仍检查结构、确定性排序与已知不可用证据，但只生成草稿，不证明事实真实性。

## 反馈记录与离线评估

推荐发出后可以按固定契约记录用户反馈（saved / skipped / replayed / hidden + 时间戳 + 推荐与分析标识）。反馈只用于**只读**离线评估，绝不参与排序：

```json
[{"schema_version": "2.0", "record_type": "recommendation_feedback", "outcome": "saved", "timestamp": "2026-09-04T08:00:00Z", "analysis_id": "analysis-...", "recommendation_id": "musicbrainz:..."}]
```

把已排序 bundle 与反馈日志对比，输出六类评估指标（precision、novelty、diversity、calibration、artist/project repetition、sequence quality），报告文件标记 `policy_changed: false`：

```bash
python workflow.py evaluate \
  --analysis runtime/<run>/musician_analysis.json \
  --bundle runtime/<run>/recommendation_bundle.ranked.json \
  --feedback runtime/<run>/feedback_log.json \
  --output runtime/<run>/evaluation_report.json
```

策略调优只能走人工批准循环：`tune` 基于反馈与当前策略生成**建议工件**（`approval_required: true`、`auto_applied: false`），权重/召回组合/上限/排序权重的调整必须由人工编辑策略 JSON 后显式应用，未审阅反馈永远不会自我调整：

```bash
python workflow.py tune \
  --analysis runtime/<run>/musician_analysis.json \
  --bundle runtime/<run>/recommendation_bundle.ranked.json \
  --feedback runtime/<run>/feedback_log.json \
  --output runtime/<run>/tuning_proposal.json
```

评估与调优共用反馈解释逻辑：只匹配当前分析中的入选歌曲，按真实时间取最新记录；时区统一比较，同一时刻按记录内容摘要稳定决胜，历史非法反馈时间排在合法时间之前并按文本比较。未反馈歌曲不计入拒绝组或校准样本。没有有效反馈，或缺少接受/拒绝任一组时，建议状态为 `insufficient_feedback`，所有调整建议为空；每组至少 1 条仅是最低门槛，不代表统计显著性。

规则分数不是喜欢概率。`calibration` 保留已反馈样本的分箱描述，但返回 `status: "not_calibrated"`、`expected_calibration_error: null`，不再把规则分数除以 100 后当成概率计算误差。

### 人工试听对照

先准备隐藏 A/B 归属、排名和分数的合并试听清单。两份方案必须来自相同收藏、来源快照、显式艺人偏好和评分日期；策略或画像可以不同，此时用 `--analysis-b` 指定第二份分析，省略时共用 A 的分析。

```bash
python workflow.py prepare-benchmark --analysis-a runtime/local-run/musician_analysis.json \
  --bundle-a runtime/local-run/recommendation_bundle.json --bundle-b runtime/local-run/expanded.json \
  --output runtime/local-run/listening.json
```

试听后在清单的 `judgments` 中人工填写 `verdict`：`like`、`dislike`、`unsure`，未听保持 `null`；可填 `allowed_reasons` 中的理由及 `notes`。未标注和拿不准都不算拒绝；清单只用于离线评估，不进入下一次偏好输入。生成命令拒绝覆盖已有标注文件。

```bash
python workflow.py benchmark --analysis-a runtime/local-run/musician_analysis.json \
  --bundle-a runtime/local-run/recommendation_bundle.json --bundle-b runtime/local-run/expanded.json \
  --judgments runtime/local-run/listening.json --output runtime/local-run/comparison.json \
  --research-report-a runtime/local-run/recommendation_bundle.research.json \
  --research-report-b runtime/local-run/expanded.research.json
```

报告包含反馈覆盖、接受率、新艺人接受率、兴趣覆盖、理由分布和证据状态。研究报告可选，必须匹配对应分析与排序包摘要；提供时比较实测耗时、输入字符量及预算是否相同。20 与 40 候选目标不是等预算实验，报告会明确标记。任一方案未全部获得明确标签时状态为 `incomplete_labels`，质量差值为 `null`；全部标注后也只作描述性比较，不选赢家、不宣称统计显著性、不自动修改策略。

### 显式应用人工策略

`analyze` 和 `run` 支持 `--policy-file`。文件直接包含最终策略值，而不是整份 `tuning_proposal.json` 或增量。例如：

```json
{
  "max_per_artist": 1,
  "ranking_weights": {"style_fit": 0.25, "axis_fit": 0.25}
}
```

```bash
python workflow.py run --input tests/fixtures/playlist_sample.json --reader local_json \
  --policy-file runtime/reviewed_policy.json --runtime-dir runtime/policy-run \
  --analysis-command 'python tests/fixtures/fake_analysis_agent.py'
```

对象字段递归覆盖，未提供的字段保留默认值；数组整体替换。可调整评分权重、四类召回比例、艺人/项目上限、最低项目覆盖、候选池最小数量、多样性、序列与显示策略。新增 `analysis_quality.min_classified_share`（默认 0.5，范围大于 0 且不超过 1）、`diversity_policy.new_interest_bonus`（默认 4）与 `min_interest_groups`（默认 1，实际约束不超过本次可用兴趣组数）。默认 `candidate_pool_min` 为 1：它只规定最低候选门槛，不再要求候选池先覆盖全部召回类型；候选不足时推荐数可低于 10，但不会超过 `target_recommendations`。这些值仅通过人工策略文件修改，不自动放宽证据门槛。各组权重和召回比例总和必须为 1；未知字段、非法数值及固定设计边界修改会被拒绝，正常目标仍为 10 首。

未指定文件时只使用默认策略，不自动发现或应用调优建议。最终策略 SHA-256 纳入 `analysis_id`、分析 manifest 和管线 manifest，且不会修改共享的 `DEFAULT_POLICY`。应用策略后重新执行 Step 2/3，不能把旧候选包当作新分析的结果使用。

## 上下文预算与画像覆盖报告

`run`、`prepare-skill` 与 `skill` 支持 `--context-budget <正整数字符数>`；`prepare-agent` 与 `agent` 仍作为兼容别名。预算是硬上限：超预算时按固定顺序剔除放不下的可编辑插槽；若固定载荷仍过大，则确定性移除候选研究不需要的 Step 2 原始证据副本和兴趣分配展开，保留分析身份、偏好结论、taxonomy、关系名称、引用 ID、策略及去重键。紧凑载荷仍超预算时直接报错，不调用 Skill。成功裁剪后 `budget_exceeded: false`、`original_budget_exceeded: true`，紧凑载荷会额外记录 `payload_compacted`。字符数、估算 token 与裁剪报告写入 context manifest；`run` 还记录 pipeline manifest。token 数仅为估算，不能当成模型 tokenizer 的精确计数。

准备阶段保存 prompt、分析投影、固定指令的摘要，以及 `run_config` 中的预算和提示词目录。`skill` 原样复用已准备的 prompt，不重新读取插槽来覆盖它；省略预算或 `--prompt-dir` 时继承准备配置。修改插槽后必须重新 `prepare-skill` 才会生效；显式参数冲突、上下文缺失或摘要不匹配也要求重新准备。只有 prompt 和 manifest 都不存在时，`skill` 才自动首次准备。

`prepare-skill --manifest <路径>` 与 `skill --manifest <同一路径>` 可指定 context manifest；省略时使用提示词文件同目录的 `agent_context_manifest.json`（文件名保持兼容）。自定义多个 prompt 时也应使用各自的 manifest，避免共用同一清单。

`analyze`/`run` 在兼容目录回退到公共示例或研究后存在未分类艺人/曲目（`profile_coverage.degraded`）时，会额外写出 `coverage_report.json`。报告列出覆盖率、最低门槛、未分类艺人及按受影响曲目数排序的 `review_queue`，附曲目、专辑和待研究字段。默认 Skill 模式无需私有目录；研究仍有缺口时补充来源后重新聚合，不自动降低门槛。曲目分类门槛与艺人完整覆盖分别报告，只有署名、没有主艺人曲目的艺人仍可能显示画像缺口。

## 评分日期与产物重建

`analyze` 与 `run` 支持 `--as-of-date YYYY-MM-DD`。不指定时采用本次快照 `captured_at` 转换到 UTC 后的日期，不使用再次分析当天的日期。新鲜度评分只读取此参考日期；同一快照、参考日期和配置可复现相同分析身份与评分，`generated_at` 仅作为生成时间元数据。

`as_of_date`、规范化曲目信息（含平台歌曲 ID）、歌单来源信息和最终策略均纳入 `analysis_id`。显式改变参考日期会生成新分析身份；证据是否过期仍按审计执行时的时间检查，不能用旧参考日期延长证据有效期。

本次仍使用 Schema 2.0，但旧工件需要重建：默认改为分析 Skill 研究，旧目录结果不能冒充研究包；需从当前快照执行 `analyze`，完成全部研究批次，再重新 `prepare-skill` 与 Step 3。需要保留旧目录行为时显式使用 `--analysis-mode catalog`。旧排序包缺少草稿状态、来源路线或重算不一致时会被拒绝；试听清单必须基于新结果生成。QQ 历史单页摘要还需重新获取快照。不要手工补字段、改 `analysis_id`，或将旧候选池冒充新分析的结果；历史文件不会自动覆盖或迁移。

## 历史 Schema 1 产物归档

当前代码只消费 Schema 2.0 工件。历史 Schema 1.0 的运行时目录（`runtime/` 下含 1.0 JSON 的目录）会被整体迁移到带时间戳的归档目录并写入 manifest，**不会删除**：

```bash
python workflow.py archive-schema1
```

归档后实时运行前必须重新执行 `run` 生成 Schema 2 工件。`runtime/` 与 `input/` 均被 Git 忽略，归档只影响本地目录。

Schema 1.0 的历史分析包和直接推荐包不能复用；修改后需要重新运行 Step 1/2 并生成 Schema 2.0 候选池。

## 生产就绪前置条件

工作流此前只用本地夹具 Skill 执行器完成端到端验证。**2026-09-11 已完成一次实时试运行**：
经网页面板提交真实网易云公开歌单（1925 首，档位选前 30 首），由项目内 OpenAI 兼容执行器
（OpenAI 兼容执行器，当前生产模型为 `deepseek-v4.1-flash`）完成 Step 2 分析与 Step 3 候选研究，程序产出 29 个候选、10 首推荐；候选携带
84 条、推荐携带 30 条可核验公开来源（证据分级 A 级 1 首、B 级 9 首），结果已原子发布到网页面板。

仍需注意：**可信在线事实核验适配器尚未补齐**（当前证据 URL 由模型凭既有知识给出，
`evidence_audit` 仍为 `not_available`），因此结果保持 `publication_status: draft` 与
「画像覆盖不足」提示；不能把离线格式检查或 Skill 自报核验状态当成生产验收。
消息发送途径已从仓库移除，不存在任何外发通道。

## 测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
node --check tools/export_apple_playlist.mjs
node --check tools/apple_export_helpers.mjs
node --check web/server.js
# 已安装 tools 依赖和 Chromium 后：
npm --prefix tools test
npm --prefix tools run test:browser
# 已安装 web 依赖后：
npm --prefix web test
npm --prefix web run test:browser
```

当前 385 项 Python 测试不依赖 `input/` 或私有风格画像。Apple 读取工具另有 15 项 Node 单元测试与 1 项 Chromium 文件写入测试；网页层另有 13 项 Node 测试与 20 项 Chromium 交互测试。线上可达性以 2026-09-18 对当前公开歌单完成的 Apple 官方两页、123 首读取为准。

`web/tests/` 覆盖网页层回归：`server.test.mjs` 与 `workflow-job.test.mjs` 用隔离端口和临时配置启动独立 `server.js` 实例（`ATLAS_WEB_CONFIG`），验证静态服务、错误路径、真实任务生命周期、SSE 事件顺序与并发互斥；`workflow-ui.browser.mjs` 用 Playwright 注入可编程 `EventSource`，覆盖乱序、重复、丢帧事件、SSE 中断回退轮询与轮询去重，以及歌单读完后才解锁的档位选择（上限、快捷键、越界拦截、提交后继续与取消）；`atlas-fixture.browser.mjs` 用 `tests/fixtures/playlist_sample.json` 与夹具执行器真实运行 `web_workflow.py`，在隔离 runtime 发布后验证页面渲染 10 首推荐。测试不访问外部歌单内容，不依赖真实检索执行器。

`tests/test_analysis_agent.py` 新增 31 项测试，覆盖无目录研究、精确逐批覆盖/身份、快照和词表绑定、未知值、证据与程序字段边界、预算与总超时、失败报告/断点续跑、结果导入、输入保护、两阶段 Skill 到 10 首草稿及严格审计。真实 115 首快照另已准备 6 批分析任务；尚未执行真实分析 Skill，不将任务准备或夹具耗时当作推荐质量验收。

`tests/test_preference_quality.py`、`tests/test_staged_research.py`、`tests/test_listening_benchmark.py` 覆盖未知画像、精准覆盖、补全队列、虚假引用、多兴趣复现、研究补充/超时/预算、程序说明、试听标签和遥测摘要。独立 CLI 回归也验证 20/40 候选、1/2 轮研究、10 首输出、拒绝覆盖标签及缺标签时不下质量结论。

`tests/test_priority_fixes.py` 覆盖草稿输出、不可用证据阻断、上下文复用与硬预算、导出参数与超时、QQ 分页摘要和跨日复现。`tests/test_hardening.py`、`tests/test_policy_feedback.py` 覆盖排序信任边界、回溯可行性、数量清单、证据状态、命令引号、反馈与人工策略；`tests/test_pipeline_cli.py` 在临时目录通过独立进程运行完整夹具链路，并校验失败退出码、输出隔离与新增 CLI 参数。

回归覆盖包括：Step 1 数量契约、Step 2 确定性分析、风格画像覆盖、Schema 2 bundle 校验、确定性七维评分与能量弧排序、证据/说明契约、反馈输入契约（`tests/test_feedback.py`）、六类离线评估指标与调优建议（`tests/test_evaluation.py`）、证据出处与 A/B/C 等级规则（`tests/test_evidence.py`）、上下文预算截断与画像覆盖报告（`tests/test_robustness.py`）。

`apple_music_weekly.py` 仅保留兼容转发入口，实际执行入口是 `workflow.py`。

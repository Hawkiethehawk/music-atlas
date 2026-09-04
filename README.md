# Music Atlas

Music Atlas 是一个可扩展的每周音乐推荐工作流。它把歌单读取、音乐人关系分析和 Agent 推荐拆成三个独立步骤，并通过 JSON 契约连接每一步。

## 设计边界

1. Step 1 将 Apple Music、网易云或本地 JSON/CSV 歌单统一为 `PlaylistSnapshot`。
2. Step 2 只读取本次 `PlaylistSnapshot`，用确定性代码统计艺人分布，并解析主唱、前乐队和 side project 关系。
3. Step 3 只接收本次 `MusicianAnalysisPacket`，由 Agent 使用公开资料研究候选歌曲，并为每首歌曲生成推荐说明。

Step 3 使用混合音乐发现策略：Agent 只提交艺人延伸、音乐人关系、细分风格邻近和探索四类候选的结构化事实与逐项证据；固定代码计算风格、听感轴、关系、频率、新鲜度、证据质量和公开关联七项分数，执行硬性召回配额、去重、项目覆盖、MMR 多样性控制和能量弧线排序。当前不调用平台个性化接口；反馈契约与离线评估已就位，但行为反馈只用于评估与人工批准的调优建议，不参与候选发现或自动排序。

Apple Music 只用于歌单快照和最终跳转链接。Apple Music 或网易云的个性化推荐、登录状态和历史运行结果不参与候选发现、排序或说明生成。

## 快速开始

仓库不包含任何个人歌单或运行产物。使用公开测试夹具可以完整运行本地链路：

```bash
python workflow.py run \
  --input tests/fixtures/playlist_sample.json \
  --reader local_json \
  --platform apple_music \
  --playlist-id sample \
  --playlist-name '示例歌单' \
  --runtime-dir runtime/local-run
```

该命令执行 Step 1、Step 2 和 Step 3 上下文准备，不调用外部模型，也不发送消息。

使用仓库内的测试 Agent 验证 Step 3 合同：

```bash
python workflow.py agent \
  --analysis runtime/local-run/musician_analysis.json \
  --prompt runtime/local-run/agent_prompt.md \
  --output runtime/local-run/recommendation_bundle.json \
  --channel-output runtime/local-run/channel_text.txt \
  --command 'python tests/fixtures/fake_agent.py'
```

`--command` 指向的进程从标准输入读取 Agent prompt，并向标准输出写入 Schema 2.0 的候选池 `RecommendationBundle`。写入渠道文本前，程序会计算全部评分，执行硬性候选类型配额和歌单顺序，并校验推荐数量、项目覆盖、重复歌曲、逐首说明、关系引用和逐事实公开来源。

## 数量契约

歌曲数量始终来自本次 Step 1 输出：

```text
declared_track_count == track_count == len(tracks)
```

代码不固定某个歌曲总数。Step 2 不重新访问音乐平台；Step 3 不读取原始快照、登录 profile、历史推荐或上一轮 Agent 输出。

## 风格口径

`style_analysis.style_distribution` 是每首歌只有一个主风格的互斥审计分布；`style_analysis.overlap_style_distribution` 是逐曲多标签覆盖分布。一首歌可以命中多个细分风格，因此后者的覆盖率不要求合计 100%。

## Agent 提示词插槽

`prompts/` 下的 Markdown 文件是可编辑提示词空间，按固定顺序附加到 Agent prompt。策略占位符会由当前分析包渲染，其他空占位符会明确显示“无额外要求”。提示词不能改变 Step 1 数量、Step 2 统计、当前输入隔离、硬性召回配额、去重上限或证据契约。每次运行的提示词文件摘要会写入 Agent context manifest。

## 扩展点

- `source_adapters.py`：本地 JSON、Apple Music JSON、网易云 JSON 和 CSV 读取器，以及 `netease_public` 网易云公开歌单匿名读取器。
- 网易云公开歌单（匿名，不使用登录态）：

  ```bash
  python workflow.py snapshot --reader netease_public --platform netease \
    --playlist-id 3778678 --playlist-name '热歌榜' --output runtime/snapshot.json
  ```

  `--playlist-id` 接受数字 ID 或 `music.163.com` 分享链接（含 `#/playlist/...` 形式）；仅能读取公开歌单，隐私歌单（如“我喜欢的音乐”）无法匿名获取。响应缓存于 `input_sha256`，数量来自接口 `trackIds`，与实际解析数不一致时 `reader_status` 为 `incomplete`。
- `relations/artist_relations.json`：可审计的公开音乐人关系目录。
- `channels.py`：微信、飞书和 Telegram 的纯文本渲染适配器，只负责输出，不负责发送。
- `visualization_interface.py`：预留可视化后端接口，当前不包含实现。
- `hermes_weekly.sh`：服务器侧调度入口模板；个人输入和运行目录应在部署环境中单独配置。
- `feedback.py` + `evaluation.py` + `tune.py`：反馈输入契约、只读离线评估与人工批准的调优建议。
- `evidence.py`：来源类别验证适配器（离线确定性验证稳定标识符）、证据出处字段与 claim_type × source_class 的 A/B/C 等级规则；在线核验是预留扩展，默认不发请求。

## 输出文件

`workflow.py run` 只生成 `snapshot.json`、`musician_analysis.json`、`musician_analysis.md`、`agent_prompt.md` 和相关 manifest；随后执行 `workflow.py agent` 才生成已排序的 `recommendation_bundle.json` 和 `channel_text.txt`。可视化接口当前没有后端。这些目录默认被 Git 忽略。

如果已有本次运行的 Step 2/Step 3 文件，也可以只校验并生成渠道文本：

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

## 上下文预算与画像覆盖报告

`run` 与 `prepare-agent` 支持 `--context-budget <字符数>`：提示词超预算时只按插槽顺序确定性截断可编辑插槽（固定指令与 JSON 载荷绝不截断），并把字符数、估算 token、截断插槽与预算报告写入 `agent_context_manifest.json` 与 `pipeline_manifest.json`。

`analyze`/`run` 在画像目录回退到公共示例或存在未分类艺人（`profile_coverage.degraded`）时，会额外写出 `coverage_report.json` 警告工件，列出目录模式与未分类艺人清单。

## 历史 Schema 1 产物归档

当前代码只消费 Schema 2.0 工件。历史 Schema 1.0 的运行时目录（`runtime/` 下含 1.0 JSON 的目录）会被整体迁移到带时间戳的归档目录并写入 manifest，**不会删除**：

```bash
python workflow.py archive-schema1
```

归档后实时运行前必须重新执行 `run` 生成 Schema 2 工件。`runtime/` 与 `input/` 均被 Git 忽略，归档只影响本地目录。

Schema 1.0 的历史分析包和直接推荐包不能复用；修改后需要重新运行 Step 1/2 并生成 Schema 2.0 候选池。

## 生产就绪前置条件

当前工作流已经用本地夹具 Agent 完成端到端验证。部署到生产前还必须完成一次使用外部可验证证据的实时代理试运行（当前范围不包含实时代理执行、提交、推送或部署），确认 Agent 输出的候选证据可通过 `evidence_audit` 的来源类别与稳定标识符验证，再启用自动调度。

## 测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

回归覆盖包括：Step 1 数量契约、Step 2 确定性分析、风格画像覆盖、Schema 2 bundle 校验、确定性七维评分与能量弧排序、证据/说明契约、反馈输入契约（`tests/test_feedback.py`）、六类离线评估指标与调优建议（`tests/test_evaluation.py`）、证据出处与 A/B/C 等级规则（`tests/test_evidence.py`）、上下文预算截断与画像覆盖报告（`tests/test_robustness.py`）。

`apple_music_weekly.py` 仅保留兼容转发入口，实际执行入口是 `workflow.py`。

# Changelog

## 2026-09-04

补齐 P1 剩余工作：反馈输入契约与只读离线评估、证据出处与等级规则、运营稳健性。

- 新增 `feedback.py`：saved/skipped/replayed/hidden 反馈输入契约（时间戳、analysis_id、recommendation_id），日志校验与日志文件读写。
- 新增 `evaluation.py`：六类只读离线评估指标（precision、novelty、diversity、calibration、artist/project repetition、sequence quality），报告标记 `policy_changed: false`。
- 新增 `tune.py`：人工批准的策略调优建议（`approval_required: true`、`auto_applied: false`）；权重、召回组合、上限与排序权重不会从未审阅反馈中自我调整。
- 新增 `evidence.py`：来源类别验证适配器（离线确定性验证 MusicBrainz UUID、Wikidata QID、YouTube/Spotify ID 等稳定标识符），出处字段（retrieved_at、source_identifier、verification_result），以及 claim_type × source_class 的 A/B/C 接受规则；在线核验仅保留扩展开关，默认不发请求。
- `contracts.py`：证据项可选出处字段校验、反馈记录与反馈日志校验。
- `agent_prompt.py` + `agent_runner.py` + `workflow.py`：prompt 字符数/估算 token 遥测；`--context-budget` 确定性截断可编辑插槽并生成预算报告，写入 context/pipeline manifest。
- `musician_analyzer.py` + `workflow.py`：画像目录回退或存在未分类艺人时输出 `coverage_report.json` 警告工件。
- `workflow.py`：新增 `evaluate`、`tune`、`archive-schema1` 子命令；`validate` 支持 `--evidence-audit` 输出。
- 归档历史 Schema 1 运行时产物到 `runtime/archive/schema1-*`（不删除）；实时运行前需重新 `run` 生成 Schema 2 工件。
- README 补充反馈/评估、证据审计、预算、覆盖报告、归档与生产就绪前置条件（实时代理试运行不在当前范围）。

验证：

- `python -m unittest discover -s tests -v`：42 个测试通过（新增 feedback/evaluation/evidence/robustness 四组）。
- `python -m compileall -q .`：通过。
- 使用仓库测试夹具完成 run -> agent(fake) -> evaluate -> tune -> evidence-audit 本地链路验证。

同日审查修复：

- `contracts.py`：配额计算 `recall_mix_ratios`/`target_counts` 自 `recommender.py` 迁入，消除 contracts ↔ recommender 循环导入；`musician_analyzer.py` 改为顶部导入 `validate_analysis_packet`。
- `musician_analyzer.py`：显式指定示例画像目录时 `profile_catalog_mode` 判定为 `example_fallback` 并标记 `degraded`，不再误报 `explicit`。
- `evaluation.py`：反馈时间戳解析为 ISO 时间后比较（兼容 `Z`/偏移量、naive 按 UTC），不可解析记录排序靠后，两条均不可解析时退回字符串比较。
- `evidence.py`：删除 `classify_source` 中不可达的 bandcamp 分支。
- 删除遗留的 `auth_server.py`：其依赖的 MusicKit 符号已随旧实现移除，导入损坏且文件本被 Git 忽略。
- 测试 42 -> 46：新增混合时区反馈取最新、不可解析时间戳字符串回退、可解析优先、显式示例目录降级四组回归。

审查修复后验证：

- `python -m unittest discover -s tests -v`：46 个测试通过。
- `python -m compileall -q .`：通过。
- 夹具链路 run -> agent(fake) -> validate(+evidence-audit) -> evaluate 复跑通过，证据审计 10/10 接受。

维护标签：`patch-20260904-152631`

## 2026-09-02

首次公开发布 Music Atlas 本地音乐推荐工作流。

- 提供动态 `PlaylistSnapshot` 数量契约，数量来自当前 Step 1 输入。
- 提供确定性音乐人分布、主唱、前乐队和 side project 分析。
- 提供隔离的 Step 3 Agent prompt、`RecommendationBundle` 校验和逐首推荐说明契约。
- 提供 Apple Music JSON、网易云 JSON、CSV 和本地 JSON 读取扩展点，以及微信、飞书和 Telegram 文本渲染适配器。
- 不提交个人歌单、登录态、运行产物或服务器配置。

验证：

- `python -m unittest discover -s tests -v`：5 个测试通过。
- `python -m compileall -q .`：通过。
- 使用仓库测试夹具完成 Step 1 -> Step 2 -> Agent -> 渠道文本的本地链路验证。

维护标签：`patch-20260902-170640`

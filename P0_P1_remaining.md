# Atlas P0/P1 Remaining Work

> 实施状态：以下 P1 工作项已于 2026-09-04 落地，见 CHANGELOG 2026-09-04 条目与
> `feedback.py` / `evaluation.py` / `tune.py` / `evidence.py` 及 `workflow.py` 新子命令。
> 范围边界（实时代理执行、提交、推送、部署）保持未纳入。

> 2026-09-06 审查修复：排序包重算、约束回溯、CSV 数量清单、反馈统一解释、人工策略入口与命令引号处理已补齐，104 项测试通过。证据能力边界已更正：只有离线格式检查，可信在线事实核验适配器仍待实现，不能将历史“接受”计数视为事实核验。

> 同日四项优先优化已逐项实现并验收：不可用证据阻断与草稿输出、准备上下文复用与硬预算、Apple 下载/CSV/独立数量核对、QQ 全分页摘要与评分参考日期。最终 123 项 Python、11 项 Node、1 项 Chromium 本地下载测试通过。旧分析/上下文/排序工件需重建，见 README；在线事实核验与 TuneMyMusic 线上复测仍未完成。

## 偏好与推荐质量改进（2026-09-05，本地未发布）

五项已实现：未知值与覆盖门槛/补全队列、当前目录绑定的关系路线、多兴趣画像、有界分轮候选研究与入选后程序说明、人工试听方案对照。148 项 Python 测试通过，20/40 候选与两轮补充均有独立 CLI 回归；没有新增 Python 依赖，没有自动学习。

## 分析 Agent 重构（2026-09-06，本地未发布）

已将逐曲音乐研究与关系研究交给分析 Agent，程序继续独占统计、兴趣聚合和评分。默认 run/analyze 不读预置画像，支持研究准备、命令执行、结果导入与绑定当前快照的完整研究包；旧路径通过显式 catalog 保留。补齐分批、硬字符预算、总超时、断点续跑、失败诊断与输入覆盖保护。179 项 Python、11 项 Node、1 项 Chromium 回归通过。

用户的 115 首 Apple Music 快照已准备为 6 批分析任务，尚未执行真实 Agent；不存在合成画像或假推荐。无需先维护私有艺人目录，但仍需有效公开研究结果才能生成分析并进入推荐。

仍需实际完成的工作：

- 为分析 Agent 配置真实公开检索执行器，完成已准备的 115 首研究任务；未知曲目保留缺口，覆盖门槛不等于画像正确性验收。维护私有目录不再是默认前置条件。
- 接入可信的在线事实核验通道。目录端点匹配、来源域名与格式检查不能证明候选歌曲、版本或关系属实。
- 使用真实试听标签，在同输入、同评分日期和相同研究预算下做对照。夹具只能证明功能与边界，尚未测得喜欢率提升或真实 Agent 时间/费用下降。
- 实时分析/候选 Agent 试运行仍未执行。2026-09-06 用户歌单已通过 TuneMyMusic 实际导出 115 首；这不代表所有歌单和当前页面状态的全面兼容性验收。部署需单独授权。
- 当前字符上限约束研究 prompt 和已捕获的 JSON；外部进程 stdout 仍先完整捕获，内存硬限、真实计费 token/费用统计与外部 Agent 沙箱属于后续执行器加固，不应声称已经实现。

## P1: Measurement And Feedback

- ~~Add an explicit recommendation-feedback input contract: saved, skipped, replayed, and hidden outcomes; timestamp; recommendation and analysis identifiers.~~ 已实现：`contracts.validate_feedback_record/log`、`feedback.py`，outcome 含 saved/skipped/replayed/hidden，字段含 timestamp、analysis_id、recommendation_id、可选 bundle_id/position。
- ~~Build offline evaluation metrics for precision, novelty, diversity, calibration, artist/project repetition, and sequence quality.~~ 已实现：`evaluation.evaluate_offline` 输出六组指标，报告 `policy_changed: false`。校准项仅保留分箱描述，`expected_calibration_error: null` / `not_calibrated`，规则分数不得解释为概率。
- ~~Add a repeatable evaluation fixture and a command that compares a candidate/ranked bundle against recorded feedback without changing ranking policy automatically.~~ 已实现：`workflow.py evaluate`、`tests/fixtures/feedback_sample.json` 与 `tests/test_feedback.py` / `tests/test_evaluation.py`；排序策略不被评估修改。
- ~~Define a human-approved policy-tuning loop. Ranking weights, recall mix, caps, and sequencing weights should not self-adjust from unreviewed feedback.~~ 已实现：`tune.propose_tuning` 只产出 `approval_required: true`、`auto_applied: false` 的建议工件；应用必须由人工编辑策略 JSON 后重新 analyze。

## P1: Evidence Quality

- Replace URL/domain syntax checks with trusted source-specific verification adapters. **尚未完成在线事实核验**；已实现来源分类与稳定标识符格式检查，并将其与来源等级、事实核验状态分开报告。Agent 自报 `verified` 不被采信。
- ~~Add source provenance fields such as retrieval time, source identifier, and verification result.~~ 已实现：`contracts` 支持可选 `retrieved_at` / `source_identifier` / `verification_result`；`evidence.verify_evidence_item` 回填 source_class 与 verification_result。
- ~~Add tests for stale, contradictory, inaccessible, and duplicated evidence.~~ 已实现：负面判定、过期、重复、无时区及非法日期、自报 verified 不采信、审计失败不写成功产物；见 `tests/test_evidence.py` 与 `tests/test_hardening.py`。
- ~~Define acceptance rules for evidence grades A/B/C per claim type and source class.~~ 已实现：`evidence.GRADE_RULES`（claim_type × source_class → 等级）与 `check_evidence_acceptance`（声明不得超过来源支持的最高等级）；`workflow.py validate --evidence-audit` 输出逐首审计。

## P1: Operational Robustness

- ~~Add an explicit warning/report artifact when the style profile catalog falls back to the public example catalog or has unclassified artists.~~ 已实现：`musician_analyzer.write_coverage_report`；`analyze`/`run` 在 degraded 时写出 `coverage_report.json`。
- ~~Add prompt-size telemetry and a configurable context budget with deterministic truncation/reporting.~~ 已实现：`agent_prompt.prompt_size_telemetry` / `apply_context_budget`；`run`/`prepare-agent`/`agent` 支持正整数字符硬预算，固定指令与载荷仍超预算时拒绝调用 Agent。准备 manifest 保存配置与摘要，执行阶段原样复用；自定义清单通过 `--manifest` 配对。
- ~~Migrate or archive historical Schema 1 runtime artifacts; current runtime artifacts must be regenerated before a live Schema 2 run.~~ 已实现：`workflow.py archive-schema1` 把含 1.0 JSON 的运行时目录迁至 `runtime/archive/schema1-*`（不删除）；README 已注明实时运行前需重新 `run`。
- Run a live-agent dry run with externally verifiable evidence before treating the workflow as production-ready. The completed end-to-end verification so far uses the local fixture agent only. 未纳入：属于范围边界内的实时代理执行，README 已列为生产就绪前置条件；本地夹具链路验证完成。

## Scope Boundary

The current change set implements Schema 2 staging, Agent-based analysis research, deterministic aggregation and seven-factor scoring, hard recall quotas and diversity caps, energy-arc sequencing, evidence/explanation contracts, profile coverage metadata, bounded prompts, resumable research, and regression coverage. No feedback-driven learning, trusted online verification adapter, live-model execution, commit, push, or deployment is included.

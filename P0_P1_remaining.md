# Atlas P0/P1 Remaining Work

> 实施状态：以下 P1 工作项已于 2026-09-04 落地，见 CHANGELOG 2026-09-04 条目与
> `feedback.py` / `evaluation.py` / `tune.py` / `evidence.py` 及 `workflow.py` 新子命令。
> 范围边界（实时代理执行、提交、推送、部署）保持未纳入。

## P1: Measurement And Feedback

- ~~Add an explicit recommendation-feedback input contract: saved, skipped, replayed, and hidden outcomes; timestamp; recommendation and analysis identifiers.~~ 已实现：`contracts.validate_feedback_record/log`、`feedback.py`，outcome 含 saved/skipped/replayed/hidden，字段含 timestamp、analysis_id、recommendation_id、可选 bundle_id/position。
- ~~Build offline evaluation metrics for precision, novelty, diversity, calibration, artist/project repetition, and sequence quality.~~ 已实现：`evaluation.evaluate_offline` 输出六组指标，报告 `policy_changed: false`。
- ~~Add a repeatable evaluation fixture and a command that compares a candidate/ranked bundle against recorded feedback without changing ranking policy automatically.~~ 已实现：`workflow.py evaluate`、`tests/fixtures/feedback_sample.json` 与 `tests/test_feedback.py` / `tests/test_evaluation.py`；排序策略不被评估修改。
- ~~Define a human-approved policy-tuning loop. Ranking weights, recall mix, caps, and sequencing weights should not self-adjust from unreviewed feedback.~~ 已实现：`tune.propose_tuning` 只产出 `approval_required: true`、`auto_applied: false` 的建议工件；应用必须由人工编辑策略 JSON 后重新 analyze。

## P1: Evidence Quality

- ~~Replace URL/domain syntax checks with source-specific verification adapters where APIs or stable public identifiers are available.~~ 已实现（离线确定性部分）：`evidence.classify_source` 与 `extract_source_identifier` 识别 MusicBrainz UUID、Wikidata QID、YouTube/Spotify ID 等稳定标识符；在线核验仅作为 `verification_result` 显式来源的扩展保留，默认不发请求。
- ~~Add source provenance fields such as retrieval time, source identifier, and verification result.~~ 已实现：`contracts` 支持可选 `retrieved_at` / `source_identifier` / `verification_result`；`evidence.verify_evidence_item` 回填 source_class 与 verification_result。
- ~~Add tests for stale, contradictory, inaccessible, and duplicated evidence.~~ 已实现：`tests/test_evidence.py` 覆盖 stale（检索时间过旧）、duplicated（重复 URL）、contradictory / inaccessible（外部显式判定保留）、unverified / verified 分类。
- ~~Define acceptance rules for evidence grades A/B/C per claim type and source class.~~ 已实现：`evidence.GRADE_RULES`（claim_type × source_class → 等级）与 `check_evidence_acceptance`（声明不得超过来源支持的最高等级）；`workflow.py validate --evidence-audit` 输出逐首审计。

## P1: Operational Robustness

- ~~Add an explicit warning/report artifact when the style profile catalog falls back to the public example catalog or has unclassified artists.~~ 已实现：`musician_analyzer.write_coverage_report`；`analyze`/`run` 在 degraded 时写出 `coverage_report.json`。
- ~~Add prompt-size telemetry and a configurable context budget with deterministic truncation/reporting.~~ 已实现：`agent_prompt.prompt_size_telemetry` / `apply_context_budget`；`run`/`prepare-agent`/`agent` 支持 `--context-budget`，预算报告写入 context/pipeline manifest。
- ~~Migrate or archive historical Schema 1 runtime artifacts; current runtime artifacts must be regenerated before a live Schema 2 run.~~ 已实现：`workflow.py archive-schema1` 把含 1.0 JSON 的运行时目录迁至 `runtime/archive/schema1-*`（不删除）；README 已注明实时运行前需重新 `run`。
- Run a live-agent dry run with externally verifiable evidence before treating the workflow as production-ready. The completed end-to-end verification so far uses the local fixture agent only. 未纳入：属于范围边界内的实时代理执行，README 已列为生产就绪前置条件；本地夹具链路验证完成。

## Scope Boundary

The current change set implements the P0/P1 minimum: Schema 2 staging, deterministic seven-factor scoring, hard recall quotas and diversity caps, energy-arc sequencing, evidence/explanation contracts, profile coverage metadata, compact prompts, and regression coverage. No feedback-driven learning, source retrieval adapters, live-agent execution, commit, push, or deployment is included.

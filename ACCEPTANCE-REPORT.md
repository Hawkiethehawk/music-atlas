# Music Atlas 验收汇报（第二轮：真实链路与故障注入）

验收日期：2026-09-11
基线代码：`fe9ac12`（feat: 完成 Atlas Skill 工作流与 GitHub 迁移）+ 同一工作区内尚未提交的网页工作流实现（`web/`、`web_workflow.py`、`web_view_model.py`、`config/`、`executors/` 等）
目标服务：隔离实例（随机端口，`ATLAS_WEB_CONFIG` 临时配置）；生产服务 `http://127.0.0.1:8420` 未用于测试
结论：验收范围内通过；本轮同时固化了可重复执行的 Playwright/Node 回归套件。仍存在明确的未覆盖范围，见文末清单。

## 与上一轮报告的差异

上一轮（2026-09-10）运行态页面仅用模拟事件验收，未验证真实链路、SSE 故障注入与事件健壮性，且无可重复的测试资产。本轮逐项补齐：

| 上轮缺口 | 本轮状态 |
| --- | --- |
| 运行态只用模拟事件验证 | 已用夹具歌单 + 夹具执行器真实运行 `web_workflow.py`，产物经隔离服务器渲染验证 |
| SSE 中断、服务重启、轮询去重未注入验证 | 已注入验证（前端回退轮询、去重、SSE 恢复；服务器侧 SSE 头、断线重放） |
| 乱序、重复、丢帧事件未测试 | 已注入验证（状态按最大 seq 收敛、DOM 幂等、计数不回退） |
| `web/package.json` 无测试命令、无可重复 E2E | 新增 `npm --prefix web test` / `test:browser`，18 项回归全部通过 |

## 本轮新增验收基础设施

- `web/server.js`：支持 `ATLAS_WEB_CONFIG` 环境变量指向临时配置，测试以随机空闲端口和隔离 runtime 目录启动独立实例；默认行为（读取 `config/web.json`）不变。
- `web/package.json`：新增 `test`（Node 协议层）与 `test:browser`（Playwright）命令；`playwright@1.62.1` 与 `tools/` 同版本，复用已下载的 Chromium。
- `web/tests/`（5 个文件）：
  - `helpers.mjs`：隔离服务器启动、SSE 流解析、夹具工作流运行工具；
  - `server.test.mjs`：静态服务、`/api/config`、`/api/health`（含数据发布前后 503→200）、非法提交 400、执行器未配置 503、未知任务 404；
  - `workflow-job.test.mjs`：真实子进程任务生命周期——SSE 头与初始快照、`queued → started → task_started → failed` 事件顺序、seq 严格递增、`exit_code=2` 与 stderr 诊断、断线后重连收到完整终态快照、活动任务期间再次提交 409 互斥；
  - `workflow-ui.browser.mjs`：注入可编程 `EventSource` + 路由拦截，覆盖正常四阶段序列（5+4 槽位、批次计数、事件上限 12 条）、乱序事件（任务状态与计数按最大 seq 收敛）、重复事件（相同 seq 只渲染一次）、丢帧事件（seq 跳号收敛）、SSE 中断回退轮询且轮询去重、轮询失败按退避重试、完成态隐藏详情并幂等刷新 Atlas、失败态提示不刷新数据；
  - `atlas-fixture.browser.mjs`：真实运行夹具工作流并在隔离服务器上验证 `/api/health`、`/api/atlas` 与页面渲染。

## 真实小规模网页工作流（本轮核心证据）

命令（`web/tests/helpers.mjs` 的 `runFixtureWorkflow` 自动执行）：

```bash
python web_workflow.py \
  --runtime-dir runtime/web-e2e-atlas-fixture-*/job \
  --current-data runtime/web-e2e-atlas-fixture-*/current.json \
  --source-kind local_json \
  --input tests/fixtures/playlist_sample.json \
  --platform apple_music --playlist-id sample --playlist-name 示例歌单 \
  --analysis-command "python tests/fixtures/fake_analysis_agent.py" \
  --recommendation-command "python tests/fixtures/fake_agent.py" \
  --analysis-batch-size 2
```

实际验证：

- 工作流退出码 0，事件流共 31 条，按 `snapshot → analysis → recommendation → export` 四阶段推进；`web_job_report.json` 状态 `completed`，`analysis_parallelism=5`、`recommendation_parallelism=4`。
- 快照 3 首（`snapshot_id: apple_music-ac99e9b5db51a1bd`），分析 2 批全部 `validated`，聚合校验通过后进入推荐，4 个并行 worker 返回，最终推荐数量固定 10 首。
- 页面（`#/discover`）渲染真实产物：导语"基于 3 首完整歌单快照"、10 条推荐列表、四类召回路径（艺人延伸/音乐人关系/风格邻近/探索）与"研究草稿"边界提示；浏览器控制台无错误。
- 夹具为合成数据（`TEST_ONLY` 标识），仅证明链路与契约，不代表真实音乐事实或推荐质量。

## 验收证据索引

- 测试脚本：`web/tests/`（上节 5 个文件）。
- 夹具运行产物：`runtime/web-e2e-atlas-fixture-20260911015903-30aa4b/`
  - `workflow-events.ndjson`：31 行真实事件流样本（started/task_started/completed 逐条含 `at` 与递增 `seq`）；
  - `job/web_job_report.json`：任务报告；`job/snapshot.json`、`job/recommendation_bundle.json`：阶段产物；
  - `screenshots/discover.png`：真实产物渲染截图。
- 隔离服务器配置样本：`runtime/web-e2e-atlas-fixture-20260911015904-6a9c45/web.config.json`。
- 失败任务事件样本：`web/tests/workflow-job.test.mjs` 运行时产生的 job 目录（含 `stderr_tail` 与 `exit_code=2` 的完整失败事件流）。

## 实际验证命令与结果

```bash
node --test tests/*.test.mjs          # 7 项通过（server + workflow-job）
node --test tests/*.browser.mjs       # 11 项通过（workflow-ui 8 项 + atlas-fixture 3 项）
python -m unittest discover -s tests  # 203 项通过
python -m compileall -q .             # 通过
node --check web/server.js            # 通过
node --check tools/export_apple_playlist.mjs
node --check tools/apple_export_helpers.mjs
```

README 测试章节已同步 `npm --prefix web test` / `test:browser`；`.gitignore` 已补充 `web/node_modules/`。

## 未覆盖范围（明确保留的缺口）

1. **真实检索执行器**：本轮夹具链路的分析/推荐 Skill 仍为合成执行器；`P0_P1_remaining.md` 中已准备的 115 首、6 批真实研究任务仍未执行。真实执行的耗时、失败率、覆盖缺口与费用未测得。
2. **可信在线事实核验**：证据侧仍只有离线格式检查；"尚未在线核验"的页面提示如实反映该边界。
3. **真实 token/费用统计与外部执行器沙箱**：未实现。
4. **真实试听对照**：benchmark 标签仍为空，未测得推荐质量提升。
5. **生产 8420 服务与部署**：本轮全部在隔离实例完成，未触碰生产配置、未部署。
6. **多浏览器/移动端视口/无障碍**：Playwright 仅覆盖默认 Chromium 视口；服务重启后的前端恢复仅以 SSE 永久断开 + 轮询失败重试间接覆盖，未做真实进程重启演练。

## 结论

验收范围内的项目全部通过，且验收本身已固化为可重复执行的回归套件。项目是否生产就绪仍取决于未覆盖范围第 1、2 项（真实 Skill 执行与可信事实核验），不应依据本报告作出生产就绪判断。

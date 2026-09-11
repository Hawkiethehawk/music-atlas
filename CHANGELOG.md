# Changelog

## 2026-09-11（规模分档品味摘要与 atlas CLI，维护标签 patch-20260911-153225）

- 新增规模分档分析：≤30 首逐曲研究（每批默认 10 首，批次并发），31-500 首品味摘要（taste_summary），≥501 首歌手摘要（artist_summary）；后两者为单任务分析，基于程序统计的歌名/歌手清单与整体锐评，不再逐曲研究。
- 新增 `taste_summary.py` 与两份 prompt 模板：程序预计算统计（唯一曲目、重复、歌手计数），分析 Agent 按三层方法论（乐派知识 / 歌名语义与艺人聚类 / 重复频率）返回结构化品味画像与锐评文案；艺人、歌名、风格引用逐字绑定清单与风格本体，幽默推演强制 speculation 标注，禁止推荐/评分/策略字段。
- 契约扩展：`taste_summary_result` 校验（analysis_contracts.py）、`profile_catalog_mode` 新增 `taste_summary`、候选池召回覆盖按策略派生类型校验；品味模式下程序独占调整策略副本——召回配额三类化（剔除 musician_relation）与覆盖门槛降为 30%（用户批准，真实验证覆盖率约 43-44%，50% 的逐曲门槛对摘要模式过严）。
- 只有带可检索来源的场景归属才驱动风格分配，无来源归属仅展示且艺人保持 unclassified；分析包保持 `packet_type: musician_analysis` 兼容，逐曲分配标记 `origin: taste_summary`。
- 新增 `atlas` 统一 CLI（`atlas.py` + `web_service.py` + `atlas.bat`）：`atlas start|stop|restart|status|logs` 管理网页面板，防护模型移植自 nocap console-service——`/api/health` 携带 service/pid/web_root 身份，只有本项目面板会被复用或停止；端口被其他服务占用默认拒绝（--force 才替换）；状态文件 PID 存活但健康不可用时拒绝覆盖。其余子命令透传 workflow.py。
- 网页面板适配：发现页新增品味锐评模块（含幽默推演标注与草稿边界提示）；工作流面板对品味摘要单任务模式的槽位与统计展示；server.js 健康端点扩展身份字段并支持 ATLAS_WEB_PORT 覆盖。
- 真实验证（codex-cli 0.153.4，--sandbox read-only，未联网）：100 首品味摘要 55.6 秒通过全部契约校验（9 风格标签、20 聚类、4 语义主题、覆盖 43%）；1923 首歌手摘要通过校验（55 聚类、覆盖 846/1923）；修复真实数据暴露的同名艺人变体重复 entity_ref 问题（按 artist_key 归并计数）。验证产物在 runtime/taste-validate-20260911（Git 忽略）。
- 修复 fake_agent 夹具在无关系目录时跳过 musician_relation 候选而非退出。

实际验证：

- `python -m unittest discover -s tests`：227 项通过（含 16 项品味摘要、8 项服务管理测试）。
- `python -m compileall -q .`：通过。
- `node --check web/server.js`：通过。
- `node --test tests/*.test.mjs`（web/）：7 项通过；`node --test tests/*.browser.mjs`（web/）：11 项通过。
- 真实 8420 冒烟：`atlas start --no-open` → 重复 start 复用（同 PID）→ `atlas stop` → `atlas status` 全链路通过。
- 未部署；真实检索执行器与在线事实核验仍未完成。

## 2026-09-11（网页端工作流与验收基础设施，维护标签 patch-20260911-100253）

- 新增网页端受控工作流：`web_workflow.py` 将整理、分析、推荐、发布四阶段通过 JSON 事件流提供给 `web/server.js`；浏览器只提交公开歌单链接，Skill 执行器由服务端配置；`web_view_model.py` 生成只读脱敏发布数据，`config/web.json` 固定端口、发布路径与并行度；`executors/` 提供项目内 Codex 执行器入口。
- `web/` 新增 Editorial Atlas 页面与零依赖静态服务：四阶段进度、5+4 并行槽位、SSE 实时更新与轮询回退、最近事件北京时间展示、任务结束后隐藏详情并刷新数据。
- 新增网页层回归套件 `web/tests/`：`server.test.mjs`/`workflow-job.test.mjs` 用 `ATLAS_WEB_CONFIG` 隔离实例验证静态服务、错误路径、真实任务生命周期与 SSE 事件顺序；`workflow-ui.browser.mjs` 用 Playwright 注入可编程 `EventSource`，覆盖乱序、重复、丢帧事件、SSE 中断回退轮询与轮询去重；`atlas-fixture.browser.mjs` 用夹具歌单与夹具执行器真实运行 `web_workflow.py` 并验证页面渲染 10 首推荐。`web/package.json` 新增 `test`/`test:browser` 命令；`.gitignore` 补充 `web/node_modules/`。
- 重写 `ACCEPTANCE-REPORT.md`：记录基线 commit、验收命令、脚本路径、事件样本与截图索引，并明确未覆盖范围（真实检索执行器、在线事实核验、费用统计、真实试听对照、生产部署）。
- 同步 README 测试章节；`design-qa.md` 保留设计验收记录。

实际验证：

- `node --test tests/*.test.mjs`（web/）：7 项通过。
- `node --test tests/*.browser.mjs`（web/）：11 项通过。
- `python -m unittest discover -s tests`：203 项通过。
- `python -m compileall -q .`：通过。
- `node --check web/server.js`、`node --check tools/export_apple_playlist.mjs`、`node --check tools/apple_export_helpers.mjs`：通过。
- 夹具端到端：`web_workflow.py` 以 `tests/fixtures/playlist_sample.json` 与夹具执行器真实运行成功，退出码 0，31 条事件按四阶段推进，推荐数量固定 10 首；产物与事件样本在 `runtime/web-e2e-atlas-fixture-20260911015903-30aa4b/`（Git 忽略）。
- 真实检索执行器、在线事实核验、真实费用统计与试听对照仍未完成；未部署。

## 2026-09-09（迁移 GitHub，维护标签 patch-20260909-160756）

- 将 Atlas 独立仓库当前工作流变更迁移到 GitHub `Hawkiethehawk/music-atlas`；GitHub 作为 `origin`，原 Gitee 远端保留为 `gitee-archive`。
- 完成通用分析/推荐 Skill 链路、研究上下文与确定性排序边界，补充微信 OpenClaw/Pi wechatbot 交付入口和微信分段文本渲染。
- 更新工作流文档、契约和测试；移除旧的 Hermes 入口脚本，保留运行产物、个人输入和凭据不进入仓库。

实际验证：

- `python -m unittest discover -s tests -v`：191 项通过。
- `python -m compileall -q .`：通过。
- `git diff --check`：通过。
- 本地夹具端到端链路：`run` 准备分析任务，`analyze` 完成 3 首测试快照的 Skill 研究汇总，`skill` 生成 10 首研究草稿并完成确定性排序；测试数据不代表真实音乐事实。

## 2026-09-07（同步 Gitee 最新提交，维护标签 patch-20260907-163229）

- 合并 Gitee origin/master 提交 9701810，纳入 research-agent 工作流重构；按确认保留本地 AGENTS.md 版本。
- 保留本地文档提交 276d524，并完成合并提交 4fd7de7。

实际验证：

- python -X utf8 -m unittest discover -s tests -v：179 项通过。
- python -X utf8 -m compileall -q .：通过。
- npm ci：通过。
- node --check export_apple_playlist.mjs、node --check apple_export_helpers.mjs：通过。
- npm test：11 项通过；npm run test:browser：1 项通过。
- git diff --check：通过。

## 2026-09-07（推送 Gitee，维护标签 patch-20260907-161134）

将前述本地未发布的工作流重构与优先优化统一提交到 `master`，保持个人输入、运行产物、凭据和设计草稿不进入仓库。

- 偏好分析改为默认由隔离的分析 Agent 研究逐曲画像、风格八轴、范围、置信度、证据和音乐人关系；程序独占统计、兴趣分组、召回配额、评分、多样性和排序。
- 推荐生成增加候选阶段、关系路径校验、分阶段研究、确定性约束选曲、证据审计、渠道草稿状态、反馈离线评估与人工批准调优边界。
- 完善快照/词表绑定、分批研究、预算继承、断点续跑、失败诊断、CSV 数量与下载保护、固定参考日期和旧目录兼容。

实际验证：

- `python -X utf8 -m unittest discover -s tests -v`：179 项通过。
- `python -X utf8 -m compileall -q .`：通过。
- 两个 Node 模块语法检查：通过；`npm test`：11 项通过；`npm run test:browser`：1 项通过。
- `git diff --check`：通过。当前环境未安装 Bash，因此未重复执行 `bash -n`；此前对应脚本检查已记录为通过。

## 2026-09-06（分析 Agent 工作流重构，本地未发布）

根据用户要求，将偏好分析中的音乐研究交给 Agent，不再要求先手工补齐私有艺人画像。保留既有未提交改动、10 首草稿、程序独占统计/评分及反馈不自动学习边界。

- 新增 analysis_contracts.py / analysis_agent.py：按艺人聚集曲目、默认每批 20 首，准备独立研究 prompt；接收逐曲风格混合、八轴描述、scope/confidence、主唱与关联项目和逐项公开证据。程序拒绝 Agent 的计数/评分/策略、漏曲/重复/替换、错批和跨快照研究包。
- 研究包绑定完整快照及词表摘要，提示词/配置保存 manifest；默认每批 100000 字符、各批共享 600 秒执行预算。逐批校验落盘、复用完成结果、导入手工完成的 JSON，失败记录具体批次和耗时；续跑继承准备配置，篡改/预算冲突拒绝执行，失败保留旧产物。
- musician_analyzer.py 编译研究为逐曲画像、艺人摘要、关系与原有分析包，再确定性生成统计、多兴趣分组和覆盖率。归一化艺人名称变体时保留原始曲目标识，避免研究与分析身份错配。Agent 模式不读本地画像、关系或偏好名单，旧目录路径显式 --analysis-mode catalog 保留，Python 分析 API 不带 research_bundle_path 时兼容旧行为。
- run/analyze 默认 agent，无命令时只准备研究并返回 analysis_agent_required；分析命令、批次导入和完整研究包三者互斥。输出跟随分析 JSON 目录并保护输入/研究文件；已绑定研究的快照不能被重复 run 覆盖，使用 analyze 续跑。
- 所有 Agent 事实保持待独立核验，关系标记 researched，正面 verified 声明不被采信；画像/关系来源须与证据一致，未来检索时间、已知不可用/过期/无效标识符和个性化证据会被拒绝。覆盖不足仍阻断推荐，不自动放宽门槛。
- Step 3 消费当前分析包的研究关系和带来源的单曲/发行差异，程序说明区分 Agent 研究路径、艺人路径和目录路径。同步 README、atlas、AGENTS、提示词和待办；服务器脚本透传 CLI 参数，不自动启用真实模型。

实际验收：

- 新增 31 项分析 Agent 测试，最终 179 项 Python 测试全部通过；覆盖准备/导入/完整双 Agent 夹具、总超时、断点续跑、越界字段、证据状态、快照与批次绑定、艺人名称变体、输入保护和旧目录兼容。compileall、两个 Node 语法检查、11 项 Node 测试、1 项 Chromium 本地下载测试和服务器脚本 bash -n 通过。
- 独立 CLI 目录 runtime/analysis-agent-acceptance-20260906-a49b1f：3 首输入经过 2 批 fake 分析 Agent，再进入 fake 候选 Agent，20 首候选最终选出 10 首草稿；validate 重算逐字节一致。严格证据审计为 0 首核验通过、10 首待核验，退出 2 且不写成功审计产物。夹具合成证据不代表音乐事实，也不代表质量或真实费用收益。
- 真实快照来自本次对话此前已完成的 Apple Music 115 首导出，115 个非空唯一平台 ID。此次没有重抓歌单，在 runtime/apple-agent-analysis-20260906-7c83e1 准备 6 批研究任务（20/20/20/20/20/15），位置恰好覆盖 115 首，总 prompt 102104 字符，单批未超预算。没有研究 result 文件，没有使用 fake Agent 填充真实歌单。
- 真实分析/候选模型执行、可信在线事实核验、同预算试听比较仍待完成；外部进程输出的流式内存硬限和实际 token/费用统计尚未实现。未发送消息、提交、打标签、推送或部署。

## 2026-09-05（偏好分析与推荐生成增强，本地未发布）

本轮从已有 123 项 Python 测试和未提交修改继续，逐项完善五个功能，保持 JSON 阶段隔离、程序确定性评分、10 首生产输出和研究草稿边界。

- 画像质量：未知听感使用 null，阻止“未知即安静”的误匹配；曲目覆盖优先于专辑覆盖，双匹配条件必须同时满足，保留字段级作用范围、置信度与来源。新增默认 50% 曲目分类门槛与按影响曲目数排序的补全清单，低覆盖保留分析但不调用 Agent。
- 关系召回：新增 candidate_routes.py，将候选艺人绑定本次艺人身份或有来源、中/高置信度的目录项目端点；随意 analysis_refs 不再增加关系和频率分。研究时纠正并记录候选类型，排序保存程序独占的 resolved_route；目录检查不冒充在线关系核验。
- 多兴趣分析：新增 preference_model.py，有界确定性分组最多 3 个兴趣，按置信度和艺人曲目数平方根平衡权重；风格和听感匹配同一兴趣组。保存代表曲目、分组来源和 matched_interest_id，支持人工配置兴趣覆盖奖励与最低覆盖，重算校验防止分组篡改，无新增 Python 依赖。
- 分阶段研究与说明：新增 research.py/explanations.py，两个 Agent CLI 支持候选目标、候选上限和最多 3 轮研究，timeout 为共享总预算；补充轮遵守原上下文硬预算及本次排重清单。候选说明可省略，仅对最终入选生成 program_explanation，渠道重算拒绝篡改。研究报告记录每轮字符量、摘要、耗时、类型纠正及成功排序包摘要；预算耗尽或进程失败仅写诊断，保留已有推荐文件。
- 人工试听对照：新增 benchmark.py 和 prepare-benchmark/benchmark 命令，生成隐藏方案归属、排名与分数的合并清单；支持明确标签、理由、备注，校验相同输入/日期/偏好及遥测对应关系，保护已有标签和输入文件。缺标签或 unsure 不当拒绝，未完整标注时不报告质量差值；完整标注也不自动选赢家或调参。规则分数不是概率，校准误差改为 null/not_calibrated。
- 同步 README、项目总结、待办与候选/说明提示词，明确旧分析、上下文和排序工件须重建，当前公共画像不等于完整个人偏好。

实际验收：

- Python 从 123 项增至 148 项，最终 `python -m unittest discover -s tests -v` 全部通过；新增覆盖画像/引用/多兴趣、分轮研究/超时/说明与人工试听的测试，独立进程回归新增候选预算和对照命令。
- `python -m compileall -q .`、两个 Node 模块的 `node --check` 通过；11 项 Node 测试、1 项 Chromium 本地下载测试通过。未访问 TuneMyMusic 线上页面。
- 新建夹具目录 `runtime/quality-acceptance-20260905-f0be62/`，固定评分参考日期 2026-09-05。3 首输入产生 A 的 20 首候选/1 轮研究、B 的 40 首候选/2 轮研究，双方各输出 10 首草稿和 10 份程序说明，validate 重算文件 SHA-256 分别与原文件一致。
- 此次 fake Agent 的 A/B 总耗时分别为 80.49/142.70 毫秒，输入字符量分别为 30,208/61,385。它们仅是本地合成链路观测，不代表真实检索时延、计费 token 或质量收益；两方案的候选预算不同。
- prepare-benchmark/benchmark 独立 CLI 通过；未填写试听标签时为 incomplete_labels，质量差值和 winner 均为 null，same_research_limits 为 false，policy_changed 为 false。
- 严格证据审计 0/10 核验通过、10 首待核验，实际 Python 退出码 2；候选轮数不足与 1 秒子进程超时也均返回 2，写出对应报告而没有成功推荐或渠道产物。
- `git diff --check` 通过（仅既有行尾转换提示）。本轮未覆盖个人输入或旧运行目录，未调用真实模型 Agent、发送消息、提交、打标签、推送或部署。

尚未验收真实推荐质量提升：私有画像仍缺失，公开示例仅含 4 位艺人；可信在线事实核验与同预算的真实试听实验仍待完成。

## 2026-09-06（四项优先优化，本地未发布）

按优先级逐项实现并验收，保留此前未提交修改，不创建维护标签或发布操作。

- 证据与输出：来源等级和公开关联分数按证据 URL/事实类型推导，忽略 Agent 自报等级、来源类别与正面核验声明；已知矛盾、不可访问、过期和无效标识符阻断排序与输出。程序独占 `publication_status: "draft"`，三个渠道明确标注研究草稿，不将离线结果冒充正式推荐。
- 上下文与预算：固定指令和完整 JSON 也超预算时直接报错；只裁剪可编辑插槽，修正裁剪后预算报告。准备阶段持久化预算、目录、分析投影与提示词摘要，执行阶段原样复用；显式配置冲突、缺失或篡改要求重新准备。两个 Agent CLI 支持自定义 `--manifest`，省略目录/预算时继承准备配置。
- Apple 导出：锁定 `csv-parse` 7.0.2，支持 BOM、跨行与转义引号；下载监听先于按钮动作，临时下载经编码/CSV/数量校验后才替换目标。新增 `--expected-count`，未提供独立数量时标记完整性未确认；失败保留旧文件、清理暂存文件并关闭浏览器，导出超时转为契约错误。
- 身份与日期：QQ 摘要覆盖有序、长度分隔的全分页原始响应，记录逐页哈希和实际页数。`analyze/run --as-of-date` 固定评分日期，默认取快照 UTC 日期；日期、平台歌曲 ID 和来源元数据进入分析身份，新鲜度评分不再依赖再次运行当天。
- README、tools 说明、项目总结和遗留项同步；Schema 仍为 2.0，但旧分析包、上下文与排序工件需要重建，禁止手工补身份字段冒充新结果。

实际验收：

- Python 测试从本轮开始时的 104 项增至 123 项，逐项验收阶段依次为 108、114、117、123 项通过；最终全量复跑 123 项通过。
- `python -m compileall -q .`、两个 Node 模块的 `node --check`：通过；`npm test` 11 项通过，`npm run test:browser` 1 项通过。
- Chromium 验收实际执行本地 Blob 下载与生产下载/校验辅助函数，覆盖多行 CSV 和数量错误时保留旧文件；已安装匹配的 headless shell。本轮未访问 TuneMyMusic 线上页面，不能据此宣称选择器在线兼容。
- 独立 CLI 验收目录 `runtime/acceptance-priority-20260906-c13e5b/`：run → fake Agent → validate → evaluate → tune 通过，3 首夹具输入生成 10 首草稿；重算前后文件 SHA-256 相同，准备预算 100,000 字符在 Agent 阶段继承，3 条反馈全部匹配，调优仍需批准且未自动应用。
- 严格证据审计：0/10 核验通过，10 首待核验，实际 Python 退出码 2；只生成审计报告，没有 strict-ranked.json 或 strict-channel.txt。`git diff --check` 通过（仅行尾转换提示）。
- 未调用真实模型 Agent、核验线上音乐事实、发送消息、提交、打标签、推送或部署；未覆盖个人输入与旧运行目录。

## 2026-09-06（本地未发布修复）

继续落实上次审查方案，保持 Schema 2.0、纯 Python 主流程及人工批准调优边界。

- 排序信任边界：Agent 的 ready 输出只能是 candidate_pool，禁止全部程序评分/选曲/序列字段；ranked 包的原始曲目信息必须与候选池一致，并在使用前重算评分、入选歌曲、顺序和 ranking manifest，拒绝篡改。评分校验仍位于推荐模块，不引入循环依赖。
- 约束选曲：保留原贪心偏好顺序，增加配额、艺人/项目名额和覆盖可行性剪枝，以及确定性回溯；修复 20 首合法候选无法选满 10 首的问题。50,000 个状态的搜索预算耗尽与约束无解分别报错，支持零配额类型。
- 证据审计：分开报告来源等级、标识符格式和事实核验状态；不再将稳定标识符或 Agent 自报 verified 当成事实核验。负面状态不被重复来源掩盖，整体建议等级受最弱证据项限制。严格审计失败返回 2，保留审计报告，不生成或覆盖排序文件和渠道文本。
- 时间与数量：检索时间无时区时按 UTC 处理，非法时间/发行日期返回 ContractError；CSV 声明数量按显式参数、显式数量文件、行数排序，数量文件损坏或缺失时不回退，数量不一致阻止 Step 2。
- 反馈与调优：评估和调优共用当前分析匹配、真实时间取最新及稳定同刻决胜逻辑；调优使用同一份排序结果。未反馈歌曲不作为拒绝或校准样本，无有效接受/拒绝对照时不产生调整建议；召回建议根据已反馈歌曲的实际接受率计算。
- 人工策略：analyze/run 新增 --policy-file，支持对象递归覆盖与数组整体替换，拒绝未知字段、无效权重/配额/上限及固定边界变更；最终策略摘要进入 analysis_id 和 manifest，默认策略深拷贝，调优建议不自动加载。
- 执行与测试：Windows 保留原生命令行引号语义，POSIX 使用 shlex，保持 shell=False 与 UTF-8；以带空格的临时 Python 环境实际验证路径、-c、引号及中文输入。两项测试移除 input/ 和私有画像依赖，改用仓库公开夹具。
- README、atlas.md 与遗留项说明同步能力边界。可信在线事实核验适配器及实时代理试运行仍未完成；历史日志中的“10/10 接受”仅是旧离线规则的输出，不能证明外部事实核验。

实际验证：

- 修改前：67 项测试中 65 项通过、2 项因缺失私人歌单报错。
- 修改后：104 项测试通过，包含回溯与穷举可行性对照、篡改拒绝、证据状态、反馈/策略边界及独立 CLI 进程回归。
- `python -m compileall -q .`：通过。
- `node --check tools/export_apple_playlist.mjs`：通过。
- 临时目录独立进程完成 run → fake Agent → validate → evaluate → tune；3 首输入生成 10 首结构合规推荐，评估与建议不修改策略。
- 严格证据审计：测试推荐 0/10 核验通过，退出码 2，仅生成审计报告；篡改结果和 CSV 数量不一致的失败分支也通过验证。
- 未执行真实 Agent、在线平台验证、消息发送、提交、打标签、推送或部署；未覆盖历史运行产物。

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


补记：创建仓库级 `AGENTS.md`，固化独立仓库边界（不随外层 AI 仓库发布）、无版本对象规则、个人数据禁止提交清单、设计边界与提交前验证方式。

同日补记：新增 `netease_public` 网易云公开歌单匿名读取器（免登录、零新依赖）。网络与解析分离，支持数字 ID 与分享链接，数量取接口 `trackIds`，坏行跳过并在数量不一致时标记 `incomplete`；隐私歌单明确不在能力范围。真实验证：热歌榜 3778678 全链路 snapshot（200 首 complete）→ analyze（172 艺人实体）→ prepare-agent。

同日补记：Apple Music 歌单导出固化为项目能力。新增 `tools/export_apple_playlist.mjs`（Playwright 无头，事件驱动等待，下载后校验表头与行数，失败显式报错并提示页面改版风险）与 `workflow.py export-apple-playlist` 子命令（node 缺失时给出安装指引）；`tools/README.md` 记录依赖与用法，`tools/node_modules/` 入 Git 忽略。修复过程中定位三处问题：目标网格未点击（漏掉 Choose Destination 点击）、`has-text("Export")` 误匹配服务块（改 exact 匹配）、页面中英双语渲染（主选择器改用语言无关的 aria-label）。验收：真实 115 首歌单经新子命令重新导出成功，snapshot(complete) -> analyze(68 实体) -> prepare-agent 全链路通过，67 个测试全绿。新增 `atlas.md` 项目总结。

维护标签：`patch-20260904-183859`

同日补记：工作流定位从"每周"改为"按需触发、不绑定时间"，明确对本次输入的整个歌单做全量解析；每次运行固定输出 10 首推荐，与歌单规模无关。仅调整描述性文案（workflow 帮助文本、contracts docstring、README 首段、hermes_weekly.sh 注释），无任何逻辑变更；hermes_weekly.sh 文件名保持不变并在注释中标注为可选调度模板。

同日补记：新增 `qq_public` QQ 音乐公开歌单匿名读取器（免登录、零新依赖）。走 `musicu.fcg` 网关 `music.srfDissInfo.DissInfo/CgiGetDiss` 模块，按接口 `hasmore` 信号分页（每页 100 首），歌曲以 `mid` 作为稳定 platform_track_id，数量取接口 `total_song_num`；接口拒绝（req_1.code 非 0）与坏行跳过均有显式语义。真实验证：30 首公开歌单 snapshot complete；三页小分页探针确认 hasmore/偏移正确；analyze -> prepare-agent 链路通过。

验证：

- `python -m unittest discover -s tests -v`：65 个测试通过（新增 QQ 音乐 ID/URL 解析、payload 归一化、分页信号与快照构建九组）。
- `python -m compileall -q .`：通过。

维护标签：`patch-20260904-170410`

同日补记：打通 Apple Music 个人歌单免登录导入链路（TuneMyMusic 中转）。实测：tunemymusic.com 无需注册登录，源选 Apple Music → "Load from URL" 粘贴歌单分享链接即可加载（本次"喜爱歌曲"歌单 115 首），目标 "Export to file" 导出 CSV。适配：`CsvPlaylistReader` 表头归一化（`Track name`→`track_name` 等），`Apple - id` 作稳定 `platform_track_id`，无元数据 CSV 以数据行数为声明数量。直接抓取 Apple 网页仅预渲染约 40 首，已验证不可行并记录。真实验证：115 首 CSV → snapshot complete → analyze（68 艺人实体）→ prepare-agent。

验证：

- `python -m unittest discover -s tests -v`：67 个测试通过（新增 CSV 表头归一化与 TuneMyMusic 导出解析两组）。
- `python -m compileall -q .`：通过。

维护标签：`patch-20260904-180934`

验证：

- `python -m unittest discover -s tests -v`：56 个测试通过（新增 `tests/test_source_adapters.py` 十组，ID/URL 解析、payload 归一化、快照构建均以 mock 网络覆盖）。
- `python -m compileall -q .`：通过。

维护标签：`patch-20260904-165242`

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

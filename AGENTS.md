# Music Atlas 仓库规则

## 仓库边界

- 本目录（`apps/music-atlas`）是**独立 Git 仓库**，远端为 `https://gitee.com/Hawkiethehawk/music-atlas.git`，分支 `master`。
- 外层 Codex 工作区仓库（`E:\LLM-Sandbox\Codex`）不管理本目录内容；在外层仓库执行 `git add`/`commit`/`push` 时不得纳入本目录。
- 所有提交、推送、标签操作仅针对本仓库；跨仓库操作前必须重新确认仓库根（`git rev-parse --show-toplevel`）。

## 版本与发布规则

- 本仓库未配置版本对象：**不设置版本号，不创建版本标签**。
- 提交与推送统一走 `version-manager` Skill；推送前必须更新本仓库 `CHANGELOG.md`（记录日期、实际变更、真实验证结果），并创建 `patch-YYYYMMDD-HHMMSS` 维护标签，标签在提交前确定并写入 CHANGELOG。
- 变更分类按 version-manager 规则执行；混合变更按其中影响最大的类型记录。
- 未获用户当前对话明确授权，不得 push、打标签或改写历史。

## 数据边界（禁止提交）

以下内容属于个人数据或本地运行产物，已被 Git 忽略，暂存时必须排除：

- `input/`：个人歌单输入
- `runtime/`：本地运行产物与归档
- `styles/artist_style_profiles.json`：私有逐艺人风格画像（公开示例 `styles/artist_style_profiles.example.json` 可以提交）
- `secrets/`、`.env`：Apple Music 凭据与令牌
- `state.json`、`*.log`、`__pycache__/`

暂存使用明确路径，不使用覆盖整个仓库的 `git add -A`；提交前核对暂存区不含上述文件。

## 设计边界（代码改动约束）

以下边界来自 README「设计边界」，修改代码时不得破坏：

1. Step 1/2/3 通过 JSON 契约连接；Step 2 只读本次 `PlaylistSnapshot`，Step 3 只读本次 `MusicianAnalysisPacket`。
2. 七维评分、召回配额、去重、MMR 多样性与能量弧排序全部由 `recommender.py` 确定性计算；Agent 不得提交评分字段（契约层强制）。
3. Apple Music 平台个性化推荐、登录态、历史运行结果不参与候选发现、排序或说明生成；个性化音乐页面不能作为证据来源（`_source_is_forbidden_personalization` 强制）。
4. 反馈只用于只读离线评估与人工批准的调优建议（`approval_required: true`），绝不自动调整排序策略。
5. 数量契约 `declared_track_count == track_count == len(tracks)` 由 `contracts.py` 强制。
6. Schema 1.0 历史产物不可复用，只允许经 `archive-schema1` 归档。

## 验证方式

提交前必须实际执行并在 CHANGELOG 中如实记录结果：

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

涉及工作流行为改动时，另跑仓库夹具端到端链路：

```bash
python workflow.py run --input tests/fixtures/playlist_sample.json --reader local_json \
  --platform apple_music --playlist-id sample --playlist-name '示例歌单' --runtime-dir runtime/local-run
python workflow.py agent --analysis runtime/local-run/musician_analysis.json \
  --prompt runtime/local-run/agent_prompt.md --output runtime/local-run/recommendation_bundle.json \
  --channel-output runtime/local-run/channel_text.txt --command 'python tests/fixtures/fake_agent.py'
```

不得把未运行的测试写成已通过。

## 其他约定

- 默认使用中文回答与书写文档；CHANGELOG 与提交说明使用中文。
- 语言强制、执行确认、PowerShell（`pwsh`）等通用规则继承外层 `E:\LLM-Sandbox\Codex\AGENTS.md`。
- 生产部署前置条件：完成一次使用外部可验证证据的实时代理试运行（见 README「生产就绪前置条件」）；部署相关操作需单独授权。

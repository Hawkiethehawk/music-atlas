# Music Atlas

Music Atlas 是一个可扩展的每周音乐推荐工作流。它把歌单读取、音乐人关系分析和 Agent 推荐拆成三个独立步骤，并通过 JSON 契约连接每一步。

## 设计边界

1. Step 1 将 Apple Music、网易云或本地 JSON/CSV 歌单统一为 `PlaylistSnapshot`。
2. Step 2 只读取本次 `PlaylistSnapshot`，用确定性代码统计艺人分布，并解析主唱、前乐队和 side project 关系。
3. Step 3 只接收本次 `MusicianAnalysisPacket`，由 Agent 使用公开资料研究候选歌曲，并为每首歌曲生成推荐说明。

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

`--command` 指向的进程从标准输入读取 Agent prompt，并向标准输出写入 `RecommendationBundle` JSON。写入渠道文本前，程序会校验推荐数量、项目覆盖、重复歌曲、逐首说明、关系引用和公开来源。

## 数量契约

歌曲数量始终来自本次 Step 1 输出：

```text
declared_track_count == track_count == len(tracks)
```

代码不固定某个歌曲总数。Step 2 不重新访问音乐平台；Step 3 不读取原始快照、登录 profile、历史推荐或上一轮 Agent 输出。

## 扩展点

- `source_adapters.py`：本地 JSON、Apple Music JSON、网易云 JSON 和 CSV 读取器。
- `relations/artist_relations.json`：可审计的公开音乐人关系目录。
- `channels.py`：微信、飞书和 Telegram 的纯文本渲染适配器，只负责输出，不负责发送。
- `hermes_weekly.sh`：服务器侧调度入口模板；个人输入和运行目录应在部署环境中单独配置。

## 输出文件

一次运行会在指定 runtime 目录生成 `snapshot.json`、`musician_analysis.json`、`musician_analysis.md`、`agent_prompt.md`、`recommendation_bundle.json` 和 `channel_text.txt`。这些目录默认被 Git 忽略。

## 测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

`apple_music_weekly.py` 仅保留兼容转发入口，实际执行入口是 `workflow.py`。

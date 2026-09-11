# Music Atlas Web · Editorial Atlas Edition

本机固定端口服务：**http://127.0.0.1:8420**

```
music-atlas-web/
├── editorial-atlas.html          # Editorial Atlas 页面（服务根路径返回它）
├── favicon.ico                   # Hawkie logo 标签页图标（16/32/48 多尺寸）
├── server.js                     # 零依赖 Node 静态服务器、Atlas API 与受控工作流 API
├── package.json                 # npm start 入口
├── deploy/
│   └── music-atlas-web.service  # Linux systemd 单元（部署用）
└── README.md
```

页面数据不再写死在前端。`workflow.py web-export` 从当前运行目录读取已校验的
`snapshot.json`、`musician_analysis.json`、`recommendation_bundle.json` 和可选的
`evidence_audit.json`，生成脱敏后的 `web_payload.json`；Node 服务仅通过
`GET /api/atlas` 提供该 JSON。网页工作流通过本地 `POST /api/jobs` 创建任务，
仍复用仓库根目录的三步契约，不直接修改页面数据。

## 从网页触发歌单工作流

页面的“推荐源”入口只需提交一个公开歌单链接，系统会按域名自动识别 Apple Music、网易云/`163cn.tv`
或 QQ 音乐。网易云大歌单使用 v6 `trackIds` 加歌曲详情分批补齐，数量不一致时停止后续步骤。

Apple Music 不再要求用户手动填写歌曲数量；后台仍记录导出完整性状态。没有独立数量依据时，
结果会标记为“待确认”，不会把导出行数包装成独立确认的歌单总数。

任务顺序固定为：

```text
Step 1 歌单拉取与解析
  -> Step 2 分析研究（5 个内部并行任务）
  -> Step 2 全部完成并校验
  -> Step 3 候选研究（4 个内部并行任务，可配置为 3）
  -> 程序统一去重、评分、排序并导出页面数据
```

Node 服务只读取项目根目录的 `config/web.json`，不依赖系统环境变量。模型、账号或联网代理仍不内置；执行器脚本必须位于项目目录内：

```json
{
  "runtime": {
    "python": "python",
    "codex_reasoning_effort": "low"
  },
  "executors": {
    "analysis": "executors/analysis_runner.py",
    "recommendation": "executors/recommendation_runner.py"
  },
  "workflow": {
    "analysis_parallelism": 5,
    "recommendation_parallelism": 4,
    "analysis_timeout_seconds": 7200,
    "recommendation_timeout_seconds": 3600
  }
}
```

当前项目内的两个执行器是 `executors/` 下的本机 Codex 桥接脚本：它们从标准输入读取任务，
调用本机已安装的 Codex CLI，再只把最终 JSON 转发到 Music Atlas。Codex 的登录、模型和供应商
继续使用本机已有配置，不复制密钥，也不修改系统配置。若本机找不到 Codex CLI，页面会保持禁用，
不会以示例结果冒充真实分析。任务状态可通过 `GET /api/jobs/:id` 或页面事件面板查看。

## 从当前 Music Atlas 运行目录启动

在仓库根目录执行：

```bash
python workflow.py web-export \
  --runtime-dir runtime/apple-link-20260908 \
  --output runtime/apple-link-20260908/web_payload.json
```

然后启动网页：

```powershell
Set-Location E:\LLM-Sandbox\Codex\apps\music-atlas
npm --prefix web start
```

网页固定读取 `config/web.json` 中 `paths.published` 指向的稳定发布文件。
未配置数据文件时，`/api/health` 会报告不可用，页面会显示数据不可用状态，
不会伪造推荐结果。

## Windows / macOS / Linux 开发环境启动（跨平台）

```bash
npm start          # 或 node server.js
```

启动后访问 http://127.0.0.1:8420 。停止：在终端 `Ctrl+C`；后台运行时结束对应 node 进程即可。
端口被占用时服务会直接退出并提示（判定为已在运行）。

## Linux 部署（生产/正式环境）

服务监听 `127.0.0.1:8420`。若需要局域网/公网访问，建议前置 Nginx/Caddy 反代，而不是改代码。

```bash
# 1. 拷贝整个目录到服务器，例如 /opt/music-atlas-web
# 2. 安装 systemd 服务（按需修改 .service 里的 User/路径/端口）
sudo cp deploy/music-atlas-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now music-atlas-web
systemctl status music-atlas-web
```

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `server.port` | `8420` | 监听端口 |
| `server.host` | `127.0.0.1` | 监听地址 |
| `paths.published` | `runtime/web/current.json` | 当前网页读取的稳定发布文件 |
| `paths.jobs` | `runtime/web-jobs` | 网页工作流运行目录 |
| `paths.input` | `input` | 本地 JSON/CSV 输入目录；网页只接受其相对路径 |
| `runtime.python` | `python` | 启动网页工作流的 Python 命令；路径形式必须位于项目内 |
| `runtime.codex_reasoning_effort` | `low` | 本机 Codex 批处理推理级别；模型、登录和供应商仍读取本机配置 |
| `executors.analysis` | 空 | 项目内 Step 2 Python 执行器脚本 |
| `executors.recommendation` | 空 | 项目内 Step 3 Python 执行器脚本 |
| `workflow.analysis_timeout_seconds` | `600` | Step 2 总执行预算；大歌单可在项目配置中提高 |
| `workflow.recommendation_parallelism` | `4` | Step 3 内部并行数，只接受 `3` 或 `4` |
| `workflow.recommendation_timeout_seconds` | `600` | Step 3 总执行预算 |

配置文件位于项目内并纳入 Git；运行产物和本地输入仍写入项目内的 `runtime/`、`input/` 目录，
不通过环境变量改写路径或端口。

## 说明

- 页面沿用 Editorial Atlas 的视觉原型，但展示内容来自当前 Music Atlas 运行产物。
- 页面显示研究草稿、证据待核验或画像覆盖不足等状态，不把这些状态提升为正式推荐。
- 无播放器；所有“打开”操作为跳转外部平台（Apple Music / 网易云 / QQ 音乐）。
- 不提供歌单 CRUD、账号登录或平台个性化推荐；`POST /api/jobs` 仅用于启动受控的本地 Music Atlas 工作流，页面数据仍由工作流原子导出。

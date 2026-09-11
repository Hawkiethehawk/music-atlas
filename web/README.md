# Music Atlas Web · Editorial Atlas Edition

本机固定端口服务：**http://127.0.0.1:8420**

## 安装

仓库根目录提供一键部署脚本，其内部只做一件事：执行 `atlas setup`（检查依赖 → 安装缺失依赖 → 注册 `atlas` 命令）：

```powershell
pwsh -File install.ps1        # Windows
```

```bash
./install.sh                  # Linux / macOS
```

只检查不安装：`python atlas.py setup --check-only`。完整参数与行为见仓库根 `README.md` 的「安装（一键部署）」。

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

## 档位选择：歌单读完后才确定处理数量

网页端不在提交前填写数量：**歌单读取完成后**才出现「处理数量」控件，上限为实际读到的曲目数，
不会超过歌单真实规模。可以输入任意整数，也可直接点 `30 / 100 / 200 / 500 / 1000` 档位快捷键
（超过上限的档位自动禁用）；输入值精确生效，等于某个档位时该档位高亮。默认档位 `30`。

确定后工作流按歌单原顺序取前 N 首继续分析：截断结果自身满足 Step 1 契约
（`declared_track_count == track_count == len(tracks)`），截断前的曲目总数保留在
`snapshot.reader.source_track_count`，`snapshot.reader.requested_track_limit` 记录生效数量，
快照 ID 追加 `-limitN` 后缀，避免与未截断运行共用分析缓存。等待期间可点「取消任务」；超过
`workflow.await_limit_timeout_seconds` 未提交则本次任务失败。

任务顺序固定为：

```text
Step 1 歌单拉取与解析
  -> 等待选择处理数量（网页端，超时 30 分钟）
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
    "codex_reasoning_effort": "low",
    "openai_compat": {
      "base_url": "https://sshzyu.com/v1",
      "model": "glm-5.3-flash",
      "api_key_env": "ZH_GLM_API_KEY",
      "timeout_seconds": 1800,
      "max_tokens": 60000
    }
  },
  "executors": {
    "analysis": "executors/openai_analysis.py",
    "recommendation": "executors/openai_recommendation.py"
  },
  "workflow": {
    "analysis_parallelism": 5,
    "recommendation_parallelism": 4,
    "analysis_timeout_seconds": 7200,
    "recommendation_timeout_seconds": 3600,
    "track_limit_options": [30, 100, 200, 500, 1000],
    "track_limit_default": 30,
    "await_limit_timeout_seconds": 1800
  }
}
```

当前生产配置使用 `executors/` 下的 **OpenAI 兼容执行器**（`openai_analysis.py` /
`openai_recommendation.py`）：它们从标准输入读取任务，调用 `runtime.openai_compat` 声明的
Chat Completions 端点（当前为 `glm-5.3-flash`），只把最终 JSON 转发给 Music Atlas。
API key 只从 `api_key_env` 指定的环境变量读取，不写入仓库，也不修改本机 Codex 配置。
原有的本机 Codex 桥接脚本（`analysis_runner.py` / `recommendation_runner.py`）仍然保留，
把 `executors` 指回它们即可切回 Codex（新版 Codex CLI 仅支持 Responses API，
因此只支持 Chat Completions 的供应商需用 OpenAI 兼容执行器）。

模型 API 本身不提供联网检索：执行器提示词明确要求只凭既有知识给出可事后核验的公开来源，
不确定就留空；事实核验与证据分级仍由程序（`evidence.py`）与契约层负责，页面不会因流程
跑通就标注为已核验。执行器文件缺失或 key 未配置时任务会失败并如实报错，不会以示例结果
冒充真实分析。任务状态可通过 `GET /api/jobs/:id` 或页面事件面板查看。任务在等待选择处理数量时状态为
`awaiting_limit`：`POST /api/jobs/:id/limit` 提交数量（`{"limit": N}`，越界返回 400），
`POST /api/jobs/:id/cancel` 取消等待中的任务。

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
| `runtime.openai_compat` | 无 | OpenAI 兼容执行器的 `base_url` / `model` / `api_key_env`（密钥所在环境变量名），以及可选 `timeout_seconds` / `max_tokens` / `temperature`；密钥不进仓库 |
| `executors.analysis` | 空 | 项目内 Step 2 Python 执行器脚本 |
| `executors.recommendation` | 空 | 项目内 Step 3 Python 执行器脚本 |
| `workflow.analysis_timeout_seconds` | `600` | Step 2 总执行预算；大歌单可在项目配置中提高 |
| `workflow.recommendation_parallelism` | `4` | Step 3 内部并行数，只接受 `3` 或 `4` |
| `workflow.track_limit_options` | `[30, 100, 200, 500, 1000]` | 页面档位快捷键；只接受递增正整数，超过曲目数的档位自动禁用 |
| `workflow.track_limit_default` | `30` | 数量控件默认档位；大于上限时按上限取值 |
| `workflow.await_limit_timeout_seconds` | `1800` | 等待选择处理数量的秒数，超时任务失败（1 到 86400） |
| `workflow.recommendation_timeout_seconds` | `600` | Step 3 总执行预算 |

配置文件位于项目内并纳入 Git；运行产物和本地输入仍写入项目内的 `runtime/`、`input/` 目录，
不通过环境变量改写路径或端口。

## 说明

- 页面沿用 Editorial Atlas 的视觉原型，但展示内容来自当前 Music Atlas 运行产物。
- 兴趣组名字由程序从该组主导风格总结生成（3–5 个汉字，同期内不重复，素材不足时回退 `兴趣组 NN`）；人工 `--editorial` 配置的名字优先。命名不调用 Agent，也不改变 Step 1–3 契约。
- 页面显示研究草稿、证据待核验或画像覆盖不足等状态，不把这些状态提升为正式推荐。
- 无播放器；所有“打开”操作为跳转外部平台（Apple Music / 网易云 / QQ 音乐）。
- 不提供歌单 CRUD、账号登录或平台个性化推荐；`POST /api/jobs` 仅用于启动受控的本地 Music Atlas 工作流，页面数据仍由工作流原子导出。

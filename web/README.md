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
├── admin.html                    # 独立管理员控制台（/admin）
├── auth_store.js                 # SQLite 用户、会话、歌单与偏好存储
├── admin_bootstrap.js            # 首次创建管理员账号的一次性命令
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
不会超过歌单真实规模。只提供 `25% / 50% / 100%` 三种歌单分位，按真实总数向上取整
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

Node 服务以项目根目录的 `config/web.json` 为基础，并在网页设置保存后读取 Git 忽略的 `runtime/web/settings.json` 覆盖；测试或多实例可用 `ATLAS_WEB_CONFIG` / `ATLAS_WEB_SETTINGS` 指定路径。模型、账号或联网代理仍不内置；执行器脚本必须位于项目目录内：

```json
{
  "runtime": {
    "python": "python",
    "codex_reasoning_effort": "low",
    "openai_compat": {
      "base_url": "https://sshzyu.com/v1",
      "model": "deepseek-v4.1-flash",
      "timeout_seconds": 1800,
      "max_tokens": 60000
    }
  },
  "executors": {
    "analysis": "executors/openai_analysis.py",
    "recommendation": "executors/openai_recommendation.py"
  },
  "crawler": {
    "request_timeout_seconds": 30,
    "netease_song_detail_batch_size": 200,
    "qq_page_size": 100,
    "qq_max_pages": 50,
    "apple_export_timeout_seconds": 600
  },
  "workflow": {
    "analysis_parallelism": 5,
    "recommendation_parallelism": 8,
    "analysis_timeout_seconds": 7200,
    "recommendation_timeout_seconds": 3600,
    "track_percentile_options": [0.25, 0.5, 1],
    "track_percentile_default": 1,
    "await_limit_timeout_seconds": 1800,
    "max_research_rounds": 2,
    "max_candidates": 60
  }
}
```

当前生产配置使用 `executors/` 下的 **OpenAI 兼容执行器**（`openai_analysis.py` /
`openai_recommendation.py`）：它们从标准输入读取任务，调用 `runtime.openai_compat` 声明的
Chat Completions 端点（当前为 `deepseek-v4.1-flash`），只把最终 JSON 转发给 Music Atlas。
API Key 优先从系统密钥库（`secret_store.py`，基于 keyring：Windows Credential Manager / macOS Keychain / Linux Secret Service）读取；没有时回退到 `MUSIC_ATLAS_API_KEY` 环境变量，都没有则报错停止，绝不写入仓库。

网页设置中的「API Key」输入框会把它加密保存到本机系统密钥库（Windows Credential Manager / macOS Keychain / Linux Secret Service），后台任务自动解密复用；网页只会显示「已配置」，不会回显密钥。无系统密钥库的环境（如无桌面的 Linux 服务器）会报错提示配置，不会静默降级到明文文件。
原有的本机 Codex 桥接脚本（`analysis_runner.py` / `recommendation_runner.py`）仍然保留，
把 `executors` 指回它们即可切回 Codex（新版 Codex CLI 仅支持 Responses API，
因此只支持 Chat Completions 的供应商需用 OpenAI 兼容执行器）。

### 网页设置入口

页面右上角「设置」可编辑下一次任务使用的基础配置：AI 兼容接口（接口地址、模型、超时、Token 上限、温度、思维链开关）、
歌单读取/分页超时、分析与推荐并行度、候选研究预算、推荐策略边界，以及页面标题和导语。设置接口为本机限定：
`GET/PUT/DELETE /api/settings`；非法字段、范围和 URL 会被拒绝。保存时写入独立覆盖文件，不改写 `config/web.json`；
任务启动时会生成完整配置快照，因此任务运行中修改设置不会影响当前任务。

AI API Key 单独由 `GET/PUT/DELETE /api/secrets` 管理，保存到系统密钥库而不是配置文件；
保存后网页只显示「已配置」，不会回显密钥。没有系统密钥库时接口返回错误提示，不会静默降级。
环境变量 `MUSIC_ATLAS_API_KEY` 仍可作为无密钥库环境（如服务器部署）的回退方式。

恢复默认覆盖可在设置页点击「恢复默认覆盖」，或调用 `DELETE /api/settings`。

设置页的「测试连接」会用**当前表单里填写的**接口地址、模型、超时等参数（并先保存到下次任务生效的配置），
发送一个最小 Chat Completions 请求（`max_tokens: 16`、`temperature: 0`，并按配置携带关闭思维链参数）：

- 成功时显示延迟、模型、密钥来源（系统密钥库 / 环境变量）与响应预览；
- 上游返回非 2xx 时显示 HTTP 状态码与响应摘要；超时按 `min(请求超时, 60 秒)` 计算；
- 接口地址与模型未填写、或未配置 API Key 时给出明确提示。

接口为 `POST /api/ai/test`（仅本机），不会写入任何运行产物，也不影响当前运行中的任务。

### 用户账号与管理员后台

生产环境默认要求普通用户登录后才能提交工作流；Atlas 首页仍可匿名浏览当前公开结果。注册、登录和退出分别使用
`/api/auth/register`、`/api/auth/login`、`/api/auth/logout`，用户歌单与偏好保存在 `runtime/web/auth.sqlite`，
每个用户的七天推荐去重缓存也通过 `MUSIC_ATLAS_USER_ID` 隔离。任务状态接口只允许任务所属用户读取，管理员可以在后台查看全部用户任务。

管理员入口为 `/admin`，不在主页面导航中显示，且所有 `/api/admin/*` 接口都要求独立的管理员会话。后台沿用主页面的字体、色彩、边框和卡片样式，
可管理用户启停与角色、编辑完整网页设置、查看密钥配置状态、更新密钥并测试 AI 连通性；密钥值不会回显。首次部署后在服务器上执行一次：

```bash
node web/admin_bootstrap.js <管理员用户名>
```

命令会从交互输入或 `MUSIC_ATLAS_ADMIN_PASSWORD` 读取至少 8 位密码；已有管理员时不会覆盖。Node 运行时需为 **22.5 或更高版本**（使用内置 `node:sqlite`）。

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
| `runtime.openai_compat.disable_thinking` | `true` | 关闭模型思维链；实测 reasoning 占单次调用约 80% 耗时（真实批次 65s → 15.8s） |
| `workflow.max_research_rounds` | `2` | 候选研究轮数上限；第 2 轮只补缺失类型 |
| `workflow.max_candidates` | `60` | 候选池上限；过小会导致探索类型产出不足 |
| `runtime.openai_compat` | 无 | OpenAI 兼容执行器的 `base_url` / `model`，以及可选 `timeout_seconds` / `max_tokens` / `temperature`；密钥不进仓库，由系统密钥库或环境变量提供 |
| `crawler.request_timeout_seconds` / `crawler.request_retries` | `30` / `1` | 网易云/QQ 接口单次请求超时与 0–3 次重试 |
| `crawler.user_agent` | `MusicAtlas/1.0 (+local)` | 公开接口请求 User-Agent；禁止换行，长度受限 |
| `crawler.netease_*_url` / `crawler.qq_musicu_url` | 项目默认接口 | 高级接口地址；网页只接受 HTTPS 与对应官方域名 |
| `crawler.netease_song_detail_batch_size` | `200` | 网易云歌曲详情批量大小 |
| `crawler.qq_page_size` / `crawler.qq_max_pages` | `100` / `50` | QQ 音乐分页大小与上限 |
| `crawler.apple_export_timeout_seconds` | `600` | Apple Music 导出工具总超时 |
| `recommendation_policy` | 程序默认 | 可覆盖艺人/项目上限、最少项目数和候选池最低数量 |
| `editorial.title` / `editorial.lede` | 自动生成 | 下一次网页导出的标题和导语 |
| `executors.analysis` | 空 | 项目内 Step 2 Python 执行器脚本 |
| `executors.recommendation` | 空 | 项目内 Step 3 Python 执行器脚本 |
| `workflow.analysis_timeout_seconds` | `600` | Step 2 总执行预算；大歌单可在项目配置中提高 |
| `workflow.recommendation_parallelism` | `4` | Step 3 内部并行数，只接受 `3` 或 `4` |
| `workflow.track_percentile_options` | `[0.25, 0.5, 1]` | 页面歌单分位选项 |
| `workflow.track_percentile_default` | `1` | 默认分析 100% 歌单 |
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

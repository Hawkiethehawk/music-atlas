# tools/

维护性辅助工具，不属于纯 Python 工作流本体。

## export_apple_playlist.mjs

经 TuneMyMusic 免登录导出 Apple Music **公开分享歌单**为 CSV。日常使用通过工作流入口调用：

```bash
python workflow.py export-apple-playlist --url <歌单分享链接> --expected-count <独立确认的歌曲数>
# 默认输出 input/apple_favorite_songs.csv；随后：
python workflow.py snapshot --reader csv --platform apple_music \
  --playlist-id <歌单ID> --playlist-name <名称> --input input/apple_favorite_songs.csv
```

### 依赖（仅本工具需要）

- Node.js、Playwright 与锁定的 `csv-parse`。在本目录执行：

```bash
npm ci
npx playwright install chromium --only-shell
```

使用 `package-lock.json` 安装依赖；Chromium 版本须与安装的 Playwright 匹配，不能假定机器上已有兼容浏览器。

### 校验与失败行为

- 标准 CSV 解析支持 UTF-8 BOM、引号内逗号、转义引号和跨行字段；要求 `Track name`、`Artist name`、`Apple - id` 表头、列数一致，且歌曲名和艺人非空。
- `--expected-count` 必须是正安全整数，来自歌单页面等独立数量依据，而不是待校验 CSV 自身的行数。数量一致时返回 `completeness_status: "confirmed"`；未传入时明确返回 `"unconfirmed"`。
- 下载监听先于导出按钮触发。文件先暂存，下载、编码、CSV 或数量校验失败均保留旧目标文件并清理暂存文件；通过后才替换目标 CSV。
- 该数量核对不能证明第三方平台没有替换、遗漏或重复同数量的曲目，也不等于推荐事实核验。

### 本地验收

在本目录执行：

```bash
npm test
npm run test:browser
```

当前 11 项单元测试与 1 项 Chromium 测试通过。浏览器测试使用本地 Blob 下载与生产辅助函数，验证多行 CSV、数量不一致和旧文件保护；不访问 TuneMyMusic 线上页面。第三方页面结构或导出规则变化仍需要单独在线验证。

主工作流（Step 1/2/3、其他 Reader）不需要本目录与 Node.js。

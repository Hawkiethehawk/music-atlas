# Apple Music 公开歌单读取工具

## export_apple_playlist.mjs

通过 Apple Music 官方嵌入播放器读取公开分享歌单。工具使用无头 Chromium 获取 Apple 官方网页播放器的临时访问上下文，再从 `amp-api.music.apple.com` 按页读取完整曲目；不需要 Apple ID 登录，也不保存令牌或浏览器状态。

```powershell
python workflow.py export-apple-playlist `
  --url "https://music.apple.com/us/playlist/example/pl.u-example" `
  --output "runtime/apple-playlist.csv" `
  --expected-count 123
```

行为：

- 只接受 `https://music.apple.com` 或 `https://embed.music.apple.com` 的公开歌单链接。
- 逐页读取，直到 Apple 官方接口不再返回下一页。
- 输出前校验歌曲 ID、曲名、艺人、重复项和可选的独立总数。
- 采用临时文件写入；读取、分页或数量校验失败时不会覆盖已有 CSV。
- 成功结果标记 `source=apple_music_official_embed`；未传 `expected-count` 时，完整性依据为官方分页结束。

## 依赖与验证

```powershell
cd tools
npm install
npm test
npm run test:browser
```

线上可达性需用真实公开歌单单独验收；本地单元测试不会把模拟数据视为线上事实。

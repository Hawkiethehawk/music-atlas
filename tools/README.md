# tools/

维护性辅助工具，不属于纯 Python 工作流本体。

## export_apple_playlist.mjs

经 TuneMyMusic 免登录导出 Apple Music **公开分享歌单**为 CSV。日常使用通过工作流入口调用：

```bash
python workflow.py export-apple-playlist --url <歌单分享链接>
# 默认输出 input/apple_favorite_songs.csv；随后：
python workflow.py snapshot --reader csv --platform apple_music \
  --playlist-id <歌单ID> --playlist-name <名称> --input input/apple_favorite_songs.csv
```

### 依赖（仅本工具需要）

- Node.js
- `npm install playwright`（在本目录执行；浏览器二进制复用系统 Playwright 缓存）

主工作流（Step 1/2/3、其他 Reader）不需要本目录与 Node.js。

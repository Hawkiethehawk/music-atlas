# Changelog

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

# Step 2 来源策略

优先使用可公开访问且能直接支持事实的来源，例如：

- 艺人或厂牌官方页面
- 公开采访、音乐评论和发行说明
- MusicBrainz、Wikidata、Wikipedia 等公开音乐资料库

每条证据记录：

- `claim_type`：`style`、`track_identity`、`relation` 或 `release`
- 具体事实 `claim`
- HTTP(S) `url`
- 实际检索时间 `retrieved_at`

禁止使用登录态、个性化推荐页、播放历史或私人资料。Apple Music 可以作为输入中的平台链接，但不能作为画像证据。

来源可访问、来源等级高或有稳定 ID，都不代表事实已经独立核验。结果必须保持 `unverified`，由程序和后续人工流程处理核验状态。

# Step 2 研究结果契约

结果顶层必须包含：

- `schema_version: "2.0"`
- `bundle_type: "musician_research_result"`
- 与输入完全一致的 `request_id` 和 `source_snapshot_id`
- 实际生成时间 `generated_at`
- `track_profiles`
- `artist_relations`

`track_profiles` 必须覆盖输入中的全部位置，不能重复或增加位置。每项包含：

当前请求若带 `style_fact_policy: "precollected_only"`，说明没有附带程序已采集的风格来源。此时每条曲目画像只能返回以下字段和值：

```json
{
  "position": 1,
  "track_key": "title - artist",
  "classification_status": "unclassified",
  "scope": "unknown",
  "confidence": "low",
  "style_mix": [],
  "summary": "尚无已采集的曲目风格来源",
  "evidence_items": []
}
```

当前请求不得填写 `style_axes`、自找来源 URL 或把关系研究当成曲目风格证据。公开风格标签由程序采集，再按单曲、专辑、艺人层级归并；专辑和艺人资料不提升为单曲已分类。所有歌曲都必须保留身份与位置，关系艺人仍按下述契约返回。

旧的研究结果若来自不带 `style_fact_policy` 的历史请求，沿用其原有 `style_axes` 与分类校验，只用于读取和验证旧工件；新请求不能省略该标记来绕过当前契约。显式 `--analysis-mode catalog` 是另一条兼容路径，不会让来源模型重新产生八轴。

证据 `claim_type` 只能是 `style`、`track_identity`、`relation` 或 `release`；不得增加自定义类型。

每个关系艺人必须恰好出现一次。主唱和关联项目事实都必须携带 `relation` 证据；无法确认时使用 `unknown` 或空列表。

同一关系艺人的主唱与关联项目列表必须按规范化姓名或关系端点去重；同一端点只能出现一次。

`artist_relations[].entity_type` 只能是 `band`、`person`、`project` 或 `unknown`；关系端点不能使用其他自定义类型。

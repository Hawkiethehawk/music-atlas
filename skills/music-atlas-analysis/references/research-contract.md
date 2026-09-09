# Step 2 研究结果契约

结果顶层必须包含：

- `schema_version: "2.0"`
- `bundle_type: "musician_research_result"`
- 与输入完全一致的 `request_id` 和 `source_snapshot_id`
- 实际生成时间 `generated_at`
- `track_profiles`
- `artist_relations`

`track_profiles` 必须覆盖输入中的全部位置，不能重复或增加位置。每项包含：

```json
{
  "position": 1,
  "track_key": "title - artist",
  "classification_status": "classified | unclassified",
  "scope": "artist | release | track | unknown",
  "confidence": "high | medium | low",
  "style_mix": [],
  "style_axes": {},
  "summary": "事实和限制",
  "evidence_items": []
}
```

已分类结果必须使用 taxonomy 中的风格引用、恰好一个 `primary` 风格、八个 0～100 风格轴和至少一条可用 `style` 证据。

证据 `claim_type` 只能是 `style`、`track_identity`、`relation` 或 `release`；不得增加自定义类型。

未分类结果必须使用 `scope: "unknown"`、`confidence: "low"`、空风格数组、八轴 `null` 和空证据数组。

每个关系艺人必须恰好出现一次。主唱和关联项目事实都必须携带 `relation` 证据；无法确认时使用 `unknown` 或空列表。

同一关系艺人的主唱与关联项目列表必须按规范化姓名或关系端点去重；同一端点只能出现一次。

`artist_relations[].entity_type` 只能是 `band`、`person`、`project` 或 `unknown`；关系端点不能使用其他自定义类型。

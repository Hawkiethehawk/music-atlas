# Step 3 候选结果契约

正常结果只能是 `RecommendationBundle` 的 `candidate_pool` 阶段：

```json
{
  "schema_version": "2.0",
  "bundle_type": "recommendation_bundle",
  "bundle_stage": "candidate_pool",
  "status": "ready",
  "analysis_id": "必须等于输入分析包",
  "generated_at": "ISO-8601",
  "candidate_pool": [],
  "recommendations": []
}
```

每个候选必须包含稳定的 `canonical_track_id`、标题、艺人、项目、`candidate_type`、至少一个 `analysis_refs`、风格混合、置信度、公开证据、来源和平台链接。`style_analysis.evidence_model == "sourced_tags_v1"` 时禁止填写 `style_axes`；只有显式历史 catalog 分析包沿用八轴字段。来源模型的单曲、专辑、艺人标签须注明对应层级，不能将背景资料表述为该单曲的声音事实。

候选不得包含程序拥有的字段，例如 `ranking_score`、`score_breakdown`、`selection_rank`、`ranking` 或最终推荐说明。

`insufficient_evidence` 时不输出候选和推荐。所有结果都是研究草稿，不等于外部事实已经核验。

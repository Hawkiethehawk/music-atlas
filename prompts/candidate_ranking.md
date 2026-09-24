# 候选排序提示词插槽

不要填写 `score_features`。为候选填写可追溯的 `style_mix`、`analysis_refs`、`evidence_grade` 和 `evidence_items`；每个候选必须同时有 `track_identity` 与 `style` 证据（音乐人关系候选另需 `relation`），并将证据 URL 列入 `sources`。单曲无标签时可用专辑或艺人资料作背景，但证据描述须写明层级，不得称为单曲事实。资料不足时不提交候选。`sourced_tags_v1` 分析包的候选不得填写 `style_axes`；只有显式历史 catalog 分析包仍按旧契约包含八轴。

不要填写 `resolved_route`、`matched_interest_id` 或 `program_explanation`，它们由程序生成。`analysis_refs` 仅作引用索引，不作为关系或频率加分证据；规则分数不是喜欢概率。

排序时同时考虑：

- 细分风格匹配
- 音乐人关系强度
- 当前艺人频率信号
- 新鲜度与探索价值
- 公开证据质量
- 公开关联信号

来源模型只使用上述六项有据可查的评分维度，推荐 Skill 不提交分数。历史 catalog 分析包的八轴仅按其旧契约兼容，不得反向写进 `sourced_tags_v1`。

排序权重补充：

{{RANKING_WEIGHTS}}

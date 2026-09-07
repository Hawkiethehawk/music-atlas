# 候选排序提示词插槽

不要填写 score_features。为每个候选填写可审计的 style_mix、八维 style_axes、analysis_refs、evidence_grade 和 evidence_items；程序会从这些结构化事实计算全部七项分数。

不要填写 resolved_route、matched_interest_id 或 program_explanation，它们由程序生成。风格与听感匹配同一兴趣组；规则分数不是喜欢概率。analysis_refs 仅作引用索引，不作为关系或频率加分证据。

排序时同时考虑：

- 细分风格匹配
- 八维听感轴匹配
- 音乐人关系强度
- 当前艺人频率信号
- 新鲜度与探索价值
- 公开证据质量
- 公开关联信号

排序权重补充：

{{RANKING_WEIGHTS}}

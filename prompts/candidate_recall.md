# 候选召回提示词插槽

首轮候选池覆盖四类：艺人延伸、音乐人关系、细分风格邻近、探索候选，并为最终硬性配额预留足够数量。若有 research_request，则只补指定缺额或有助于满足约束的新候选，不重复首轮数量和全部类型要求。

分别承接 interest_profiles 中由本次收藏推导的兴趣组。艺人延伸与音乐人关系必须匹配当前分析包中的候选艺人端点；researched 关系来自分析 Agent，不等于已核验事实。单独填写 analysis_refs 或 relation_path 不能证明关系；无匹配路径时按风格邻近或探索研究。

每个候选必须同时提供 `track_identity` 与 `style` 两类 `evidence_items`（音乐人关系候选另需 `relation`），并把这些 URL 同时列进 `sources`；同时 `platform_links` 必须是至少含一个试听平台（如 `apple_music`、`netease`、`qq_music`）的非空对象。结构不完整的候选会被程序直接丢弃，不计入本轮配额。

候选优先级：

1. 当前喜欢艺人的未收录作品或新发行
2. 主唱、前成员、side project 和本次分析包中的关联项目
3. 共享具体细分风格或相近听感轴的作品
4. 有明确证据、但不与当前歌单完全同质的中尾部作品

召回比例补充：

{{RECALL_MIX}}

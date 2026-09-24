---
name: music-atlas-recommendation
description: 执行 Music Atlas Step 3 的受限候选研究，读取当前分析包并返回未排序候选事实；不负责评分、选曲、排序或任何发送。
metadata:
  short-description: Music Atlas 第三步候选 Skill
---

# Music Atlas Step 3

本 Skill 是模型和供应商无关的候选事实提取层。执行环境可以自行选择模型、工具和检索方式，但 Skill 不要求或假设任何特定模型、厂商、SDK、联网方式或账号。

## 工作范围

1. 唯一偏好输入是当前 `MusicianAnalysisPacket` 及任务中明确给出的候选研究约束。
2. 研究公开来源支持的候选歌曲身份、发行项目、风格、关系路径和平台链接。
3. 返回未排序的 `candidate_pool`，供程序后续校验和确定性排序。
4. 候选必须属于四类之一：`artist_continuation`、`musician_relation`、`style_neighbor`、`exploration`。
5. 证据不足时不提交候选，或返回明确的 `insufficient_evidence`，不得用猜测凑数量。

## 硬约束

- 不读取原始歌单、登录态、播放历史、历史推荐、上一轮结果或个性化推荐页面。
- 不命中当前 `favorite_track_keys` 或相同平台歌曲 ID。
- 每个候选必须有稳定歌曲身份、风格画像、分析引用和公开证据。
- 关系候选必须能在当前分析包中找到对应的关系路径；不能把 Skill 自己猜测的关系当成已确认关系。
- 只输出 `candidate_pool` 阶段；不得输出 `recommendations`、排名、分数、配额完成情况、MMR、排序顺序、最终说明或发布状态。
- 不改变推荐策略，不降低覆盖率门槛，不绕过艺人/项目上限。
- 结果交给程序校验；来源模型由程序计算六项有据可查的评分维度、路由、去重、召回配额、项目覆盖、MMR、多样性和顺序。只有显式历史 catalog 分析包仍按旧字段契约处理，不能把八轴混入来源模型。

## 输出

严格按照 [recommendation-contract.md](references/recommendation-contract.md) 返回一个 JSON 对象。没有公开来源访问能力时，应返回证据不足，而不是编造来源。

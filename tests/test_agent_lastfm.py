import unittest
import json
from unittest.mock import patch
from copy import deepcopy
from tempfile import TemporaryDirectory
from contracts import ContractError
from agent_lastfm import validate_islands,curate,analyze,validate_overall_summary,validate_candidate_copy,validate_copy

SUMMARY='整体以现代重型与另类摇滚为底色，融合 Shoegaze、Emo、电子与嘻哈元素，气质冷峻、朦胧，带些浪漫。不同风格交织出夜色、霓虹与雾气般的画面，从克制到浓烈的审美对照贯穿始终，呈现开阔而鲜明的现代另类取向。'
DETAILS={'preference_basis':'现代重型和另类摇滚构成这片兴趣岛的主要风格底色。','music_fit':'候选艺人的另类摇滚标签与岛内已出现的风格有所交集。','novelty':'沿相似艺人方向拓宽收藏，比较同一风格的不同表达。','listening_tip':'建议对照熟悉的作品，留意听感的异同再决定是否收藏。'}
class AgentLastFMTests(unittest.TestCase):
 def setUp(self):
  self.groups=[{'name':str(i),'summary':'source supported','record_ids':[i]} for i in range(3)]
  self.directory=TemporaryDirectory();self.addCleanup(self.directory.cleanup)
 def test_three_groups_complete(self):
  self.assertEqual(len(validate_islands({'islands':self.groups},[{}, {}, {}])),3)
 def test_missing_duplicate_or_extra_groups_rejected(self):
  for groups in [self.groups[:2],self.groups+[self.groups[0]], [self.groups[0],self.groups[0],self.groups[2]]]:
   with self.assertRaises(ContractError):validate_islands({'islands':groups},[{}, {}, {}])
 def test_extra_island_feedback_preserves_its_record_ids(self):
  groups=deepcopy(self.groups)+[{'name':'extra','summary':'source supported','record_ids':[3]}]
  with self.assertRaisesRegex(ContractError,r'当前返回 4 个.*\[3\]'):
   validate_islands({'islands':groups},[{}, {}, {}, {}])
 def test_agent_cannot_invent_catalog_candidate(self):
  packet={'agent_islands':[{'id':'a'}],'recommendation_policy':{}}
  result={'candidates':[{'id':'fiction','island_id':'a','reason':'x','details':DETAILS}]}
  with patch('agent_lastfm.invoke',return_value=result),self.assertRaises(ContractError):curate(packet,[],None,10,self.directory.name)
 def test_agent_order_and_reason_preserved(self):
  packet={'agent_islands':[{'id':'a'}],'recommendation_policy':{}}
  c={'canonical_track_id':'real','title':'T','artist':'A','project':'P','provider_similarity':{}}
  result={'candidates':[{'id':'real','island_id':'a','reason':'source-based reason','details':DETAILS}]}
  with patch('agent_lastfm.invoke',return_value=result):out=curate(packet,[c],None,10,self.directory.name)
  self.assertEqual(out[0]['agent_reason'],'source-based reason')
  self.assertEqual(out[0]['matched_interest_id'],'a')
  self.assertEqual(out[0]['agent_details'],DETAILS)
 def test_unknown_and_duplicate_candidate_rows_are_discarded(self):
  packet={'agent_islands':[{'id':'a'}],'recommendation_policy':{}}
  c={'canonical_track_id':'real','title':'T','artist':'A','project':'P','provider_similarity':{}}
  valid={'id':'real','island_id':'a','reason':'从另类摇滚的边缘切入，适合比较相近审美的不同表达。','details':DETAILS}
  unknown={**valid,'id':'invented'}
  result={'candidates':[valid,unknown,valid]}
  with patch('agent_lastfm.invoke',return_value=result):out=curate(packet,[c],None,10,self.directory.name)
  self.assertEqual([item['canonical_track_id'] for item in out],['real'])
 def test_required_candidate_copy_may_be_beyond_formal_window(self):
  packet={'agent_islands':[{'id':'a'}],'recommendation_policy':{'target_recommendations':1}}
  regular={'canonical_track_id':'regular','title':'T1','artist':'A1','project':'P1','candidate_type':'style_neighbor','provider_similarity':{}}
  exploration={'canonical_track_id':'far','title':'T2','artist':'A2','project':'P2','candidate_type':'exploration','provider_similarity':{}}
  result={'candidates':[{'id':'regular','island_id':'a','reason':'从另类摇滚的边缘切入，适合比较相近审美的不同表达。','details':DETAILS},
                        {'id':'far','island_id':'a','reason':'沿公开相似路径向外一步，适合作为本期的探索入口。','details':DETAILS}]}
  with patch('agent_lastfm.invoke',return_value=result):
   out=curate(packet,[regular,exploration],None,10,self.directory.name)
  self.assertEqual([item['canonical_track_id'] for item in out],['regular','far'])
 def test_analysis_withholds_names_and_persists_overall_summary(self):
  packet={'favorite_tracks':[{'title':'Secret Song','artist':'Secret Artist'} for _ in range(3)],'source_tags':{'records':[{'scope':'artist','tags':[{'tag':'rock'}]} for _ in range(3)]}}
  with patch('agent_lastfm.invoke',return_value={'islands':self.groups,'overall_summary':SUMMARY}) as call:
   analyze(packet,None,10,self.directory.name)
  self.assertNotIn('Secret',str(call.call_args.args[1]))
  self.assertEqual(packet['overall_summary'],SUMMARY)
  self.assertEqual(packet['agent_copy_version'],1)
 def test_summary_rejects_names_brands_and_disallowed_words(self):
  for text in [SUMMARY+' Secret Song',SUMMARY+' SECRET ARTIST',SUMMARY+' Last.fm',SUMMARY+'力量感力量感',SUMMARY+'孤独孤独']:
   with self.subTest(text=text),self.assertRaises(ContractError):
    validate_overall_summary(text,[{'title':'Secret Song','artist':'Secret Artist'}])
  validate_overall_summary(SUMMARY,[{'title':'Core','artist':'A'}])
 def test_one_vague_word_with_music_context_is_allowed(self):
  self.assertEqual(validate_copy('以电子音墙铺开，氛围感只作为空间层次的补充。'), '以电子音墙铺开，氛围感只作为空间层次的补充。')
  with self.assertRaisesRegex(ContractError,'氛围感'):
   validate_copy('整体朦胧，氛围感明显，但没有具体风格或音乐语境。')
 def test_incomplete_or_repeated_agent_detail_rejected(self):
  for details in [None,{},dict.fromkeys(DETAILS,'候选艺人的另类摇滚标签与岛内已出现的风格有所交集。')]:
   with self.assertRaises(ContractError):validate_candidate_copy({'agent_reason':'source-based reason','agent_details':details})
 def test_missing_assignment_repaired_once_with_feedback(self):
  packet={'favorite_tracks':[{'title':'Secret Song','artist':'Secret Artist'} for _ in range(3)],'source_tags':{'records':[{'scope':'artist','tags':[]} for _ in range(3)]}}
  missing=deepcopy(self.groups);missing[2]['record_ids']=[]
  with patch('agent_lastfm.invoke',side_effect=[{'overall_summary':SUMMARY,'islands':missing},{'overall_summary':SUMMARY,'islands':self.groups}]) as call:
   analyze(packet,None,10,self.directory.name)
  self.assertEqual(call.call_count,2)
  self.assertIn('[2]',call.call_args.args[1]['repair']['error'])
  self.assertEqual(call.call_args.args[1]['repair']['attempt'],2)
  self.assertEqual(len(packet['agent_islands']),3)
 def test_repair_cannot_relax_contract_or_start_recommendation(self):
  packet={'favorite_tracks':[{'title':'Secret Song','artist':'Secret Artist'} for _ in range(3)],'source_tags':{'records':[{'scope':'artist','tags':[]} for _ in range(3)]}}
  with patch('agent_lastfm.invoke',return_value={'overall_summary':SUMMARY,'islands':self.groups[:2]}) as call,self.assertRaises(ContractError):
   analyze(packet,None,10,self.directory.name)
  self.assertEqual(call.call_count,3)
  self.assertNotIn('agent_islands',packet)
 def test_copy_rejection_regenerates_and_reports_field(self):
  packet={'favorite_tracks':[{'title':'Secret Song','artist':'Secret Artist'} for _ in range(3)],'source_tags':{'records':[{'scope':'artist','tags':[{'tag':'rock'}]} for _ in range(3)]}}
  invalid=deepcopy(self.groups); invalid[1]['summary']='这段文案氛围感明显，但没有具体音乐语境。'
  valid=deepcopy(self.groups); valid[1]['summary']='以摇滚音墙和宽阔空间铺开，层次由克制逐步推向开阔。'
  events=[]
  with patch('agent_lastfm.invoke',side_effect=[{'overall_summary':SUMMARY,'islands':invalid},{'overall_summary':SUMMARY,'islands':valid}]) as call:
   analyze(packet,None,10,self.directory.name,lambda attempt,feedback: events.append((attempt,feedback)))
  self.assertEqual(call.call_count,2)
  self.assertEqual(events[0][0],2)
  self.assertIn('islands[1].summary',events[0][1])
  self.assertEqual(call.call_args.args[1]['repair']['attempt'],2)
  self.assertEqual(len(packet['agent_islands']),3)
 def test_recommendation_copy_rejection_regenerates(self):
  packet={'agent_islands':[{'id':'a','name':'a','summary':'rock','record_ids':[0]}],'recommendation_policy':{'target_recommendations':1}}
  candidate={'canonical_track_id':'real','title':'T','artist':'A','project':'P','provider_similarity':{},'style_evidence':{}}
  bad={'candidates':[{'id':'real','island_id':'a','reason':'力量感力量感，这是一条过长的推荐说明。','details':DETAILS}]}
  good={'candidates':[{'id':'real','island_id':'a','reason':'从另类摇滚的边缘切入，适合继续比较同一审美中的不同表达。','details':DETAILS}]}
  events=[]
  with patch('agent_lastfm.invoke',side_effect=[bad,good]) as call:
   out=curate(packet,[candidate],None,10,self.directory.name,lambda attempt,feedback: events.append((attempt,feedback)))
  self.assertEqual(call.call_count,2)
  self.assertIn('candidates[0].reason',events[0][1])
  self.assertEqual(out[0]['canonical_track_id'],'real')
 def test_recommendation_copy_feedback_aggregates_all_short_fields(self):
  packet={'agent_islands':[{'id':'a','name':'a','summary':'rock','record_ids':[0,1]}],'recommendation_policy':{'target_recommendations':2}}
  candidates=[
   {'canonical_track_id':'one','title':'T1','artist':'A1','project':'P1','provider_similarity':{},'style_evidence':{}},
   {'canonical_track_id':'two','title':'T2','artist':'A2','project':'P2','provider_similarity':{},'style_evidence':{}},
  ]
  short={'preference_basis':'太短','music_fit':'太短','novelty':'太短','listening_tip':'太短'}
  bad={'candidates':[{'id':'one','island_id':'a','reason':'太短','details':short},
                     {'id':'two','island_id':'a','reason':'太短','details':short}]}
  good_details={key:value+'，用于说明具体风格连接和比较方向。' for key,value in DETAILS.items()}
  good={'candidates':[{'id':'one','island_id':'a','reason':'从另类摇滚的边缘切入，适合继续比较同一审美中的不同表达。','details':good_details},
                      {'id':'two','island_id':'a','reason':'沿公开相似路径向外一步，适合作为本期的探索入口。','details':{key:value+'，用于说明具体风格连接和比较方向。' for key,value in DETAILS.items()}}]}
  events=[]
  with patch('agent_lastfm.invoke',side_effect=[bad,good]) as call:
   out=curate(packet,candidates,None,10,self.directory.name,lambda attempt,feedback: events.append((attempt,feedback)))
  self.assertEqual(call.call_count,2)
  self.assertIn('candidates[0].reason',events[0][1])
  self.assertIn('candidates[0].details.preference_basis',events[0][1])
  self.assertIn('candidates[1].reason',events[0][1])
  self.assertIn('candidates[1].details.listening_tip',events[0][1])
  self.assertIn('一次性检查并修复',call.call_args.args[1]['repair']['required_change'])
  self.assertEqual(len(out),2)

 def test_recommendation_count_shortfall_regenerates_from_full_catalog(self):
  packet={'agent_islands':[{'id':'a','name':'a','summary':'rock','record_ids':[0]}],'recommendation_policy':{'target_recommendations':1}}
  candidates=[
   {'canonical_track_id':'one','title':'T1','artist':'A1','project':'P1','provider_similarity':{},'style_evidence':{}},
   {'canonical_track_id':'two','title':'T2','artist':'A2','project':'P2','provider_similarity':{},'style_evidence':{}},
  ]
  valid=lambda cid: {'id':cid,'island_id':'a','reason':'从另类摇滚的边缘切入，适合继续比较同一审美中的不同表达。','details':DETAILS}
  bad={'candidates':[valid('one'),valid('one')]}
  good={'candidates':[valid('one'),valid('two')]}
  events=[]
  with patch('agent_lastfm.invoke',side_effect=[bad,good]) as call:
   out=curate(packet,candidates,None,10,self.directory.name,lambda attempt,feedback: events.append((attempt,feedback)))
  self.assertEqual(call.call_count,2)
  self.assertIn('至少需要 2 首',events[0][1])
  self.assertIn('去重后候选数量不足',call.call_args.args[1]['repair']['required_change'])
  self.assertEqual([item['canonical_track_id'] for item in out],['one','two'])

 def test_execution_timeout_retries_within_shared_budget(self):
  packet={'favorite_tracks':[{'title':'Secret Song','artist':'Secret Artist'} for _ in range(3)],'source_tags':{'records':[{'scope':'artist','tags':[]} for _ in range(3)]}}
  valid={'overall_summary':SUMMARY,'islands':self.groups}
  events=[]
  with patch('agent_lastfm.time.monotonic',side_effect=[0,0,151,152]), patch('agent_lastfm.invoke',side_effect=[ContractError('Agent 执行超时：150 秒'),valid]) as call:
   analyze(packet,None,180,self.directory.name,lambda attempt,feedback: events.append((attempt,feedback)))
  self.assertEqual([c.args[3] for c in call.call_args_list],[150,29])
  self.assertEqual(events,[(2,'Agent 执行超时：150 秒')])
  self.assertEqual(call.call_args.args[1]['retry']['attempt'],2)
  self.assertEqual(len(packet['agent_islands']),3)
 def test_regeneration_uses_one_shared_time_budget(self):
  bad={'overall_summary':SUMMARY,'islands':self.groups[:2]}
  good={'overall_summary':SUMMARY,'islands':self.groups}
  with patch('agent_lastfm.time.monotonic',side_effect=[0,0,2,5,7]), patch('agent_lastfm.invoke',side_effect=[bad,bad,good]) as call:
   packet={'favorite_tracks':[{'title':'Secret Song','artist':'Secret Artist'} for _ in range(3)],'source_tags':{'records':[{'scope':'artist','tags':[]} for _ in range(3)]}}
   analyze(packet,None,10,self.directory.name)
  self.assertEqual([c.args[3] for c in call.call_args_list],[10,8,5])
 def test_failed_regeneration_writes_complete_telemetry(self):
  packet={'favorite_tracks':[{'title':'Secret Song','artist':'Secret Artist'} for _ in range(3)],'source_tags':{'records':[{'scope':'artist','tags':[]} for _ in range(3)]}}
  with patch('agent_lastfm.invoke',return_value={'overall_summary':SUMMARY,'islands':self.groups[:2]}),self.assertRaisesRegex(ContractError,'连续 3 次'):
   analyze(packet,None,10,self.directory.name)
  with open(self.directory.name+'/analysis_telemetry.json',encoding='utf-8') as stream:
   telemetry=json.loads(stream.read())
  self.assertEqual(telemetry['attempts'],3)
  self.assertEqual(telemetry['status'],'failed')
  self.assertEqual(len(telemetry['validation_errors']),3)
if __name__=='__main__':unittest.main()

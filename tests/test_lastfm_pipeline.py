import unittest
import os
import tempfile
from copy import deepcopy
from unittest.mock import patch
from lastfm_pipeline import LastFM, select, validate_bundle
from contracts import ContractError, track_key

class LastFMSelectionTests(unittest.TestCase):
    def setUp(self):
        self.packet={'primary_distribution':[{'artist':'Seed','entity_ref':'seed'}],'analysis_id':'a','playlist_exclusion':{'track_keys':[track_key('Old','A')],'platform_track_ids':['old']},
          'recommendation_policy':{'target_recommendations':10,'max_per_artist':1,'max_per_project':1}}
        self.pool=[]
        for i in range(4):
            fact={'title':str(i),'artist':'A' if i<2 else 'B','album':'Album','platform_track_id':str(i),'source':'netease','url':'https://music.163.com/song?id='+str(i)}
            self.pool.append({'canonical_track_id':'platform:netease:'+str(i),'analysis_refs':['seed'],'title':fact['title'],'artist':fact['artist'],'platform_track_id':str(i),'project':'Album','metadata_verified':fact,
                 'provider_similarity':{'seed':'Seed','artist':fact['artist'],'url':'https://www.last.fm/music/Seed/+similar'}})
        self.bundle={'schema_version':'2.0','bundle_type':'recommendation_bundle','analysis_id':'a','bundle_stage':'candidate_pool','candidate_pool':self.pool}
    def test_constraints_no_scores_and_shortfall(self):
        result=select(self.bundle,self.packet)
        self.assertEqual(len(result['recommendations']),2)
        self.assertEqual(result['ranking']['shortfall'],8)
        self.assertTrue(all('ranking_score' not in c and 'style_axes' not in c for c in result['recommendations']))
        validate_bundle(result,self.packet)
    def test_agent_islands_are_interleaved(self):
        self.packet['agent_islands']=[{'id':str(i)} for i in range(3)]
        self.packet['recommendation_policy']['target_recommendations']=3
        self.packet['recommendation_policy']['max_per_artist']=3
        self.packet['recommendation_policy']['max_per_project']=3
        for i,c in enumerate(self.pool):c['matched_interest_id']=str(max(0,i-1))
        result=select(self.bundle,self.packet)
        self.assertEqual([r['matched_interest_id'] for r in result['recommendations']],['0','1','2'])
    def test_exploration_is_kept_inside_target_window(self):
        self.packet['recommendation_policy']['target_recommendations']=2
        self.packet['recommendation_policy']['max_per_artist']=4
        self.packet['recommendation_policy']['max_per_project']=4
        for candidate in self.pool:candidate['candidate_type']='style_neighbor'
        self.pool[3]['candidate_type']='exploration'
        result=select(self.bundle,self.packet)
        self.assertEqual([r['candidate_type'] for r in result['recommendations']],['style_neighbor','exploration'])
    def test_relation_is_kept_inside_target_window(self):
        self.packet['recommendation_policy']['target_recommendations']=2
        self.packet['recommendation_policy']['max_per_artist']=4
        self.packet['recommendation_policy']['max_per_project']=4
        for candidate in self.pool:candidate['candidate_type']='style_neighbor'
        self.pool[3]['candidate_type']='musician_relation'
        result=select(self.bundle,self.packet)
        self.assertIn('musician_relation',[r['candidate_type'] for r in result['recommendations']])
    def test_complete_playlist_exclusion(self):
        self.packet['playlist_exclusion']['track_keys'].append(track_key('0','A'))
        with self.assertRaises(ContractError):validate_bundle(self.bundle,self.packet)
        self.assertNotIn('0',[c['title'] for c in select(self.bundle,self.packet)['recommendations']])
    def test_forged_identity_and_reordered_output(self):
        bad=deepcopy(self.bundle);bad['candidate_pool'][0]['artist']='Other'
        with self.assertRaises(ContractError):validate_bundle(bad,self.packet)
        result=select(self.bundle,self.packet);result['recommendations'].reverse()
        with self.assertRaises(ContractError):validate_bundle(result,self.packet)

class LastFMTagsTests(unittest.TestCase):
    def test_non_genre_tags_fall_back_and_keep_scope(self):
        import threading
        from lastfm_pipeline import collect_tags,validate_knowledge
        class Client:
            local=threading.local()
            events=[]
            def call(self,method,**params):
                self.local.retrieved_at='2026-09-16T00:00:00Z'
                return {'toptags':{'tag':[{'name':'seen live' if method!='artist.getTopTags' else 'metalcore'}]}}
        packet={'style_analysis':{'style_definitions':[]},'favorite_tracks':[{'title':'Song','artist':'Artist','album':'Album'}]}
        packet['source_tags']=collect_tags(packet,Client())
        validate_knowledge(packet)
        record=packet['source_tags']['records'][0]
        self.assertEqual(record['scope'],'artist')
        self.assertEqual(record['tags'],[{'tag':'metalcore','style_ref':None}])
    def test_unavailable_tags_remain_unknown(self):
        import threading
        from lastfm_pipeline import collect_tags,validate_knowledge
        class Client:
            local=threading.local()
            events=[]
            def call(self,*args,**params):return {}
        packet={'style_analysis':{'style_definitions':[]},'favorite_tracks':[{'title':'Song','artist':'Artist'}]}
        packet['source_tags']=collect_tags(packet,Client())
        validate_knowledge(packet)
        r=packet['source_tags']['records'][0]
        self.assertEqual(r['scope'],'unknown')
        self.assertEqual(r['tags'],[])
        self.assertIsNone(r['retrieved_at'])
        packet['source_tags']['records']=[]
        with self.assertRaises(ContractError):validate_knowledge(packet)


class LastFMKeyConfigTests(unittest.TestCase):
    def test_server_environment_key_is_used_when_keyring_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LASTFM_API_KEY': 'env-key'}), patch(
            'lastfm_pipeline._load_keyring', side_effect=RuntimeError('no desktop keyring')
        ):
            client = LastFM(directory, seconds=1)
        self.assertEqual(client.key, 'env-key')

if __name__=='__main__':unittest.main()

import unittest
import os
import tempfile
from copy import deepcopy
from unittest.mock import patch
from lastfm_pipeline import LastFM, artist_url, discover, select, validate_bundle
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
    def test_first_run_prefetch_source_casing_preserves_origin_and_identity(self):
        # The source itself is immutable; the final Step 2 packet may only
        # canonicalize the displayed capitalization of the seed artist.
        self.packet['primary_distribution']=[
            {'artist':'The Plot in You','entity_ref':'artist:theplotinyou'}]
        candidate=deepcopy(self.pool[0])
        candidate['analysis_refs']=['artist:theplotinyou']
        candidate['provider_similarity']={
            'seed':'The Plot In You','artist':candidate['artist'],
            'url':artist_url('The Plot In You')+'/+similar'}
        self.bundle['candidate_pool']=[candidate]
        validate_bundle(self.bundle,self.packet)

        wrong_source=deepcopy(self.bundle)
        wrong_source['candidate_pool'][0]['provider_similarity']['url']=artist_url('The Plot in You')+'/+similar'
        with self.assertRaisesRegex(ContractError,'种子来源无效'):
            validate_bundle(wrong_source,self.packet)
        wrong_ref=deepcopy(self.bundle)
        wrong_ref['candidate_pool'][0]['analysis_refs']=['artist:other']
        with self.assertRaisesRegex(ContractError,'艺人来源不一致'):
            validate_bundle(wrong_ref,self.packet)
        wrong_seed=deepcopy(self.bundle)
        wrong_seed['candidate_pool'][0]['provider_similarity']['seed']='Other'
        wrong_seed['candidate_pool'][0]['provider_similarity']['url']=artist_url('Other')+'/+similar'
        with self.assertRaisesRegex(ContractError,'种子来源无效'):
            validate_bundle(wrong_seed,self.packet)

        relation=deepcopy(self.bundle)
        row=relation['candidate_pool'][0]
        row['candidate_type']='musician_relation'
        row['provider_similarity']={}
        row['provider_relation']={
            'seed':'The Plot In You','artist':row['artist'],'person':'Member',
            'url':'https://musicbrainz.org/artist/relation',
            'sources':['https://musicbrainz.org/artist/relation']}
        row['evidence_items']=[{'claim_type':'relation','url':row['provider_relation']['url']}]
        validate_bundle(relation,self.packet)
    def test_strict_mix_selects_exact_types(self):
        mix=[('style_neighbor',4),('artist_continuation',3),('musician_relation',2),('exploration',1)]
        self.packet['strict_recall_mix']=True
        self.packet['recommendation_policy']['recall_mix']=[
            {'candidate_type':kind,'target_ratio':count/10} for kind,count in mix]
        pool=[]
        for index,kind in enumerate(kind for kind,count in mix for _ in range(count)):
            candidate=deepcopy(self.pool[0]);candidate.update(
                candidate_type=kind,canonical_track_id=f'platform:netease:strict-{index}',
                title=f'Strict {index}',artist=f'Artist {index}',project=f'Album {index}',
                platform_track_id=f'strict-{index}')
            pool.append(candidate)
        self.bundle['candidate_pool']=pool
        ranked=select(self.bundle,self.packet)
        self.assertEqual(ranked['ranking']['algorithm_version'],'lastfm_constraints_v1_strict_mix')
        self.assertEqual({kind:sum(r['candidate_type']==kind for r in ranked['recommendations'])
                          for kind,_ in mix},dict(mix))
        self.bundle['candidate_pool']=pool[:-1]
        with self.assertRaisesRegex(ContractError,'探索推荐 0/1'):
            select(self.bundle,self.packet)
    def test_strict_mix_reassigns_early_choice_when_artist_capacity_conflicts(self):
        # The first exploration choice occupies an artist needed by relation;
        # the second exploration choice makes all four quotas feasible.
        mix=[('style_neighbor',4),('artist_continuation',3),
             ('musician_relation',2),('exploration',1)]
        self.packet['strict_recall_mix']=True
        self.packet['recommendation_policy'].update(target_recommendations=10,
            recall_mix=[{'candidate_type':kind,'target_ratio':count/10}
                        for kind,count in mix])
        kinds=[kind for kind,count in mix for _ in range(count)]
        artists=[*(f'Style {i}' for i in range(4)),
                 *(f'Continuation {i}' for i in range(3)),
                 'Relation A','Relation B','Relation B',
                 'Relation A','Exploration only']
        kinds.insert(9,'musician_relation')
        kinds.extend(['exploration','exploration'])
        pool=[]
        for index,(kind,artist) in enumerate(zip(kinds,artists)):
            candidate=deepcopy(self.pool[0]);candidate.update(
                candidate_type=kind,canonical_track_id=f'platform:netease:flow-{index}',
                title=f'Flow {index}',artist=artist,project=f'Album {index}',
                platform_track_id=f'flow-{index}')
            pool.append(candidate)
        self.bundle['candidate_pool']=pool
        ranked=select(self.bundle,self.packet)
        self.assertEqual(len(ranked['recommendations']),10)
        self.assertEqual({kind:sum(r['candidate_type']==kind for r in ranked['recommendations'])
                          for kind,_ in mix},dict(mix))
        self.assertIn('flow-11',ranked['ranking']['selected_canonical_track_ids'][-1])
        self.assertNotIn('platform:netease:flow-10',ranked['ranking']['selected_canonical_track_ids'])

    def test_agent_copy_never_upgrades_artist_tags_to_track_claims(self):
        self.packet['agent_copy_version']=1
        candidate=self.pool[0]
        candidate['candidate_type']='style_neighbor'
        candidate['agent_reason']='这首歌有重型鼓点和失真吉他的实测风格。'
        candidate['agent_details']={
            'preference_basis':'这首歌的失真和节奏变化与歌单完全一致。',
            'music_fit':'单曲呈现重型鼓点和现场感。',
            'novelty':'这首歌的副歌爆发带来新鲜感。',
            'listening_tip':'副歌鼓点会突然加速。'}
        candidate['style_evidence']={
            'scope':'artist','tags':[{'tag':'metalcore'}],
            'url':'https://www.last.fm/music/A',
            'retrieved_at':'2026-09-24T00:00:00Z','status':'supported',
            'evidence':[{'scope':'artist','subject':{'artist':'A'},
                'tags':[{'tag':'metalcore'}],
                'url':'https://www.last.fm/music/A',
                'retrieved_at':'2026-09-24T00:00:00Z','status':'supported'}]}
        ranked=select(self.bundle,self.packet)
        explanation=ranked['recommendations'][0]['program_explanation']
        self.assertIn('艺人标签',explanation['music_fit'])
        self.assertIn('不代表这首歌',explanation['music_fit'])
        self.assertNotIn('鼓点',str(explanation))
        self.assertNotIn('副歌',str(explanation))
        # A plausible Last.fm URL for a different artist is not enough.
        candidate['style_evidence']['evidence'][0]['subject']['artist']='Other'
        with patch('agent_lastfm.validate_candidate_copy',return_value={}):
            with self.assertRaisesRegex(ContractError,'候选风格标签与曲目'):
                validate_bundle(self.bundle,self.packet)

    def test_provider_artist_url_keeps_original_casing_after_platform_canonicalization(self):
        self.packet['agent_copy_version'] = 1
        candidate = self.pool[0]
        candidate['artist'] = 'Motionless In White'
        candidate['metadata_verified']['artist'] = 'Motionless In White'
        candidate['provider_similarity']['artist'] = 'Motionless In White'
        source_name = 'Motionless in White'
        url = artist_url(source_name)
        layer = {'scope': 'artist', 'subject': {'artist': source_name},
                 'identity_status': 'matched', 'status': 'supported',
                 'url': url, 'retrieved_at': '2026-09-24T00:00:00Z',
                 'tags': [{'tag': 'metalcore'}]}
        candidate['style_evidence'] = {**layer, 'evidence': [deepcopy(layer)]}
        with patch('agent_lastfm.validate_candidate_copy', return_value={}):
            validate_bundle(self.bundle, self.packet)
            candidate['style_evidence']['url'] = artist_url('Different')
            candidate['style_evidence']['evidence'][0]['url'] = artist_url('Different')
            with self.assertRaisesRegex(ContractError, '候选风格标签与曲目'):
                validate_bundle(self.bundle, self.packet)

    def test_agent_copy_reports_every_supported_scope_separately(self):
        self.packet['agent_copy_version']=1
        candidate=self.pool[0]
        candidate['agent_reason']='Agent 的未证实听感描述。'
        layers=[]
        for scope,tag,subject,url in [
            ('track','metalcore',{'artist':'A','track':'0'},'https://www.last.fm/music/A/_/0'),
            ('album','rock',{'artist':'A','album':'Album'},'https://www.last.fm/music/A/Album'),
            ('artist','metal',{'artist':'A'},'https://www.last.fm/music/A')]:
            layers.append({'scope':scope,'status':'supported','subject':subject,
                           'url':url,'retrieved_at':'2026-09-24T00:00:00Z',
                           'tags':[{'tag':tag}]})
        candidate['style_evidence']={**layers[0],'evidence':layers}
        explanation=select(self.bundle,self.packet)['recommendations'][0]['program_explanation']
        for fragment in ('单曲标签：metalcore','所属专辑标签：rock','艺人标签：metal'):
            self.assertIn(fragment,explanation['music_fit'])
        self.assertIn('专辑或艺人标签只提供背景',explanation['music_fit'])
    def test_three_groups_reject_missing_type_before_publication(self):
        from web_workflow import _build_atlas_groups
        self.packet['strict_recall_mix']=True
        self.packet['primary_distribution'][0]['count']=1
        self.packet['recommendation_policy']['recall_mix']=[
            {'candidate_type':kind,'target_ratio':ratio}
            for kind,ratio in [('style_neighbor',.4),('artist_continuation',.3),
                               ('musician_relation',.2),('exploration',.1)]]
        candidates=[]
        for index in range(30):
            candidate=deepcopy(self.pool[0]);candidate.update(
                canonical_track_id=f'platform:netease:three-{index}',
                title=f'Three {index}',artist=f'Artist {index}',
                platform_track_id=f'three-{index}',candidate_type='style_neighbor')
            candidates.append(candidate)
        with self.assertRaisesRegex(ContractError,'探索推荐 0/3'):
            _build_atlas_groups(candidates,self.packet)

    def test_bounded_discovery_reaches_distant_neighbors(self):
        class Client:
            events=[]
            def call(self,method,**params):
                if method=='artist.getSimilar':
                    return {'similarartists':{'artist':[{'name':f"{params['artist']}-neighbor-{n}",'match':'0.9'} for n in range(1,5)]}}
                if method=='artist.getTopTracks':
                    return {'toptracks':{'track':[{'name':f"Track {n}"} for n in range(1,4)]}}
                return {}
        packet={'primary_distribution':[{'artist':f'Anchor {n}','entity_ref':f'artist-{n}'} for n in range(16)],
                'playlist_exclusion':{'track_keys':[],'platform_track_ids':[]},
                'style_analysis':{'style_definitions':[]}}
        def verify(requested,**_kwargs):
            return [{'title':title,'artist':artist,'album':'Album','source':'netease',
                     'platform_track_id':str(n),'url':f'https://music.163.com/song?id={n}'}
                    for n,(title,artist) in enumerate(requested)]
        with patch('metadata_verify.verify_many',side_effect=verify), patch('lastfm_pipeline.collect_tags',return_value={'records':[{'scope':'unknown','tags':[],'url':None,'retrieved_at':None,'status':'unavailable'} for _ in range(40)]}):
            candidates,_=discover(packet,Client(),max_candidates=40,concurrency=4)
        self.assertEqual(len(candidates),40)
        self.assertGreaterEqual(sum((item.get('provider_similarity') or {}).get('rank',0)>1 for item in candidates),3)

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

    def test_transient_missing_track_and_album_are_retried_once_without_fabrication(self):
        import threading
        from lastfm_pipeline import collect_tags, validate_knowledge

        class Client:
            local = threading.local()
            events = []

            def __init__(self):
                self.calls = {}

            def call(self, method, **params):
                key = (method, tuple(sorted(params.items())))
                self.calls[key] = self.calls.get(key, 0) + 1
                self.local.retrieved_at = None
                # The first track/album request simulates the transient
                # provider failure that previously opened the global circuit.
                if method in ('track.getTopTags', 'album.getTopTags') and self.calls[key] == 1:
                    return {}
                if method == 'artist.getTopTags':
                    self.local.retrieved_at = '2026-09-24T00:00:00Z'
                    return {'toptags': {'@attr': params, 'tag': [{'name': 'rock'}]}}
                self.local.retrieved_at = '2026-09-24T00:00:00Z'
                return {'toptags': {'@attr': params, 'tag': [{'name': 'metalcore'}]}}

        packet = {'style_analysis': {'style_definitions': []}, 'favorite_tracks': [
            {'title': 'Song', 'artist': 'Artist', 'album': 'Album'}]}
        client = Client()
        packet['source_tags'] = collect_tags(packet, client, concurrency=3)
        validate_knowledge(packet)
        record = packet['source_tags']['records'][0]
        self.assertEqual(record['scope'], 'track')
        self.assertEqual(packet['source_tags']['retry_request_count'], 2)
        self.assertEqual([layer['collection_pass'] for layer in record['evidence']],
                         ['retry', 'retry', 'initial'])
        self.assertTrue(all(layer['status'] == 'supported' for layer in record['evidence']))

    def test_retry_keeps_empty_public_response_unknown(self):
        import threading
        from lastfm_pipeline import collect_tags, validate_knowledge

        class Client:
            local = threading.local()
            events = []

            def call(self, method, **params):
                self.local.retrieved_at = None
                return {}

        packet = {'style_analysis': {'style_definitions': []}, 'favorite_tracks': [
            {'title': 'Song', 'artist': 'Artist', 'album': 'Album'}]}
        packet['source_tags'] = collect_tags(packet, Client(), concurrency=3)
        validate_knowledge(packet)
        record = packet['source_tags']['records'][0]
        self.assertEqual(record['scope'], 'unknown')
        self.assertEqual(record['tags'], [])
        self.assertEqual(packet['source_tags']['retry_request_count'], 2)

    def test_all_scopes_retained_and_duplicate_album_artist_queried_once(self):
        import threading
        from lastfm_pipeline import collect_tags, validate_knowledge, attach_style_evidence
        class Client:
            local = threading.local()
            events = []
            def __init__(self):
                self.calls = []
                self.lock = threading.Lock()
            def call(self, method, **params):
                with self.lock:
                    self.calls.append((method, params.copy()))
                self.local.retrieved_at = '2026-09-24T00:00:00Z'
                name = {'track': params.get('track'), 'album': params.get('album'),
                        'artist': params.get('artist')}[method.split('.')[0]]
                tag = ('rock' if name == 'First' else 'jazz' if name == 'One'
                       else 'folk' if name == 'A' else 'seen live')
                return {'toptags': {'@attr': params, 'tag': [{'name': tag}]}}
        packet = {'style_analysis': {'style_definitions': []}, 'favorite_tracks': [
            {'title': 'First', 'artist': 'A', 'album': 'One'},
            {'title': 'Second', 'artist': 'A', 'album': 'One'}]}
        client = Client()
        packet['source_tags'] = collect_tags(packet, client, concurrency=3)
        validate_knowledge(packet)
        self.assertEqual(len(client.calls), 4)  # Two tracks + one album + one artist.
        first, second = packet['source_tags']['records']
        self.assertEqual((first['scope'], second['scope']), ('track', 'album'))
        self.assertEqual([layer['scope'] for layer in first['evidence']],
                         ['track', 'album', 'artist'])
        self.assertEqual(second['evidence'][0]['status'], 'no_style_tags')
        self.assertTrue(all(layer['identity_status'] == 'matched'
                            for layer in first['evidence']))
        candidate = {'sources': ['https://music.163.com/song?id=1'], 'evidence_items': []}
        attach_style_evidence(candidate, first)
        self.assertEqual(len(candidate['evidence_items']), 3)
        self.assertEqual([item['scope'] for item in candidate['evidence_items']],
                         ['track', 'album', 'artist'])
        self.assertEqual(len(candidate['sources']), 4)

    def test_mismatched_response_identity_cannot_be_style_evidence(self):
        import threading
        from lastfm_pipeline import collect_tags, validate_knowledge
        class Client:
            local = threading.local()
            events = []
            def call(self, method, **params):
                self.local.retrieved_at = '2026-09-24T00:00:00Z'
                if method == 'track.getTopTags':
                    return {'toptags': {'@attr': {**params, 'track': 'Other'},
                                        'tag': [{'name': 'metalcore'}]}}
                return {'toptags': {'@attr': params, 'tag': [{'name': 'folk'}]
                                     if method == 'artist.getTopTags' else []}}
        packet = {'style_analysis': {'style_definitions': []}, 'favorite_tracks': [
            {'title': 'Track', 'artist': 'A', 'album': 'One'}]}
        packet['source_tags'] = collect_tags(packet, Client())
        validate_knowledge(packet)
        record = packet['source_tags']['records'][0]
        self.assertEqual(record['scope'], 'artist')
        self.assertEqual(record['evidence'][0]['status'], 'identity_mismatch')
        self.assertEqual(record['evidence'][0]['tags'], [])
        packet['source_tags']['records'][0]['evidence'][0]['status'] = 'supported'
        with self.assertRaises(ContractError):
            validate_knowledge(packet)

    def test_artist_only_mode_never_requests_tracks_or_albums(self):
        import threading
        from lastfm_pipeline import collect_artist_tags
        class Client:
            local = threading.local()
            events = []
            calls = []
            def call(self, method, **params):
                self.calls.append((method, params.copy()))
                self.local.retrieved_at = '2026-09-24T00:00:00Z'
                return {'toptags': {'@attr': {'artist': params['artist']},
                                    'tag': [{'name': 'rock'}]}}
        packet = {'style_analysis': {'style_definitions': []}, 'primary_distribution': [
            {'artist': 'A', 'count': 30}, {'artist': 'a', 'count': 2},
            {'artist': 'B', 'count': 1}]}
        client = Client()
        knowledge = collect_artist_tags(packet, client)
        self.assertEqual([method for method, _ in client.calls],
                         ['artist.getTopTags', 'artist.getTopTags'])
        self.assertEqual([record['count'] for record in knowledge['artist_records']], [32, 1])
        client.calls.clear()
        direct = collect_artist_tags(packet['primary_distribution'], client, style_definitions=[])
        self.assertEqual(direct['artist_records'], knowledge['artist_records'])

    def test_artist_budget_maps_unqueried_tracks_without_track_requests(self):
        import threading
        from lastfm_pipeline import collect_artist_tags, validate_knowledge
        class Client:
            local = threading.local()
            events = []
            calls = []
            def call(self, method, **params):
                self.calls.append((method, params.copy()))
                self.local.retrieved_at = '2026-09-24T00:00:00Z'
                return {'toptags': {'@attr': params, 'tag': [{'name': 'rock'}]}}
        packet = {'style_analysis': {'style_definitions': []},
                  'primary_distribution': [{'artist': 'A', 'count': 3},
                                           {'artist': 'B', 'count': 1}],
                  'favorite_tracks': [{'title': 'One', 'artist': 'A'},
                                      {'title': 'Two', 'artist': 'B'}]}
        client = Client()
        packet['source_tags'] = collect_artist_tags(packet, client, max_artists=1)
        validate_knowledge(packet)
        self.assertEqual([method for method, _ in client.calls], ['artist.getTopTags'])
        self.assertEqual(packet['source_tags']['budget_omitted_count'], 1)
        self.assertEqual([record['scope'] for record in packet['source_tags']['records']],
                         ['artist', 'unknown'])
        self.assertEqual(packet['source_tags']['records'][1]['evidence'][0]['status'],
                         'not_queried_budget')


class LastFMKeyConfigTests(unittest.TestCase):
    def test_server_environment_key_is_used_when_keyring_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LASTFM_API_KEY': 'env-key'}), patch(
            'lastfm_pipeline._load_keyring', side_effect=RuntimeError('no desktop keyring')
        ):
            client = LastFM(directory, seconds=1)
        self.assertEqual(client.key, 'env-key')

if __name__=='__main__':unittest.main()

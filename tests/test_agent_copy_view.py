import unittest
from copy import deepcopy
from unittest.mock import patch

from web_view_model import build_web_payload
from tests.test_agent_lastfm import SUMMARY, DETAILS
from tests import test_lastfm_pipeline
from lastfm_pipeline import select, validate_bundle
from contracts import ContractError


class AgentCopyViewTests(unittest.TestCase):
    def setUp(self):
        fixture=test_lastfm_pipeline.LastFMSelectionTests();fixture.setUp()
        self.packet=fixture.packet
        self.packet.update(agent_copy_version=1,overall_summary=SUMMARY,selection_mode='lastfm_constraints_v1',
            favorite_tracks=[{'title':'Old','artist':'A'}],source_tags={'records':[{'scope':'artist','tags':[]}]},
            agent_islands=[{'id':'agent-island-'+str(i+1),'name':'风格岛'+str(i),'summary':'风格归纳','record_ids':[0] if i==0 else []} for i in range(3)],
            style_analysis={})
        self.bundle=fixture.bundle
        for candidate in self.bundle['candidate_pool']:
            candidate.update(matched_interest_id='agent-island-1',agent_reason='沿现代另类摇滚方向延伸，探索熟悉风格的另一种表达。',agent_details=deepcopy(DETAILS))

    def test_summary_and_distinct_details_projected_without_process_boilerplate(self):
        ranked=select(self.bundle,self.packet)
        with patch('web_view_model.rank_bundle',return_value=ranked):
            payload=build_web_payload({'track_count':1},self.packet,ranked,editorial={'lede':'旧的流程说明'})
        self.assertEqual(payload['issue']['lede'],SUMMARY)
        rec=payload['recommendations'][0]
        self.assertEqual(rec['details'],DETAILS)
        self.assertNotIn('核验',rec['why'])
        self.assertNotIn('Last.fm',str([rec['route'],rec['evidence']]))
        self.assertEqual(len(payload['interests']),3)
        self.assertEqual(payload['status']['analysis'],'complete')
        self.assertFalse(payload['status']['profile_coverage_degraded'])
        self.assertEqual(payload['analysis']['classifiedTrackCount'],1)
        self.assertEqual(payload['analysis']['unclassifiedTrackCount'],0)
        self.assertEqual(payload['analysis']['profileCoverage']['mode'],'agent_islands')

    def test_detail_tampering_and_missing_details_fail_bundle_validation(self):
        validate_bundle(select(self.bundle,self.packet),self.packet)
        bad=deepcopy(self.bundle);bad['candidate_pool'][0].pop('agent_details')
        with self.assertRaises(ContractError):validate_bundle(bad,self.packet)
        bad=select(self.bundle,self.packet)
        bad['recommendations'][0]['program_explanation']['novelty']='篡改详情'
        with self.assertRaises(ContractError):validate_bundle(bad,self.packet)

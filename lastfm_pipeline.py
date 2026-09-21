"""Last.fm public labels and provider-ranked discovery; no inferred audio axes."""
from __future__ import annotations
import hashlib
import json
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from contracts import ContractError, track_key, normalized_name, utc_now, STYLE_AXIS_IDS, STYLE_AXIS_IDS
from secret_store import _load_keyring, SERVICE_NAME

class LastFM:
    def __init__(self, cache_dir, seconds=90):
        try:
            ring, _ = _load_keyring()
            self.key = ring.get_password(SERVICE_NAME, 'lastfm_api_key')
        except Exception:  # noqa: BLE001 - 无桌面的服务器使用受保护的环境变量回退
            self.key = None
        self.key = self.key or os.environ.get('LASTFM_API_KEY')
        if not self.key: raise ContractError('音乐资料服务密钥未配置')
        self.cache = Path(cache_dir)
        self.deadline = time.monotonic() + seconds
        self.events = []
        self.failures = 0
        self.lock = threading.RLock()
        self.request_locks = {}
        self.local = threading.local()

    def call(self, method, **params):
        key = json.dumps([method, params], sort_keys=True)
        with self.lock:
            gate = self.request_locks.setdefault(key, threading.Lock())
        with gate:
            self.local.retrieved_at = None
            return self._call(method, **params)

    def _call(self, method, **params):
        public = {'method': method, **params, 'format': 'json'}
        digest = hashlib.sha256(json.dumps(public,sort_keys=True).encode()).hexdigest()
        path = self.cache / (digest + '.json')
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            if 0 <= time.time()-cached['saved'] < 86400:
                self.events.append({'method':method,'status':'cache_hit','retrieved_at':cached['retrieved_at']})
                self.local.retrieved_at = cached['retrieved_at']
                return cached['data']
        except (OSError,ValueError,KeyError): pass
        if self.failures >= 3 or time.monotonic() >= self.deadline:
            self.events.append({'method':method,'status':'budget_or_circuit_open'})
            return {}
        for attempt in range(2):
            remaining = self.deadline-time.monotonic()
            if remaining <= 0: return {}
            try:
                req = Request('https://ws.audioscrobbler.com/2.0/?'+urlencode({**public,'api_key':self.key}),headers={'User-Agent':'MusicAtlas/1.0'})
                with urlopen(req,timeout=min(5,remaining)) as response: data=json.loads(response.read(2000000))
                if not isinstance(data, dict): raise ValueError('Invalid Last.fm response')
                if 'error' in data: raise ValueError('Last.fm error '+str(data['error']))
                self.cache.mkdir(parents=True,exist_ok=True)
                stamp=utc_now()
                path.write_text(json.dumps({'saved':time.time(),'retrieved_at':stamp,'data':data},ensure_ascii=False),encoding='utf-8')
                self.events.append({'method':method,'status':'ok','retrieved_at':stamp})
                self.local.retrieved_at = stamp
                self.failures=0
                return data
            except (OSError,ValueError):
                self.failures+=1
                self.events.append({'method':method,'status':'failed'})
                if attempt==0: time.sleep(min(.25,max(0,self.deadline-time.monotonic())))
        return {}


def artist_url(name): return 'https://www.last.fm/music/'+quote(name,safe='')


def collect_tags(packet, client, concurrency=5):
    taxonomy=packet['style_analysis']['style_definitions']
    allowed={normalized_name(d['style_id'].replace('_',' ')):d['style_ref'] for d in taxonomy}
    allowed.update({normalized_name(d['label']):d['style_ref'] for d in taxonomy})
    # Broad tags remain broad; never convert metalcore into a specific subgenre.
    broad={'metalcore','metal','rock','pop','electronic','jazz','folk','hardcore','punk'}
    def collect(item):
        queries=[('track', 'track.getTopTags', {'artist':item['artist'],'track':item['title']}, artist_url(item['artist'])+'/_/'+quote(item['title'],safe=''))]
        if item.get('album'):
            queries.append(('album','album.getTopTags',{'artist':item['artist'],'album':item['album']},artist_url(item['artist'])+'/'+quote(item['album'],safe='')))
        queries.append(('artist','artist.getTopTags',{'artist':item['artist']},artist_url(item['artist'])))
        for scope,method,params,url in queries:
            data=client.call(method,**params)
            tags=data.get('toptags',{}).get('tag',[])
            if not isinstance(tags,list): tags=[]
            genres=[{'tag':t['name'],'style_ref':allowed.get(normalized_name(t['name']))} for t in tags
                    if isinstance(t,dict) and isinstance(t.get('name'),str) and (normalized_name(t['name']) in allowed or t['name'].casefold() in broad)]
            if genres: break
        return {'track_key':track_key(item['title'],item['artist']),'scope':scope if genres else 'unknown','url':url if genres else None,
                'tags':genres,'raw_tags':tags,'retrieved_at':getattr(client.local,'retrieved_at',None),
                'status':'supported' if genres else 'unavailable'}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        records=list(pool.map(collect,packet['favorite_tracks']))
    return {'provider':'lastfm','axis_policy':'removed','records':records,'requests':list(client.events)}



def validate_knowledge(packet):
    knowledge=packet.get('source_tags',{})
    records=knowledge.get('records')
    expected={track_key(t['title'],t['artist']) for t in packet['favorite_tracks']}
    provider=knowledge.get('provider')
    if provider not in ('lastfm','taste_summary') or knowledge.get('axis_policy')!='removed' or not isinstance(records,list):
        raise ContractError('风格分析来源结构无效')
    if len(records)!=len(packet['favorite_tracks']) or {r.get('track_key') for r in records}!=expected:
        raise ContractError('风格标签记录未覆盖分析输入')
    if provider=='lastfm':
        for record in records:
            if record.get('tags') and (record.get('scope') not in ('track','album','artist') or not record.get('retrieved_at') or not str(record.get('url','')).startswith('https://www.last.fm/music/')):
                raise ContractError('风格标签缺少来源或获取时间')


def discover(packet,client,max_candidates=60,similar_limit=4,top_track_limit=3,relation_project_limit=4,relation_top_track_limit=2,excluded_track_keys=None,excluded_canonical_track_ids=None):
    from metadata_verify import verify_many
    favorites=set(packet['playlist_exclusion']['track_keys'])
    extra_excluded_keys={str(value) for value in (excluded_track_keys or set()) if value}
    blocked_canonical_ids={str(value) for value in (excluded_canonical_track_ids or set()) if value}
    ids=set(packet['playlist_exclusion']['platform_track_ids'])
    source_artists={normalized_name(a['artist']) for a in packet['primary_distribution']}
    anchors={a['artist']:a for a in packet['primary_distribution']}
    anchors_by_marker={normalized_name(a['artist']):a for a in packet['primary_distribution']}

    # Relationship candidates come only from the independently collected catalog in
    # the analysis packet. Last.fm supplies the target artist's public top-track
    # order, but it is never treated as proof of the relationship itself.
    relation_lanes=[]
    for entity in packet.get('entities',[]):
        anchor=anchors_by_marker.get(normalized_name(entity.get('name')))
        if not anchor:
            continue
        lane=[]
        projects=entity.get('related_projects',[]) if isinstance(entity.get('related_projects'),list) else []
        for project in projects[:relation_project_limit]:
            name=str(project.get('name') or '').strip()
            person=str(project.get('person') or '').strip()
            sources=[url for url in project.get('sources',[]) if isinstance(url,str) and url.startswith('http')]
            if not name or not person or not sources or normalized_name(name) in source_artists:
                continue
            songs=client.call('artist.getTopTracks',artist=name,limit=relation_top_track_limit).get('toptracks',{}).get('track',[])
            if not isinstance(songs,list):
                songs=[]
            for track_rank,song in enumerate(songs,start=1):
                title=str(song.get('name') or '').strip()
                if title:
                    lane.append({'kind':'relation','title':title,'artist':name,'anchor':anchor,
                                 'track_rank':track_rank,'relation':project})
        if lane:
            relation_lanes.append(lane)

    def collect_similarity_lane(anchor):
        similar=client.call('artist.getSimilar',artist=anchor['artist'],limit=similar_limit).get('similarartists',{}).get('artist',[])
        if not isinstance(similar,list):
            similar=[]
        lane=[]
        for neighbor_rank,neighbor in enumerate(similar,start=1):
            name=neighbor.get('name','')
            if not name or normalized_name(name) in source_artists:
                continue
            songs=client.call('artist.getTopTracks',artist=name,limit=top_track_limit).get('toptracks',{}).get('track',[])
            if not isinstance(songs,list):
                songs=[]
            for track_rank,song in enumerate(songs,start=1):
                title=str(song.get('name') or '').strip()
                if title:
                    lane.append({'kind':'similarity','title':title,'artist':name,'anchor':anchor,
                                 'neighbor':neighbor,'neighbor_rank':neighbor_rank,'track_rank':track_rank})
        return lane
    with ThreadPoolExecutor(max_workers=8) as pool:
        # 大歌单只对出现频率最高的一批艺人做相似艺人召回：
        # 对上千位艺人逐个查询会把召回阶段拖到十分钟以上。
        similarity_anchors=list(packet['primary_distribution'])[:16]
        similarity_lanes=list(pool.map(collect_similarity_lane,similarity_anchors))

    # Round-robin all lanes so one prolific project or similarity seed cannot
    # consume the bounded verification budget. Relationship lanes are placed
    # first at each depth because there are fewer of them and they are factual.
    lanes=[*relation_lanes,*similarity_lanes]
    proposed=[]
    for offset in range(max(map(len,lanes),default=0)):
        for lane in lanes:
            if offset<len(lane):
                proposed.append(lane[offset])
    unique={}
    playlist_excluded_count=0
    history_excluded_count=0
    for proposal in proposed:
        key=track_key(proposal['title'],proposal['artist'])
        if key in favorites:
            playlist_excluded_count += 1
            continue
        if key in extra_excluded_keys:
            history_excluded_count += 1
            continue
        unique.setdefault(key,proposal)
    proposed=list(unique.values())[:max_candidates]
    facts=verify_many([(p['title'],p['artist']) for p in proposed],concurrency=8)
    result=[];seen=set(favorites)
    verification_rejected_count=0
    for proposal,fact in zip(proposed,facts):
        title,artist=proposal['title'],proposal['artist']
        if track_key(title,artist) in seen:
            continue
        if not fact or fact.get('source')=='skipped' or normalized_name(fact.get('artist'))!=normalized_name(artist):
            verification_rejected_count += 1
            continue
        key=track_key(fact['title'],fact['artist'])
        canonical_id=f"platform:{fact['source']}:{fact['platform_track_id']}"
        if canonical_id in blocked_canonical_ids:
            history_excluded_count += 1
            continue
        if key in seen or str(fact['platform_track_id']) in ids:
            verification_rejected_count += 1
            continue
        seen.add(key)
        anchor=proposal['anchor']
        if proposal['kind']=='relation':
            relation=proposal['relation']
            relation_sources=list(dict.fromkeys(url for url in relation.get('sources',[]) if isinstance(url,str)))
            relation_url=relation_sources[0]
            provider_relation={'seed':anchor['artist'],'artist':artist,'person':relation['person'],
                               'relation':relation.get('relation') or 'shared_member',
                               'role':relation.get('role'),'status':relation.get('status'),
                               'cross_checked':bool(relation.get('cross_checked')),
                               'url':relation_url,'sources':relation_sources,
                               'track_rank':proposal['track_rank']}
            candidate_type='musician_relation'
            relation_path=[anchor['artist'],'共享成员 '+relation['person'],artist,fact['title']]
            provider_fields={'provider_relation':provider_relation}
            evidence=[{'claim_type':'track_identity','claim':'平台公开曲目记录','url':fact['url']},
                      {'claim_type':'relation','claim':'公开目录记录的共享成员路径','url':relation_url}]
            sources=[*relation_sources,fact['url']]
            grade='A' if relation.get('cross_checked') else 'B'
        else:
            neighbor=proposal['neighbor']
            url=artist_url(anchor['artist'])+'/+similar'
            try:
                match=float(neighbor.get('match'))
            except (TypeError,ValueError):
                match=None
            candidate_type='exploration' if proposal['neighbor_rank']>1 or (match is not None and match<0.8) else 'style_neighbor'
            relation_path=[anchor['artist'],'相似艺人',artist,fact['title']]
            provider_fields={'provider_similarity':{'seed':anchor['artist'],'artist':artist,'match':neighbor.get('match'),
                               'rank':proposal['neighbor_rank'],'track_rank':proposal['track_rank'],'url':url}}
            evidence=[{'claim_type':'track_identity','claim':'平台公开曲目记录','url':fact['url']},
                      {'claim_type':'style','claim':'相似艺人线索','url':url}]
            sources=[url,fact['url']]
            grade='C'
        result.append({'canonical_track_id':f"platform:{fact['source']}:{fact['platform_track_id']}",
          'platform_track_id':fact['platform_track_id'],'title':fact['title'],'artist':fact['artist'],
          'project':fact['album'] or '未知专辑','candidate_type':candidate_type,
          'analysis_refs':[anchor['entity_ref']],'relation_path':relation_path,**provider_fields,
          'metadata_verified':fact,'sources':list(dict.fromkeys(sources)),
          'platform_links':{fact['source']:fact['url']},'evidence_grade':grade,'evidence_items':evidence,
          # 逐曲候选由 Last.fm 路径产生，没有八轴风格画像；这些默认值让候选
          # 同时满足新旧两套选曲契约（风格资料仍保留在 style_evidence）。
          'discovery_source':fact['url'],'style_status':'unclassified','style_refs':[],'style_mix':[],
          'style_axes':{axis:None for axis in STYLE_AXIS_IDS},'style_confidence':'low'})
    if result:
        evidence=collect_tags({'style_analysis':packet['style_analysis'],'favorite_tracks':[
            {'title':c['title'],'artist':c['artist'],'album':c['metadata_verified'].get('album')} for c in result
        ]},client,concurrency=4)['records']
        for candidate,record in zip(result,evidence):
            candidate['style_evidence']={k:record[k] for k in ('scope','tags','url','retrieved_at','status')}
            if record['tags']:
                # 证据 URL 必须同时列入 sources，否则候选契约会拒绝整包。
                candidate['sources']=list(dict.fromkeys([*candidate.get('sources',[]),record['url']]))
                candidate['evidence_items'].append({'claim_type':'style','claim':{'track':'曲目风格标签','album':'所属专辑风格标签','artist':'艺人风格标签'}[record['scope']], 'url':record['url'],'retrieved_at':record['retrieved_at']})
    return result,{'provider':'lastfm+public_relations','candidate_count':len(result),'playlist_excluded_count':playlist_excluded_count,'history_excluded_count':history_excluded_count,'verification_rejected_count':verification_rejected_count,
                  'relation_candidate_count':sum(c['candidate_type']=='musician_relation' for c in result),
                  'similarity_candidate_count':sum(c['candidate_type']!='musician_relation' for c in result),
                  'proposed_count':len(proposed),'requests':list(client.events)}


def _safe_reason(candidate):
    """文案含未经核验的沿革断言时退回程序表述，避免幻觉文案直接展示。"""
    from agent_lastfm import RELATION_CLAIM_WORDS
    reason=candidate.get('agent_reason')
    if not isinstance(reason,str) or not reason.strip():return ''
    return '' if any(word in reason for word in RELATION_CLAIM_WORDS) else reason


def select(bundle,packet):
    from copy import deepcopy
    from contracts import target_counts
    policy=packet['recommendation_policy'];result=[];artists={};projects={}
    pool=bundle['candidate_pool']
    # 每批 Atlas 的类型占比由 recall_mix 决定（风格邻近 4 ：艺人延伸 3 ：
    # 音乐人关系 2 ：探索推荐 1）。这里按配额优先重排候选：先按原顺序满足
    # 各类型名额，再用其余候选补足，避免稀缺类型被大类型挤空。
    target=int(policy['target_recommendations'])
    quota=target_counts(target,packet)
    filled={}
    preferred=[]
    for item in pool:
        kind=str(item.get('candidate_type') or '')
        if filled.get(kind,0)<quota.get(kind,0):
            preferred.append(item);filled[kind]=filled.get(kind,0)+1
    preferred_ids={id(item) for item in preferred}
    pool=preferred+[item for item in pool if id(item) not in preferred_ids]
    if packet.get('agent_islands'):
        lanes=[[c for c in pool if c.get('matched_interest_id')==g['id']] for g in packet['agent_islands']]
        pool=[lane[index] for index in range(max(map(len,lanes),default=0)) for lane in lanes if index<len(lane)]
    selection_exclusion=bundle.get('selection_exclusion') or {}
    excluded_ids={str(value) for value in selection_exclusion.get('canonical_track_ids',[]) if value}
    excluded_keys={str(value) for value in selection_exclusion.get('track_keys',[]) if value}
    pool=[item for item in pool if str(item.get('canonical_track_id') or '') not in excluded_ids
          and track_key(item['title'],item['artist']) not in excluded_keys]
    # Agent 负责候选顺序；程序仅保留两条硬边界：若候选池存在真实
    # 音乐人关系或探索路径，正式推荐范围各至少保留一条。
    relation_index=next((i for i,c in enumerate(pool) if c.get('candidate_type')=='musician_relation'),None)
    if relation_index is not None and relation_index>=target and target>0:
        pool.insert(max(0,target-2),pool.pop(relation_index))
    exploration_index=next((i for i,c in enumerate(pool) if c.get('candidate_type')=='exploration'),None)
    if exploration_index is not None and exploration_index>=target and target>0:
        pool.insert(target-1,pool.pop(exploration_index))
    for item in pool:
        artist=normalized_name(item['artist']);project=artist+'|'+normalized_name(item['project'])
        if artists.get(artist,0)>=policy['max_per_artist'] or projects.get(project,0)>=policy['max_per_project']:continue
        if track_key(item['title'],item['artist']) in packet['playlist_exclusion']['track_keys']:continue
        if str(item['platform_track_id']) in packet['playlist_exclusion']['platform_track_ids']:continue
        selected=deepcopy(item);selected['selection_rank']=len(result)+1
        details=item.get('agent_details')
        if details:
            selected['program_explanation']={**details,'text':item['agent_reason']}
        else:
            if item.get('candidate_type')=='musician_relation':
                relation=item.get('provider_relation') or {}
                text=_safe_reason(item) or ('沿 '+str(relation.get('person') or '共享音乐人')+' 的公开成员路径发现。')
                fit='沿公开目录核验的成员或合作关系发现的候选。'
            elif item.get('candidate_type')=='artist_continuation' and not item.get('provider_similarity'):
                text=_safe_reason(item) or ('来自当前歌单艺人 '+str(item.get('artist') or '')+' 的平台公开歌曲记录。')
                fit='沿当前歌单艺人的平台公开歌曲记录发现的候选。'
            else:
                seed=(item.get('provider_similarity') or {}).get('seed')
                text=_safe_reason(item) or ('从 '+str(seed or '相似艺人')+' 的相似艺人方向向外探索。')
                fit='沿相似艺人线索发现的候选。'
            selected['program_explanation']={'text':text,'preference_basis':text,'music_fit':fit}
        result.append(selected);artists[artist]=artists.get(artist,0)+1;projects[project]=projects.get(project,0)+1
        if len(result)>=target:break
    out=deepcopy(bundle);out.update(bundle_stage='ranked',publication_status='draft',status='ready' if result else 'insufficient_evidence',recommendations=result,
       ranking={'algorithm_version':'lastfm_constraints_v1','selected_count':len(result),'selected_canonical_track_ids':[r['canonical_track_id'] for r in result],
                'shortfall':max(0,policy['target_recommendations']-len(result)),'ordering':'agent_order_round_robin_islands' if packet.get('agent_islands') else 'provider_order_round_robin_seeds'})
    return out


def validate_bundle(bundle,packet):
    if not isinstance(bundle,dict) or bundle.get('schema_version')!='2.0' or bundle.get('bundle_type')!='recommendation_bundle':
        raise ContractError('推荐包结构无效')
    if not isinstance(bundle.get('candidate_pool'),list): raise ContractError('候选池必须是列表')
    if bundle.get('bundle_stage')=='candidate_pool' and (bundle.get('recommendations') or bundle.get('ranking')):
        raise ContractError('候选阶段禁止预填选曲')
    if bundle.get('analysis_id')!=packet['analysis_id']:raise ContractError('分析包不匹配')
    if bundle.get('bundle_stage') not in ('candidate_pool','ranked'):raise ContractError('推荐阶段无效')
    seen=set()
    for c in bundle.get('candidate_pool',[]):
        if not isinstance(c,dict): raise ContractError('候选结构无效')
        if packet.get('agent_islands') and (c.get('matched_interest_id') not in {g['id'] for g in packet['agent_islands']} or not isinstance(c.get('agent_reason'),str) or not c['agent_reason'].strip()): raise ContractError('候选缺少 Agent 兴趣岛归属或理由')
        if packet.get('agent_copy_version') == 1:
            from agent_lastfm import validate_candidate_copy
            validate_candidate_copy(c)
            style=c.get('style_evidence') or {}
            if style.get('tags') and (style.get('scope') not in ('track','album','artist') or not style.get('retrieved_at') or not str(style.get('url','')).startswith('https://www.last.fm/music/')):
                raise ContractError('候选风格资料缺少来源、范围或时间')
        f=c.get('metadata_verified') or {}
        if not f.get('url') or f.get('source')=='skipped':raise ContractError('候选缺少平台事实或来源')
        if any(c.get(k)!=f.get(k) for k in ('title','artist','platform_track_id')):raise ContractError('候选身份不一致')
        anchors={a['artist']:a['entity_ref'] for a in packet['primary_distribution']}
        if c.get('candidate_type')=='musician_relation':
            p=c.get('provider_relation') or {}
            relation_sources=p.get('sources')
            if (p.get('seed') not in anchors or not p.get('person') or not p.get('url')
                    or not isinstance(relation_sources,list) or p.get('url') not in relation_sources):
                raise ContractError('音乐人关系候选缺少公开关系来源')
            if c.get('analysis_refs')!=[anchors[p['seed']]] or normalized_name(p.get('artist'))!=normalized_name(c.get('artist')):
                raise ContractError('音乐人关系候选与来源不一致')
            if not any(item.get('claim_type')=='relation' and item.get('url')==p.get('url')
                       for item in c.get('evidence_items',[]) if isinstance(item,dict)):
                raise ContractError('音乐人关系候选缺少关系证据')
        else:
            p=c.get('provider_similarity') or {}
            platform_continuation=False
            if c.get('candidate_type')=='artist_continuation' and not p:
                # 平台公开记录路径（platform_discovery）：候选锚定当前歌单艺人，
                # 以平台歌曲记录作为身份证据，不需要相似艺人来源。
                refs=c.get('analysis_refs') if isinstance(c.get('analysis_refs'),list) else []
                owners=[artist for artist,ref in anchors.items() if ref in refs]
                verified=c.get('metadata_verified') or {}
                sources=c.get('sources') if isinstance(c.get('sources'),list) else []
                platform_continuation=(len(refs)==1 and len(owners)==1
                    and normalized_name(owners[0])==normalized_name(c.get('artist'))
                    and bool(verified.get('url')) and verified.get('url') in sources
                    and any(isinstance(item,dict) and item.get('claim_type')=='track_identity' and item.get('url')==verified.get('url')
                            for item in c.get('evidence_items',[])))
                if not platform_continuation:raise ContractError('艺人延伸候选与当前歌单艺人不一致，或缺少平台事实证据')
            if not platform_continuation:
                if not p.get('url') or not p.get('seed'):
                    raise ContractError('候选缺少相似艺人来源')
                if p.get('seed') not in anchors or p.get('url')!=artist_url(p['seed'])+'/+similar': raise ContractError('种子来源无效')
                if c.get('analysis_refs')!=[anchors[p['seed']]] or normalized_name(p.get('artist'))!=normalized_name(c.get('artist')): raise ContractError('艺人来源不一致')
        if f.get('source') not in ('netease','itunes','qq') or c.get('canonical_track_id')!=f"platform:{f.get('source')}:{f.get('platform_track_id')}": raise ContractError('平台标识无效')
        if c.get('project')!=(f.get('album') or '未知专辑'): raise ContractError('专辑不一致')
        key=track_key(c['title'],c['artist'])
        if key in seen or key in packet['playlist_exclusion']['track_keys'] or str(c['platform_track_id']) in packet['playlist_exclusion']['platform_track_ids']:raise ContractError('候选重复或属于完整歌单')
        if any(k in c for k in ('ranking_score','score_features')):raise ContractError('候选禁止自建评分')
        seen.add(key)
    if bundle['bundle_stage']=='ranked':
        expected=select(bundle,packet)
        if bundle.get('recommendations')!=expected['recommendations'] or bundle.get('ranking')!=expected['ranking']:raise ContractError('约束选曲重算不一致')
    return bundle

"""Last.fm public labels and provider-ranked discovery; no inferred audio axes."""
from __future__ import annotations
import hashlib
import json
import os
import time
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from contracts import ContractError, track_key, normalized_name, utc_now
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
        # A short-lived circuit protects the provider during a burst of
        # transport/API failures without permanently abandoning the rest of
        # the playlist.  The old global ``failures >= 3`` check made every
        # queued request return immediately after three failures, even when
        # the provider recovered a moment later.
        self.circuit_open_until = 0.0
        self.circuit_cooldown = 0.35
        self.lock = threading.RLock()
        self.request_locks = {}
        self.local = threading.local()

    def _event(self, method, status, **fields):
        event = {'method': method, 'status': status}
        phase = getattr(self.local, 'collection_phase', None)
        if phase:
            event['phase'] = phase
        event.update(fields)
        with self.lock:
            self.events.append(event)

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
                self._event(method, 'cache_hit', retrieved_at=cached['retrieved_at'])
                self.local.retrieved_at = cached['retrieved_at']
                return cached['data']
        except (OSError,ValueError,KeyError): pass
        now = time.monotonic()
        with self.lock:
            circuit_open = now < self.circuit_open_until
        if circuit_open or now >= self.deadline:
            self._event(method, 'budget_or_circuit_open')
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
                self._event(method, 'ok', retrieved_at=stamp,
                            provider_attempt=attempt + 1)
                self.local.retrieved_at = stamp
                with self.lock:
                    self.failures = 0
                    self.circuit_open_until = 0.0
                return data
            except (OSError,ValueError):
                with self.lock:
                    self.failures += 1
                    if self.failures >= 3:
                        self.circuit_open_until = min(
                            self.deadline,
                            time.monotonic() + self.circuit_cooldown,
                        )
                self._event(method, 'failed', provider_attempt=attempt + 1)
                if attempt==0: time.sleep(min(.25,max(0,self.deadline-time.monotonic())))
        return {}


def artist_url(name): return 'https://www.last.fm/music/'+quote(name,safe='')


def _tag_vocabulary(style_definitions):
    allowed = {normalized_name(d['style_id'].replace('_', ' ')): d['style_ref']
               for d in style_definitions}
    allowed.update({normalized_name(d['label']): d['style_ref'] for d in style_definitions})
    # A broad public tag remains broad; never infer a narrower genre or listening axis.
    broad = {'metalcore', 'metal', 'rock', 'pop', 'electronic', 'jazz', 'folk', 'hardcore', 'punk'}
    return allowed, broad


def _tag_query(scope, artist, title=None, album=None):
    params = {'artist': artist}
    if scope == 'track':
        params['track'] = title
        url = artist_url(artist) + '/_/' + quote(title, safe='')
    elif scope == 'album':
        params['album'] = album
        url = artist_url(artist) + '/' + quote(album, safe='')
    else:
        url = artist_url(artist)
    subject = {key: params[key] for key in ('artist', 'track', 'album') if key in params}
    return scope + '.getTopTags', params, url, subject


def _parse_tag_evidence(scope, query, data, retrieved_at, vocabulary):
    _method, _params, url, subject = query
    top = data.get('toptags') if isinstance(data, dict) else None
    top = top if isinstance(top, dict) else {}
    raw = top.get('tag', [])
    if not isinstance(raw, list):
        raw = [raw] if isinstance(raw, dict) else []
    echoed = top.get('@attr')
    echoed = echoed if isinstance(echoed, dict) else {}
    identity_fields = {key: echoed[key] for key in subject
                       if isinstance(echoed.get(key), str) and echoed[key].strip()}
    mismatched = any(normalized_name(identity_fields[key]) != normalized_name(expected)
                     for key, expected in subject.items() if key in identity_fields)
    identity_status = ('mismatch' if mismatched else 'matched'
                       if len(identity_fields) == len(subject) else 'request_only')
    allowed, broad = vocabulary
    tags = [] if mismatched else [
        {'tag': tag['name'], 'style_ref': allowed.get(normalized_name(tag['name']))}
        for tag in raw if isinstance(tag, dict) and isinstance(tag.get('name'), str)
        and (normalized_name(tag['name']) in allowed or normalized_name(tag['name']) in broad)
    ]
    # A response lacking a timestamp cannot be accepted as attributable evidence.
    status = ('identity_mismatch' if mismatched else 'supported' if tags and retrieved_at
              else 'unverified_time' if tags else 'no_style_tags' if raw else 'unavailable')
    if not retrieved_at:
        tags = []
    return {'scope': scope, 'subject': subject, 'returned_subject': identity_fields,
            'source': 'lastfm', 'url': url,
            'retrieved_at': retrieved_at, 'identity_status': identity_status,
            'status': status, 'tags': tags, 'raw_tags': raw}


def collect_tags(packet, client, concurrency=5):
    """Keep every public tag layer distinct, querying each unique object once.

    `scope/tags` remain the first supported layer for existing callers; the
    complete evidence array never claims artist/album context as track fact.
    """
    vocabulary = _tag_vocabulary(packet['style_analysis']['style_definitions'])
    items = packet['favorite_tracks']
    order = []
    unique = {}

    def add(scope, artist, title=None, album=None):
        key = (scope, normalized_name(artist), normalized_name(title) if scope == 'track' else '',
               normalized_name(album) if scope == 'album' else '')
        if key not in unique:
            unique[key] = _tag_query(scope, artist, title, album)
            order.append(key)
        return key

    item_keys = []
    for item in items:
        keys = [add('track', item['artist'], title=item['title'])]
        if item.get('album'):
            keys.append(add('album', item['artist'], album=item['album']))
        keys.append(add('artist', item['artist']))
        item_keys.append(keys)

    def fetch(key, phase='initial'):
        query = unique[key]
        method, params, _, _ = query
        local = getattr(client, 'local', None)
        if local is not None:
            # LastFM uses this thread-local marker to make cache hits and
            # retry requests visible in the persisted request log.  Test and
            # alternate clients that do not expose a thread-local simply omit
            # the marker without changing their call contract.
            setattr(local, 'collection_phase', phase)
        try:
            data = client.call(method, **params)
        except Exception as exc:  # One provider failure must not erase other scoped evidence.
            data = {}
            failure = type(exc).__name__
        else:
            failure = None
        stamp = (None if failure else getattr(getattr(client, 'local', None), 'retrieved_at', None))
        evidence = _parse_tag_evidence(key[0], query, data, stamp, vocabulary)
        evidence['collection_pass'] = phase
        if failure:
            evidence['error_type'] = failure
        return evidence

    def fetch_many(keys, phase):
        if not keys:
            return {}
        workers = max(1, min(int(concurrency), len(keys)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return {key: evidence for key, evidence in zip(
                keys, pool.map(lambda current: fetch(current, phase), keys)
            )}

    found = {}
    if order:
        found.update(fetch_many(order, 'initial'))

    # A transient provider failure can leave only a small subset of objects
    # without an attributable timestamp.  Retry only those track/album
    # objects once, after the short circuit cooldown, and never turn an empty
    # or mismatched response into evidence.  The cap keeps the second pass
    # bounded for large playlists and leaves the existing 50% quality gate
    # unchanged.
    retry_candidates = [
        key for key in order
        if key[0] in ('track', 'album')
        and found.get(key, {}).get('status') != 'supported'
        and found.get(key, {}).get('identity_status') != 'mismatch'
        and not found.get(key, {}).get('retrieved_at')
    ]
    retry_limit = max(8, min(96, max(1, int(concurrency)) * 6))
    retry_keys = retry_candidates[:retry_limit]
    deadline = getattr(client, 'deadline', float('inf'))
    circuit_until = getattr(client, 'circuit_open_until', 0.0)
    retry_delay = max(0.0, min(0.5, float(circuit_until) - time.monotonic()))
    if retry_keys and time.monotonic() + retry_delay < deadline:
        if retry_delay:
            time.sleep(retry_delay)
        found.update(fetch_many(retry_keys, 'retry'))

    requests = list(getattr(client, 'events', []))
    records = []
    for item, keys in zip(items, item_keys):
        evidence = [found[key] for key in keys]
        best = next((layer for layer in evidence if layer['status'] == 'supported'), None)
        records.append({'track_key': track_key(item['title'], item['artist']),
                        'scope': best['scope'] if best else 'unknown',
                        'url': best['url'] if best else None,
                        'tags': best['tags'] if best else [],
                        'raw_tags': best['raw_tags'] if best else [],
                        'retrieved_at': best['retrieved_at'] if best else None,
                        'status': 'supported' if best else 'unavailable',
                        'evidence': evidence})
    return {'provider': 'lastfm', 'axis_policy': 'removed', 'records': records,
            'requests': requests,
            'retry_candidate_count': len(retry_candidates),
            'retry_request_count': len(retry_keys),
            'retry_omitted_count': max(0, len(retry_candidates) - len(retry_keys)),
            'cache_hit_count': sum(1 for event in requests if event.get('status') == 'cache_hit'),
            'retry_event_count': sum(1 for event in requests if event.get('phase') == 'retry')}


def collect_artist_tags(artists, client, concurrency=8, *, style_definitions=None,
                        max_artists=96, max_seconds=40):
    """For 1000+ tracks, query artist-level evidence only, once per artist.

    No synthetic favorite track is introduced, so the request log itself proves
    that this mode did not perform per-track or album Last.fm analysis.
    Accepts a primary_distribution list or its containing analysis packet.
    """
    packet = artists if isinstance(artists, dict) else None
    if packet is not None:
        artists = packet['primary_distribution']
        if style_definitions is None:
            style_definitions = packet.get('style_analysis', {}).get('style_definitions', [])
    vocabulary = _tag_vocabulary(style_definitions or [])
    unique_artists = {}
    for value in artists:
        item = {'artist': value, 'count': 1} if isinstance(value, str) else value
        name = str(item.get('artist') or '').strip() if isinstance(item, dict) else ''
        if name:
            key = normalized_name(name)
            if key not in unique_artists:
                unique_artists[key] = {'artist': name, 'count': 0}
            unique_artists[key]['count'] += max(0, int(item.get('count', 0) or 0))

    def fetch(item):
        artist = item['artist']
        query = _tag_query('artist', artist)
        try:
            data = client.call(query[0], **query[1])
        except Exception as exc:
            data = {}
            failure = type(exc).__name__
        else:
            failure = None
        stamp = (None if failure else getattr(getattr(client, 'local', None), 'retrieved_at', None))
        evidence = _parse_tag_evidence('artist', query, data, stamp, vocabulary)
        if failure:
            evidence['error_type'] = failure
        return {'artist': artist, 'count': item.get('count', 0), 'scope': 'artist',
                'tags': evidence['tags'], 'status': evidence['status'],
                'url': evidence['url'], 'retrieved_at': evidence['retrieved_at'],
                'evidence': [evidence]}

    ordered = sorted(unique_artists.values(), key=lambda item: (-item['count'], normalized_name(item['artist'])))
    selected = ordered[:max(0, int(max_artists))]
    cutoff = min(time.monotonic() + max(0, float(max_seconds)),
                 getattr(client, 'deadline', float('inf')))
    results = {}
    if selected and time.monotonic() < cutoff:
        workers = max(1, min(int(concurrency), len(selected)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {}
            next_index = 0

            def submit_next():
                nonlocal next_index
                if next_index < len(selected) and time.monotonic() < cutoff:
                    index = next_index
                    next_index += 1
                    pending[pool.submit(fetch, selected[index])] = index

            for _ in range(workers):
                submit_next()
            while pending:
                completed, _ = wait(pending, timeout=max(0, cutoff - time.monotonic()),
                                    return_when=FIRST_COMPLETED)
                if not completed:
                    break
                for future in completed:
                    index = pending.pop(future)
                    results[index] = future.result()
                    submit_next()
        # In-flight provider calls have their own short timeout; unscheduled
        # artists remain explicitly unqueried rather than assumed unknown.
        for future, index in pending.items():
            results[index] = future.result()

    records = []
    for index, item in enumerate(ordered):
        record = results.get(index)
        if record is None:
            record = {'artist': item['artist'], 'count': item['count'], 'scope': 'artist',
                      'tags': [], 'status': 'not_queried_budget', 'url': None,
                      'retrieved_at': None, 'evidence': [{
                          'scope': 'artist', 'subject': {'artist': item['artist']},
                          'source': 'lastfm', 'url': None, 'retrieved_at': None,
                          'identity_status': 'not_checked', 'status': 'not_queried_budget',
                          'tags': [], 'raw_tags': []}]}
        records.append(record)
    result = {'provider': 'lastfm', 'scope': 'artist', 'axis_policy': 'removed',
              'artist_records': records, 'queried_artist_count': len(results),
              'budget_omitted_count': len(records) - len(results),
              'requests': list(client.events)}
    if packet is not None:
        by_artist = {normalized_name(record['artist']): record for record in records}
        mapped = []
        for track in packet.get('favorite_tracks', []):
            artist = by_artist.get(normalized_name(track.get('artist')))
            layer = artist['evidence'][0] if artist else {
                'scope': 'artist', 'subject': {'artist': track['artist']},
                'source': 'lastfm', 'url': None, 'retrieved_at': None,
                'identity_status': 'not_checked', 'status': 'not_queried_budget',
                'tags': [], 'raw_tags': []}
            supported = bool(layer and layer['status'] == 'supported')
            mapped.append({'track_key': track_key(track['title'], track['artist']),
                           'scope': 'artist' if supported else 'unknown',
                           'url': layer['url'] if supported else None,
                           'tags': layer['tags'] if supported else [],
                           'raw_tags': layer['raw_tags'] if supported else [],
                           'retrieved_at': layer['retrieved_at'] if supported else None,
                           'status': 'supported' if supported else 'unavailable',
                           'evidence': [layer]})
        result['records'] = mapped  # Compatibility only; these are still artist facts.
    return result



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
        by_track_key = {track_key(item['title'], item['artist']): item for item in packet['favorite_tracks']}
        for record in records:
            if record.get('tags') and (record.get('scope') not in ('track','album','artist') or not record.get('retrieved_at') or not str(record.get('url','')).startswith('https://www.last.fm/music/')):
                raise ContractError('风格标签缺少来源或获取时间')
            layers = record.get('evidence')
            if layers is None:  # Older persisted packets remain readable.
                continue
            expected_scopes = ((['artist'],) if knowledge.get('scope') == 'artist'
                               else (['track', 'artist'], ['track', 'album', 'artist']))
            if not isinstance(layers, list) or [e.get('scope') for e in layers if isinstance(e, dict)] not in expected_scopes:
                raise ContractError('曲目、专辑和艺人证据层级不完整')
            for layer in layers:
                if layer.get('source') != 'lastfm' or layer.get('identity_status') not in (
                        'matched', 'request_only', 'mismatch', 'not_checked'):
                    raise ContractError('风格标签身份或来源无效')
                subject = layer.get('subject')
                item = by_track_key.get(record.get('track_key'))
                if not isinstance(subject, dict) or item is None or normalized_name(subject.get('artist')) != normalized_name(item['artist']):
                    raise ContractError('风格来源对象与输入曲目不一致')
                scope = layer['scope']
                if scope == 'track' and normalized_name(subject.get('track')) != normalized_name(item['title']):
                    raise ContractError('曲目标签与输入歌曲身份不一致')
                if scope == 'album' and normalized_name(subject.get('album')) != normalized_name(item.get('album')):
                    raise ContractError('专辑标签与输入专辑身份不一致')
                expected_url = _tag_query(scope, subject['artist'], title=subject.get('track'),
                                          album=subject.get('album'))[2]
                omitted = layer.get('status') == 'not_queried_budget'
                if omitted and (layer.get('url') or layer.get('retrieved_at') or layer.get('tags')
                                or layer.get('identity_status') != 'not_checked'):
                    raise ContractError('预算未查询的歌手不得附加风格资料')
                if not omitted and layer.get('url') != expected_url:
                    raise ContractError('风格资料网址与来源对象不一致')
                if layer.get('status') == 'supported' and (not layer.get('tags') or layer.get('identity_status') == 'mismatch'
                        or not layer.get('retrieved_at') or not str(layer.get('url', '')).startswith('https://www.last.fm/music/')):
                    raise ContractError('风格证据缺少身份、网址或取得时间')
            best = next((e for e in layers if e['status'] == 'supported'), None)
            legacy = (record.get('scope'), record.get('tags'), record.get('url'), record.get('retrieved_at'))
            expected_legacy = ((best['scope'], best['tags'], best['url'], best['retrieved_at'])
                               if best else ('unknown', [], None, None))
            if legacy != expected_legacy:
                raise ContractError('兼容字段与来源证据不一致')


def attach_style_evidence(candidate, record):
    """Attach scoped public labels, keeping platform metadata for identity only."""
    candidate['style_evidence'] = {
        key: record.get(key) for key in ('scope', 'tags', 'url', 'retrieved_at', 'status')}
    layers = record.get('evidence')
    candidate['style_evidence']['evidence'] = layers if isinstance(layers, list) else []
    supported = ([layer for layer in layers if layer.get('status') == 'supported']
                 if isinstance(layers, list) else
                 [candidate['style_evidence']] if record.get('tags') else [])
    claims = {'track': '曲目公开风格标签', 'album': '所属专辑公开风格标签',
              'artist': '艺人公开风格标签'}
    for layer in supported:
        url = layer.get('url')
        if not url:
            continue
        candidate.setdefault('evidence_items', []).append({
            'claim_type': 'style', 'scope': layer['scope'],
            'claim': claims[layer['scope']], 'url': url,
            'retrieved_at': layer['retrieved_at']})
        candidate['sources'] = list(dict.fromkeys([*candidate.get('sources', []), url]))


def discover(packet,client,max_candidates=60,similar_limit=4,top_track_limit=3,relation_project_limit=4,relation_top_track_limit=2,excluded_track_keys=None,excluded_canonical_track_ids=None,concurrency=8,include_similarity=True):
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
        # A bounded candidate budget must reach more than the first similar artist.
        # Otherwise many anchors fill the pool with rank-1 songs and exploration
        # never gets a chance to pass the final strict quota.
        lane.sort(key=lambda item: (item['track_rank'], item['neighbor_rank']))
        return lane
    similarity_lanes=[]
    if include_similarity:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
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
    facts=verify_many([(p['title'],p['artist']) for p in proposed],concurrency=concurrency)
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
          # Platform records establish identity, not subjective style descriptors.
          'discovery_source':fact['url'],'style_status':'unclassified','style_refs':[],'style_mix':[],
          'style_confidence':'low'})
    if result:
        evidence=collect_tags({'style_analysis':packet['style_analysis'],'favorite_tracks':[
            {'title':c['title'],'artist':c['artist'],'album':c['metadata_verified'].get('album')} for c in result
        ]},client,concurrency=4)['records']
        for candidate,record in zip(result,evidence):
            attach_style_evidence(candidate, record)
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


def _source_bounded_explanation(candidate):
    """Publish only program-verifiable discovery and correctly scoped tags.

    Agent copy is useful for ordering, but its prose cannot establish a track's
    sound from an artist/album tag or from a similarity link alone.
    """
    relation=candidate.get('provider_relation') or {}
    similarity=candidate.get('provider_similarity') or {}
    seed=str(relation.get('seed') or similarity.get('seed') or '').strip()
    artist=str(candidate.get('artist') or '').strip()
    if candidate.get('candidate_type')=='musician_relation' and relation.get('url'):
        path=f"从歌单艺人 {seed} 的公开音乐人关系找到 {artist}"
    elif candidate.get('candidate_type')=='artist_continuation' and not similarity:
        path=f"沿歌单已有艺人 {artist} 的公开曲目记录找到这首候选"
    else:
        path=f"沿歌单艺人 {seed} 的公开相似艺人线索找到 {artist}"

    style=candidate.get('style_evidence') or {}
    layers=style.get('evidence') if isinstance(style,dict) else None
    layers=layers if isinstance(layers,list) else [style]
    labels={'track':'单曲','album':'所属专辑','artist':'艺人'}
    facts=[]
    for layer in layers:
        if not isinstance(layer,dict) or layer.get('scope') not in labels:
            continue
        if layer.get('status')!='supported' or layer.get('identity_status')=='mismatch':
            continue
        if not str(layer.get('url') or '').startswith('https://www.last.fm/music/') or not layer.get('retrieved_at'):
            continue
        tags=list(dict.fromkeys(str(tag.get('tag') or '').strip() for tag in (layer.get('tags') or [])
                               if isinstance(tag,dict) and tag.get('tag')))
        if tags:
            facts.append(f"{labels[layer['scope']]}标签：{'、'.join(tags)}")
    if facts:
        fit='；'.join(facts)+'。'
        if any(fact.startswith(('所属专辑','艺人')) for fact in facts):
            fit+='专辑或艺人标签只提供背景，不代表这首歌的逐曲听感。'
    else:
        fit='没有可核验的风格标签，仅保留公开发现路径，不推断这首歌的具体听感。'
    return {'text':path+'；'+facts[0]+'。' if facts else path+'。',
            'preference_basis':path+'，这是候选与本次歌单的可追溯连接。',
            'music_fit':fit,
            'novelty':'候选不在本次完整歌单中，提供一首可进一步比较的作品。',
            'listening_tip':'建议与歌单中熟悉的作品并排试听，再判断实际听感与偏好的距离。'}


def _strict_capacity_selection(eligible,quota,policy):
    """Exact quotas with nested project/artist capacities; reroute conflicts."""
    graph=[]
    def node():
        graph.append([])
        return len(graph)-1
    def edge(start,end,capacity):
        forward=[end,capacity,len(graph[end])]
        backward=[start,0,len(graph[start])]
        graph[start].append(forward)
        graph[end].append(backward)
        return forward

    source=node();sink=node()
    kinds={kind:node() for kind,need in quota.items() if need>0}
    kind_edges={kind:edge(source,vertex,quota[kind]) for kind,vertex in kinds.items()}
    artists={};projects={};items=[]
    for item in eligible:
        kind=item.get('candidate_type')
        if kind not in kinds:continue
        artist=normalized_name(item['artist'])
        project=artist+'|'+normalized_name(item['project'])
        if artist not in artists:
            artists[artist]=node()
            edge(artists[artist],sink,policy['max_per_artist'])
        if project not in projects:
            projects[project]=node()
            edge(projects[project],artists[artist],policy['max_per_project'])
        item_node=node()
        choice=edge(kinds[kind],item_node,1)
        edge(item_node,projects[project],1)
        items.append((item,choice))

    from collections import deque
    while True:
        parents={source:None};pending=deque([source])
        while pending and sink not in parents:
            vertex=pending.popleft()
            for index,connection in enumerate(graph[vertex]):
                neighbor,capacity,_=connection
                if capacity>0 and neighbor not in parents:
                    parents[neighbor]=(vertex,index)
                    pending.append(neighbor)
        if sink not in parents:break
        cursor=sink
        while cursor!=source:
            previous,index=parents[cursor]
            connection=graph[previous][index]
            connection[1]-=1
            graph[cursor][connection[2]][1]+=1
            cursor=previous
    return {id(item) for item,choice in items if choice[1]==0}, {
        kind:quota[kind]-connection[1] for kind,connection in kind_edges.items()}


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
    strict_mix=packet.get('strict_recall_mix') is True
    strict_ids=None
    if strict_mix:
        from collections import Counter
        labels={'style_neighbor':'风格邻近','artist_continuation':'艺人延伸',
                'musician_relation':'音乐人关系','exploration':'探索推荐'}
        eligible=[item for item in pool
                  if track_key(item['title'],item['artist']) not in packet['playlist_exclusion']['track_keys']
                  and str(item['platform_track_id']) not in packet['playlist_exclusion']['platform_track_ids']]
        available=Counter(str(item.get('candidate_type')) for item in eligible)
        shortages=[f"{labels.get(kind,kind)} {available[kind]}/{need}"
                   for kind,need in quota.items() if available[kind]<need]
        if shortages:
            raise ContractError('Atlas 严格配比候选不足：'+ '；'.join(shortages))
        strict_ids,matched=_strict_capacity_selection(eligible,quota,policy)
        if sum(matched.values())!=target:
            shortage='；'.join(f'{labels.get(kind,kind)} {matched[kind]}/{need}'
                             for kind,need in quota.items() if matched.get(kind,0)<need)
            raise ContractError('Atlas 严格配比无法满足：'+shortage+'，候选受艺人或专辑上限限制')
    # Agent 负责候选顺序；程序仅保留两条硬边界：若候选池存在真实
    # 音乐人关系或探索路径，正式推荐范围各至少保留一条。
    if not strict_mix:
        relation_index=next((i for i,c in enumerate(pool) if c.get('candidate_type')=='musician_relation'),None)
        if relation_index is not None and relation_index>=target and target>0:
            pool.insert(max(0,target-2),pool.pop(relation_index))
        exploration_index=next((i for i,c in enumerate(pool) if c.get('candidate_type')=='exploration'),None)
        if exploration_index is not None and exploration_index>=target and target>0:
            pool.insert(target-1,pool.pop(exploration_index))
    for item in pool:
        if strict_mix and id(item) not in strict_ids:continue
        artist=normalized_name(item['artist']);project=artist+'|'+normalized_name(item['project'])
        if artists.get(artist,0)>=policy['max_per_artist'] or projects.get(project,0)>=policy['max_per_project']:continue
        if track_key(item['title'],item['artist']) in packet['playlist_exclusion']['track_keys']:continue
        if str(item['platform_track_id']) in packet['playlist_exclusion']['platform_track_ids']:continue
        selected=deepcopy(item);selected['selection_rank']=len(result)+1
        details=item.get('agent_details')
        if packet.get('agent_copy_version')==1:
            selected['program_explanation']=_source_bounded_explanation(item)
        elif details:
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
    if strict_mix and (len(result)!=target or Counter(item['candidate_type'] for item in result)!=Counter(quota)):
        raise ContractError('Atlas 严格配比校验失败，未发布本组推荐')
    out=deepcopy(bundle);out.update(bundle_stage='ranked',publication_status='draft',status='ready' if result else 'insufficient_evidence',recommendations=result,
       ranking={'algorithm_version':'lastfm_constraints_v1_strict_mix' if strict_mix else 'lastfm_constraints_v1','selected_count':len(result),'selected_canonical_track_ids':[r['canonical_track_id'] for r in result],
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
    anchors={a['artist']:a['entity_ref'] for a in packet['primary_distribution']}
    anchor_refs={}
    for name,ref in anchors.items():
        anchor_refs.setdefault(name.casefold(),set()).add(ref)

    def source_anchor_ref(seed):
        # In-flight prefetch can retain the seed's public display casing while
        # relation research canonicalizes the final packet's display casing.
        # Accept only one unambiguous case-insensitive anchor identity; the
        # original seed URL and the resolved analysis ref remain strict below.
        refs=anchor_refs.get(seed.casefold(),set()) if isinstance(seed,str) else set()
        return next(iter(refs)) if len(refs)==1 else None

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
            if style.get('tags'):
                fact=c.get('metadata_verified') or {}
            # The source URL preserves the provider-request display casing.
            # Compare the subject below, then verify that URL against the
            # original subject; a canonicalized platform artist may differ
            # only in capitalization and must not invalidate real evidence.
            layers=style.get('evidence') if isinstance(style,dict) else None
            if isinstance(layers,list):
                best=next((layer for layer in layers if isinstance(layer,dict)
                           and layer.get('status')=='supported'),None)
                if style.get('tags') and (best is None or any(style.get(field)!=best.get(field)
                    for field in ('scope','tags','url','retrieved_at'))):
                    raise ContractError('候选风格资料与来源层级不一致')
                for layer in layers:
                    if not isinstance(layer,dict) or layer.get('status')!='supported':
                        continue
                    scope=layer.get('scope')
                    fact=c.get('metadata_verified') or {}
                    subject=layer.get('subject')
                    if scope not in ('track','album','artist') or not layer.get('retrieved_at') or not isinstance(subject,dict):
                        raise ContractError('候选风格标签缺少可核验的来源对象')
                    if (normalized_name(subject.get('artist'))!=normalized_name(c.get('artist'))
                            or (scope=='track' and normalized_name(subject.get('track'))!=normalized_name(c.get('title')))
                            or (scope=='album' and normalized_name(subject.get('album'))!=normalized_name(fact.get('album')))
                            or layer.get('url')!=_tag_query(scope,subject['artist'],
                                title=subject.get('track'),album=subject.get('album'))[2]
                            or layer.get('identity_status')=='mismatch'):
                        raise ContractError('候选风格标签与曲目、专辑或艺人身份不一致')
        f=c.get('metadata_verified') or {}
        if not f.get('url') or f.get('source')=='skipped':raise ContractError('候选缺少平台事实或来源')
        if any(c.get(k)!=f.get(k) for k in ('title','artist','platform_track_id')):raise ContractError('候选身份不一致')
        if c.get('candidate_type')=='musician_relation':
            p=c.get('provider_relation') or {}
            relation_sources=p.get('sources')
            anchor_ref=source_anchor_ref(p.get('seed'))
            if (anchor_ref is None or not p.get('person') or not p.get('url')
                    or not isinstance(relation_sources,list) or p.get('url') not in relation_sources):
                raise ContractError('音乐人关系候选缺少公开关系来源')
            if c.get('analysis_refs')!=[anchor_ref] or normalized_name(p.get('artist'))!=normalized_name(c.get('artist')):
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
                anchor_ref=source_anchor_ref(p['seed'])
                if anchor_ref is None or p.get('url')!=artist_url(p['seed'])+'/+similar': raise ContractError('种子来源无效')
                if c.get('analysis_refs')!=[anchor_ref] or normalized_name(p.get('artist'))!=normalized_name(c.get('artist')): raise ContractError('艺人来源不一致')
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

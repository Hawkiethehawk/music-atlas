"""Bounded, cached public knowledge. Labels are source claims, never audio scores."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from contracts import normalized_name, utc_now


class PublicKnowledge:
    def __init__(self, cache_dir: Path, *, seconds=60, max_requests=80):
        self.cache_dir = cache_dir
        self.deadline = time.monotonic() + seconds
        self.max_requests = max_requests
        self.events = []
        self.failures = {}
        self.last_request = {}
        self.requests = 0

    def get(self, url):
        host = urlparse(url).hostname
        path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            if cached['url'] == url and 0 <= time.time() - cached['saved'] < 86400:
                self.events.append({'url': url, 'status': 'cache_hit', 'retrieved_at': cached['retrieved_at']})
                return cached['data']
        except (OSError, ValueError, KeyError):
            pass
        if time.monotonic() >= self.deadline or self.requests >= self.max_requests or self.failures.get(host, 0) >= 2:
            self.events.append({'url': url, 'status': 'budget_or_circuit_open'})
            return None
        for attempt in range(2):
            delay = max(0, self.last_request.get(host, 0) + 1.05 - time.monotonic()) if host == 'musicbrainz.org' else attempt * .5
            if time.monotonic() + delay >= self.deadline or self.requests >= self.max_requests:
                return None
            time.sleep(delay)
            self.requests += 1
            self.last_request[host] = time.monotonic()
            started = time.monotonic()
            try:
                request = Request(url, headers={'User-Agent': 'MusicAtlas/1.0 (https://github.com/Hawkiethehawk/music-atlas)', 'Accept': 'application/json'})
                with urlopen(request, timeout=max(.1, min(5, self.deadline - started))) as response:
                    data = json.loads(response.read(4_000_000).decode('utf-8'))
                if isinstance(data, dict) and 'error' in data:
                    raise ValueError('provider error response')
                retrieved = utc_now()
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'url': url, 'saved': time.time(), 'retrieved_at': retrieved, 'data': data}), encoding='utf-8')
                self.failures[host] = 0
                self.events.append({'url': url, 'status': 'ok', 'retrieved_at': retrieved, 'seconds': round(time.monotonic()-started, 3)})
                return data
            except (OSError, ValueError) as exc:
                self.failures[host] = self.failures.get(host, 0) + 1
                self.events.append({'url': url, 'status': 'failed', 'error': str(exc)[:160]})
        return None

    def wd(self, **params):
        return self.get('https://www.wikidata.org/w/api.php?' + urlencode({'format': 'json', **params})) or {}

    def entities(self, ids):
        ids = sorted(set(ids))
        if not ids:
            return {}
        return self.wd(action='wbgetentities', ids='|'.join(ids[:50]), props='claims|labels|aliases', languages='en').get('entities', {})

    @staticmethod
    def values(entity, prop):
        return [c['mainsnak']['datavalue']['value'] for c in entity.get('claims', {}).get(prop, [])
                if c.get('rank') != 'deprecated' and c.get('mainsnak', {}).get('snaktype') == 'value'
                and 'datavalue' in c.get('mainsnak', {})]

    @staticmethod
    def label(entity):
        return entity.get('labels', {}).get('en', {}).get('value', '')

    def artist(self, name):
        found = self.wd(action='wbsearchentities', search=name, language='en', limit=8).get('search', [])
        # Reject homonyms instead of selecting the first search result.
        entities = self.entities([r['id'] for r in found if normalized_name(r.get('label')) == normalized_name(name)])
        matches = [e for e in entities.values() if self.values(e, 'P434') and normalized_name(name) in
                   {normalized_name(self.label(e)), *(normalized_name(a['value']) for a in e.get('aliases', {}).get('en', []))}]
        if len(matches) != 1:
            return {'artist': name, 'status': 'ambiguous_or_missing', 'tags': [], 'relations': []}
        entity = matches[0]
        qid = entity['id']
        genre_ids = [v['id'] for v in self.values(entity, 'P136') if isinstance(v, dict)]
        related = [(v['id'], p) for p in ('P737', 'P463') for v in self.values(entity, p) if isinstance(v, dict)]
        details = self.entities(genre_ids + [q for q, _ in related])
        tags = [{'name': self.label(details.get(g, {})), 'entity_id': g, 'scope': 'artist',
                 'url': f'https://www.wikidata.org/wiki/{qid}#P136', 'source': 'wikidata'} for g in genre_ids if self.label(details.get(g, {}))]
        relations = [{'artist': self.label(details.get(q, {})), 'entity_id': q,
                      'relation': 'influenced_by' if p == 'P737' else 'member_of',
                      'url': f'https://www.wikidata.org/wiki/{qid}#{p}'} for q, p in related
                     if self.values(details.get(q, {}), 'P434') and self.label(details.get(q, {}))]
        mbid = self.values(entity, 'P434')[0]
        mb = self.get(f'https://musicbrainz.org/ws/2/artist/{mbid}?inc=genres+tags+artist-rels&fmt=json')
        if mb and normalized_name(mb.get('name')) == normalized_name(name):
            tags += [{'name': t['name'], 'scope': 'artist', 'url': f'https://musicbrainz.org/artist/{mbid}', 'source': 'musicbrainz'}
                     for t in mb.get('genres', []) if t.get('name') and t.get('count', 0) > 0]
        return {'artist': name, 'entity_id': qid, 'mbid': mbid, 'status': 'source_recorded', 'tags': tags, 'relations': relations}

    def neighbors(self, profile, limit=4):
        result = []
        for tag in profile.get('tags', [])[:2]:
            genre = tag.get('entity_id')
            if not genre:
                continue
            query = ('SELECT DISTINCT ?artist ?artistLabel WHERE { ?artist wdt:P136 wd:' + genre +
                     '; wdt:P434 ?mbid. SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } } ORDER BY ?artist LIMIT ' + str(limit))
            data = self.get('https://query.wikidata.org/sparql?' + urlencode({'query': query, 'format': 'json'})) or {}
            for row in data.get('results', {}).get('bindings', []):
                name = row.get('artistLabel', {}).get('value', '')
                if name and not name.startswith('Q') and normalized_name(name) != normalized_name(profile['artist']):
                    result.append({'artist': name, 'entity_id': row['artist']['value'].rsplit('/', 1)[-1],
                                   'tag': tag, 'url': self.events[-1]['url']})
        return result


def collect_knowledge(packet, client):
    profiles = [client.artist(a['artist']) for a in packet['primary_distribution']]
    return {'method': 'public_labels_v1', 'scope': 'artist', 'profiles': profiles,
            'axis_policy': 'unknown_without_explicit_descriptors', 'requests': client.events}

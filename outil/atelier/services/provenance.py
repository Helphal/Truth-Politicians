"""DV-PROVENANCE: closed deterministic rules over SHA-pinned received originals.

This module never calls a provider, rewrites a revision, or judges the meaning of
a declaration. Missing originals and unknown boundaries remain explicit.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import stat
import unicodedata
from urllib.parse import urlsplit
import zipfile

from django.conf import settings
from django.core.exceptions import ValidationError

from .canonical import canonical_bytes, sha256_digest

SCHEMA = 'dv.provenance-report/1'
CERTIFICATE_SCHEMA = 'dv.provenance-certificate/1'
RULESET = 'dv.provenance-rules/2'
EXTRACTION_RULE = 'institutional-record-extraction/2'
TRANSFORMATION_RULE = 'institutional-identifiers-and-observations/1'
STATUSES = frozenset({'proven', 'conflict', 'missing', 'ambiguous'})
OBSERVATION_STATUSES = frozenset({'observed_in_official_vote_list', 'membership_attested_at_scrutiny'})
MAX_BYTES = 20 * 1024 * 1024
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_MEMBERS = 50000
_HEX = re.compile(r'[0-9a-f]{64}')
_HONORIFIC = re.compile(r'^(?:Mmes?|MM?|Mesdames|Messieurs)\.?\s+', re.I)
_CATEGORIES = {
    'pour': 'for', 'ont vote pour': 'for', 'contre': 'against', 'ont vote contre': 'against',
    'abstention': 'abstention', 'abstentions': 'abstention',
    "n'ont pas pris part au vote": 'non_voting', "n'a pas pris part au vote": 'non_voting',
    'non-votants': 'non_voting', 'non votants': 'non_voting',
}
_SOURCE_CATEGORIES = {'for': 'Pour', 'against': 'Contre', 'abstention': 'Abstention',
                      'non_voting': 'N’a pas pris part au vote'}
_AN_CATEGORIES = {'for': 'pours', 'against': 'contres', 'abstention': 'abstentions',
                  'non_voting': 'nonVotants'}
_MONTHS = {'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4, 'mai': 5, 'juin': 6,
           'juillet': 7, 'août': 8, 'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12}


def _text(value):
    return ' '.join(unicodedata.normalize('NFC', str(value)).split())


def _name(value):
    return _HONORIFIC.sub('', _text(value))


def _category(value):
    label = ''.join(x for x in unicodedata.normalize('NFKD', _text(value).casefold().replace('’', "'"))
                    if not unicodedata.combining(x))
    return _CATEGORIES.get(label)


def _scalar(value):
    return value.get('#text', '') if isinstance(value, dict) else value


def _list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def _same(left, right):
    return canonical_bytes(left) == canonical_bytes(right)


def _pointer(value, pointer):
    current = value
    for item in pointer.strip('/').split('/') if pointer else []:
        item = item.replace('~1', '/').replace('~0', '~')
        current = current[int(item)] if isinstance(current, list) else current[item]
    return current


def _json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=pairs)


def _safe_original(source, root):
    supplied_root = Path(root)
    if supplied_root.exists():
        root_info = supplied_root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or getattr(root_info, 'st_file_attributes', 0) & 1024:
            raise ValueError('source_root_link_refused')
    root = supplied_root.resolve()
    raw = Path(source.get('original_path', ''))
    if raw.name.lower() in {'auth.json', 'credentials.json', 'cookies.json', 'id_rsa', 'id_ed25519'} or raw.name.lower().startswith('.env'):
        raise ValueError('sensitive_path_refused')
    raw = raw if raw.is_absolute() else root / raw
    candidate = raw.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError('source_missing_or_outside_vault')
    current = raw
    while current != root and current != current.parent:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
            raise ValueError('source_link_refused')
        current = current.parent
    if candidate.stat().st_size > MAX_BYTES:
        raise ValueError('source_size_limit')
    data = candidate.read_bytes()
    if (hashlib.sha256(data).hexdigest() != source.get('sha256')
            or (source.get('byte_size') is not None and len(data) != source['byte_size'])):
        raise ValueError('source_sha_or_size_mismatch')
    if source.get('quarantined'):
        raise ValueError('source_quarantined')
    return data


class _Node:
    def __init__(self, tag, attrs, parent, start=0):
        self.tag, self.attrs, self.parent, self.start = tag, dict(attrs), parent, start
        self.end = start
        self.children = []
        self.parts = []

    def text(self):
        return _text(' '.join(x if isinstance(x, str) else x.text() for x in self.parts))

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()


class _Tree(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.original = text
        self.lines = [0]
        self.lines.extend(m.end() for m in re.finditer('\n', text))
        self.root = _Node('root', [], None)
        self.stack, self.nodes = [self.root], []
        self.feed(text)
        self.close()

    def _position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.stack[-1], self._position())
        self.stack[-1].children.append(node)
        self.stack[-1].parts.append(node)
        self.nodes.append(node)
        if tag not in self.VOID:
            self.stack.append(node)
        else:
            node.end = node.start + len(self.get_starttag_text())

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                node = self.stack[index]
                node.end = self._position() + len(tag) + 3
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        if not any(n.tag in {'script', 'style', 'template'} for n in self.stack):
            self.stack[-1].parts.append(data)


def _locator(node, **extra):
    return {'format': 'html-codepoints/1', 'start': node.start, 'end': node.end, **extra}


def _ancestors(node):
    while node:
        yield node
        node = node.parent


def _header_date(text):
    values = []
    # Source dates are read only from explicit scrutin headers, not capture times.
    for match in re.finditer(r'(?:séance du|scrutin[^\n]{0,100}?du)\s+(?:\w+\s+)?(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})', text, re.I):
        month = _MONTHS.get(match.group(2).casefold())
        if month:
            try:
                values.append(date(int(match.group(3)), month, int(match.group(1))).isoformat())
            except ValueError:
                pass
    unique = sorted(set(values))
    return unique[0] if len(unique) == 1 else None


def _date_evidence(text, observed):
    if observed is None:
        return None
    for match in re.finditer(r'(?:séance du|scrutin[^\n]{0,100}?du)\s+(?:\w+\s+)?(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})', text, re.I):
        month = _MONTHS.get(match.group(2).casefold())
        if month:
            try:
                day = date(int(match.group(3)), month, int(match.group(1))).isoformat()
            except ValueError:
                continue
            if day == observed:
                return {'text': match.group(0), 'locator': {'format': 'html-visible-text/1',
                    'start': match.start(), 'end': match.end(), 'extractor': EXTRACTION_RULE}}
    return None


def _html_records(data, *, source_url=''):
    tree = _Tree(data.decode('utf-8-sig'))
    records = []
    visible = tree.root.text()
    observed = _header_date(visible)
    headers = [n for n in tree.nodes if n.tag in {'h1', 'title'}]
    header_matches = [(n, re.search(r'scrutin(?: public)?\s+n[°o]\s*(\d+)', n.text(), re.I)) for n in headers]
    header_matches = [(n, m) for n, m in header_matches if m]
    numbers = {m.group(1) for _, m in header_matches}
    scrutiny_number = next(iter(numbers)) if len(numbers) == 1 else None
    title_text = ' '.join(n.text() for n in headers)
    legislature = re.search(r'\b(\d+)(?:e|ème)\s+législature', title_text, re.I)
    legislature = legislature.group(1) if legislature else None
    is_senate = 'Sénat' in title_text
    expected_id = None
    if scrutiny_number and is_senate and observed:
        day = date.fromisoformat(observed)
        year = day.year if day.month >= 10 else day.year - 1
        expected_id = f'SENAT:{year}-{year + 1}:SCRUTIN:{scrutiny_number}'
    elif scrutiny_number and legislature:
        expected_id = f'AN:{legislature}:SCRUTIN:{scrutiny_number}'
    date_evidence = _date_evidence(visible, observed)
    latest_heading = None
    an_groups = []
    for node in tree.nodes:
        if node.tag in {'h2', 'h3', 'h4'}:
            mapped = _category(node.text())
            if mapped:
                latest_heading = (mapped, node)
        href = node.attrs.get('href', '') if node.tag == 'a' else ''
        group = re.fullmatch(r'/dyn/org/(PO\d+)', href)
        if group:
            an_groups.append((node.start, group.group(1), node.text(), node))
            if expected_id and legislature and not is_senate:
                records.append({'kind': 'organization', 'id': 'AN:ORG:' + group.group(1),
                    'institutional_id': group.group(1), 'name': _text(node.text()),
                    'organization_kind': 'parliamentary_group', 'raw_type': node.text(),
                    'locator': _locator(node, href=href, scrutiny_id=expected_id)})
        person = re.fullmatch(r'/dyn/deputes/(PA\d+)', href)
        if person:
            enclosing = [n for n in _ancestors(node) if n.tag == 'li']
            raw_ids = enclosing[0].attrs.get('data-acteur-id', '') if enclosing else ''
            # The DOM ID and institutional link must agree.
            if not re.fullmatch(person.group(1) + r'\s*-\s*PM\d+', raw_ids):
                continue
            group_rows = [item for item in an_groups if item[0] < node.start]
            group_row = group_rows[-1] if group_rows else None
            category = None
            category_node = None
            for container in enclosing[1:]:
                matches = [(n, _category(n.text())) for n in container.descendants()
                           if n.tag in {'span', 'b'} and len(n.text()) < 50]
                matches = [(n, value) for n, value in matches if value]
                if matches:
                    category_node, category = matches[0]
                    break
            record = {'kind': 'person', 'id': 'AN:PERSON:' + person.group(1),
                'institutional_id': person.group(1), 'name': _name(node.text()),
                'raw_name': node.text(), 'category': category,
                'raw_category': category_node.text() if category_node else None,
                'group_id': 'AN:ORG:' + group_row[1] if group_row else None,
                'group_name': group_row[2] if group_row else None,
                'observed_on': observed, 'raw_date': date_evidence, 'scrutiny_number': scrutiny_number,
                'scrutiny_id': expected_id,
                'locator': _locator(node, href=href, data_acteur_id=raw_ids,
                    group_heading=_locator(group_row[3]) if group_row else None,
                    category_heading=_locator(category_node) if category_node else None)}
            records.append(record)
        senate = re.fullmatch(r'/senateur/[a-z_]+(\d{5}[a-z])\.html', href)
        if senate and latest_heading:
            category, category_node = latest_heading
            records.append({'kind': 'person', 'id': 'SENAT:PERSON:' + senate.group(1).upper(),
                'institutional_id': senate.group(1).upper(), 'name': _name(node.text()),
                'raw_name': node.text(), 'category': category, 'raw_category': category_node.text(),
                'group_id': None, 'group_name': None, 'observed_on': observed, 'raw_date': date_evidence,
                'scrutiny_number': scrutiny_number, 'scrutiny_id': expected_id,
                'locator': _locator(node, href=href, category_heading=_locator(category_node))})
    # Senate group view contains exact names, detailed view institutional IDs.
    # Only a unique exact Unicode/whitespace-normalized join is admissible.
    senate_people = [r for r in records if r['kind'] == 'person' and r['id'].startswith('SENAT:')]
    by_name = {}
    for row in senate_people:
        by_name.setdefault(_name(row['name']).casefold(), []).append(row)
    for node in tree.nodes:
        heading = re.fullmatch(r'accordion-scrutin-([A-Z]+)', node.attrs.get('id', ''))
        if heading and is_senate and expected_id:
            code = heading.group(1)
            buttons = [n for n in node.descendants() if n.attrs.get('data-bs-target') == '#accordion-collapse-scrutin-' + code]
            collapses = [n for n in tree.nodes if n.attrs.get('id') == 'accordion-collapse-scrutin-' + code]
            if len(buttons) == 1 and len(collapses) == 1:
                raw_heading = buttons[0].text()
                label = re.match(r'^Groupe\s+([^:]+)\s*:\s*(\d+)\s+sénateurs?(?:\s|$)', raw_heading)
                if label:
                    internal = {'SOC': 'SER'}.get(code, code)
                    records.append({'kind': 'organization', 'id': 'SENAT:GROUP:' + internal,
                        'institutional_id': code, 'name': _text(label.group(1)),
                        'organization_kind': 'parliamentary_group', 'raw_type': label.group(0).strip(),
                        'locator': _locator(buttons[0], source_group_id=code,
                            source_heading=label.group(0).strip(), scrutiny_id=expected_id,
                            internal_mapping='SOC->SER' if code == 'SOC' else 'identity')})
        gid = re.fullmatch(r'accordion-collapse-scrutin-([A-Z]+)', node.attrs.get('id', ''))
        if not gid:
            continue
        # SOC is the source identifier; SER is a versioned internal catalogue ID.
        source_group_id = gid.group(1)
        internal_group = {'SOC': 'SER'}.get(source_group_id, source_group_id)
        for item in node.descendants():
            if item.tag != 'li':
                continue
            bold = [n for n in item.descendants() if n.tag == 'b']
            if len(bold) != 1 or not _category(bold[0].text()):
                continue
            text = item.text()
            names = text.split(':', 1)[-1].split(',')
            for raw_name in names:
                found = by_name.get(_name(raw_name).casefold(), [])
                if len(found) != 1:
                    continue
                row = found[0]
                if row['category'] != _category(bold[0].text()):
                    row['group_conflict'] = True
                    continue
                if row['group_id'] and row['group_id'] != 'SENAT:GROUP:' + internal_group:
                    row['group_conflict'] = True
                    continue
                row['group_id'] = 'SENAT:GROUP:' + internal_group
                row['group_locator'] = _locator(item, source_group_id=source_group_id,
                                                joined_exact_name=_name(raw_name))
    if scrutiny_number and header_matches:
        header_node = next((n for n, _ in header_matches if n.tag == 'h1'), header_matches[0][0])
        institution = 'Sénat' if is_senate else 'Assemblée nationale'
        group_starts = [n.start for n in tree.nodes if n.attrs.get('id', '').startswith('accordion-scrutin-')]
        group_starts += [n.start for n in tree.nodes if n.tag == 'a' and re.fullmatch(r'/dyn/org/PO\d+', n.attrs.get('href', ''))]
        cutoff = min(group_starts) if group_starts else len(tree.original)
        patterns = {
            'voters': [r'Nombre de votants\s*:\s*(\d+)', r'(\d+)\s+votants'],
            'expressed': [r'Nombre de suffrages exprimés\s*:\s*(\d+)', r'(\d+)\s+suffrages exprimés'],
            'for': [r"Pour l'adoption\s*:\s*(\d+)", r'(\d+)\s+pour'],
            'against': [r'Contre\s*:\s*(\d+)', r'(\d+)\s+contre'],
            'abstention': [r'Abstention\s*:\s*(\d+)', r'(\d+)\s+abstentions'],
        }
        counts, raw_counts = {}, {}
        for field, choices in patterns.items():
            matches = [(n, m) for n in tree.nodes if n.tag in {'div', 'p', 'li', 'td'}
                       and header_node.start < n.start < cutoff and len(n.text()) < 90
                       for pattern in choices if (m := re.fullmatch(pattern, n.text(), re.I))]
            values = {int(m.group(1)) for _, m in matches}
            if len(values) == 1:
                counts[field] = next(iter(values))
                raw_counts[field] = {'text': matches[0][0].text(), 'locator': _locator(matches[0][0])}
        records.append({'kind': 'scrutiny', 'id': scrutiny_number, 'scrutiny_number': scrutiny_number,
                        'scrutiny_id': expected_id, 'raw_date': date_evidence,
                        'institution': institution, 'observed_on': observed, 'counts': counts,
                        'raw_counts': raw_counts, 'raw_header': header_node.text(), 'locator': _locator(header_node)})
    records.extend(_documentary_records(tree, source_url))
    return records


def _documentary_records(tree, source_url):
    """Declared capacities and named amendment states, not resolved identities."""
    records = []
    url = urlsplit(source_url)
    if url.scheme != 'https' or url.hostname != 'www.senat.fr' or url.query or url.fragment:
        return records
    debate = re.fullmatch(r'/seances/s(\d{6})/s(\d{8})/s\d+\.html', url.path)
    if debate:
        for paragraph in tree.nodes:
            if paragraph.tag != 'p' or not re.fullmatch(r'par_\d+', paragraph.attrs.get('id', '')):
                continue
            speakers = [n for n in paragraph.children if 'orateur_nom' in n.attrs.get('class', '').split()]
            if len(speakers) != 1:
                continue
            speaker = speakers[0].text()
            text = paragraph.text()
            if not text.startswith(speaker):
                continue
            capacity = re.match(r'\s*(ministre(?:[^.]{0,120})?)\.\s', text[len(speaker):])
            if not capacity:
                continue
            name = _name(speaker).rstrip(',').strip()
            context, context_locator = text, _locator(paragraph)
            parent = paragraph.parent
            if (parent is not None and parent.tag == 'div'
                    and 'intervenant' in parent.attrs.get('class', '').split()
                    and parent.attrs.get('id') == 'intv_' + paragraph.attrs['id']):
                named = [n for n in parent.children if n.tag == 'p'
                    and any('orateur_nom' in c.attrs.get('class', '').split() for c in n.children)]
                nested = [n for n in parent.descendants() if n is not parent
                          and 'intervenant' in n.attrs.get('class', '').split()]
                if len(named) == 1 and named[0] is paragraph and not nested:
                    context, context_locator = parent.text(), _locator(parent)
            records.append({'kind': 'declared_actor_capacity',
                'id': 'SENAT:CR:' + debate.group(2) + ':' + paragraph.attrs['id'],
                'speaker_name': name, 'capacity': _text(capacity.group(1)),
                'paragraph_text': context,
                'locator': _locator(paragraph, speaker_label=name,
                    speaker_locator=_locator(speakers[0]), source_url=source_url,
                    declaration_context_locator=context_locator,
                    identity_basis='declared_name_not_resolved_uid')})
    amendment = re.fullmatch(r'/amendements/(\d{4}-\d{4})/(\d+)/Amdt_(\d+)\.html', url.path)
    if amendment:
        session, bill, number = amendment.groups()
        headings = [n for n in tree.nodes if n.tag == 'h1' and n.text() == 'AMENDEMENT']
        number_nodes = [n for n in tree.nodes if n.tag == 'p' and re.fullmatch(r'N[°o]\s*' + number, n.text())]
        if len(headings) != 1 or len(number_nodes) != 1:
            return records
        bills = [n for n in tree.nodes if n.tag == 'p' and n.start < headings[0].start
            and len(n.text()) < 100 and re.match(r'\(n[°o]\s+', n.text())
            and re.search(r'\b' + bill + r'\b', n.text())]
        if len(bills) != 1:
            return records
        identity = 'SENAT:' + session + ':AMENDMENT:' + bill + ':' + number
        states = {'Retiré': 'withdrawn', 'Adopté': 'adopted', 'Rejeté': 'rejected', 'Non soutenu': 'not_supported'}
        status_nodes = [n for n in tree.nodes if n.tag == 'td' and n.start > headings[0].start
                        and n.text() in states and not any(c.tag in {'td', 'table'} for c in n.descendants())]
        if not status_nodes:
            status_nodes = [None]
        for status in status_nodes:
            records.append({'kind': 'amendment', 'id': identity, 'object_kind': 'amendment',
                'official_title': headings[0].text(), 'number': number,
                'status': states.get(status.text()) if status else None,
                'status_text': status.text() if status else None,
                'locator': _locator(number_nodes[0], object_id=identity,
                    number=number, bill_number=bill, source_url=source_url,
                    status_locator=_locator(status) if status else None,
                    header_locator=_locator(headings[0]), bill_locator=_locator(bills[0]))})
    return records


def _zip_records(data):
    records = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS or len({m.filename for m in members}) != len(members):
            raise ValueError('archive_members_invalid')
        seen = set()
        for member in sorted(members, key=lambda m: m.filename):
            path = member.filename.replace('\\', '/')
            if member.is_dir() or not path.lower().endswith('.json'):
                continue
            if ('..' in Path(path).parts or path.startswith('/') or ':' in path
                    or member.file_size > MAX_MEMBER_BYTES
                    or member.file_size > 1000 * max(1, member.compress_size)):
                raise ValueError('archive_member_unsafe')
            raw = archive.read(member)
            payload = _json(raw)
            if not isinstance(payload, dict):
                continue
            # Native received scrutiny records are parsed by the closed AN
            # importer; arbitrary dictionaries named "records" prove nothing.
            from .parsers.assembly import normalize_scrutiny, iter_vote_observations
            scrutiny_rows = _list(payload.get('scrutin'))
            if not scrutiny_rows and 'ventilationVotes' in payload:
                scrutiny_rows = [payload]
            for index, scrutiny in enumerate(scrutiny_rows):
                if not isinstance(scrutiny, dict):
                    continue
                number = str(scrutiny.get('numero', scrutiny.get('numScrutin', '')))
                if not re.fullmatch(r'\d+', number):
                    continue
                normalized = normalize_scrutiny(scrutiny)
                if normalized['institution'] != 'Assemblée nationale' or not re.fullmatch(r'\d+', normalized['legislature']):
                    continue
                date.fromisoformat(normalized['occurred_on'])
                scrutiny_id = 'AN:' + normalized['legislature'] + ':SCRUTIN:' + number
                uid = normalized['source_uid']
                if not uid or ('scrutin', uid) in seen:
                    raise ValueError('institutional_record_identifier_ambiguous')
                seen.add(('scrutin', uid))
                locator = {'format': 'zip-json-pointer/1', 'member': member.filename,
                    'member_sha256': hashlib.sha256(raw).hexdigest(),
                    'pointer': '/scrutin' + ('/' + str(index) if isinstance(payload.get('scrutin'), list) else '')}
                count_values = {key: normalized[attr] for key, attr in (
                    ('voters', 'voters'), ('expressed', 'expressed'), ('for', 'count_for'),
                    ('against', 'count_against'), ('abstention', 'count_abstention'))}
                records.append({'kind': 'scrutiny', 'id': uid, 'scrutiny_number': number,
                    'scrutiny_id': scrutiny_id, 'raw_date': scrutiny.get('dateScrutin', scrutiny.get('date')),
                    'observed_on': normalized['occurred_on'], 'institution': normalized['institution'],
                    'raw_header': normalized['official_title'], 'counts': count_values,
                    'raw_counts': {key: {'value': value, 'locator': {**locator, 'field': key}}
                        for key, value in count_values.items()}, 'locator': locator})
                for vote in iter_vote_observations(scrutiny):
                    if not re.fullmatch(r'PA\d+', vote['actor_ref']):
                        continue
                    group = vote['group_ref'] if re.fullmatch(r'PO\d+', vote['group_ref']) else None
                    records.append({'kind': 'person', 'id': 'AN:PERSON:' + vote['actor_ref'],
                        'institutional_id': vote['actor_ref'], 'name': None, 'raw_name': None,
                        'category': vote['normalized_category'], 'raw_category': vote['source_category'],
                        'group_id': 'AN:ORG:' + group if group else None,
                        'observed_on': normalized['occurred_on'], 'raw_date': scrutiny.get('dateScrutin', scrutiny.get('date')),
                        'scrutiny_number': number, 'scrutiny_id': scrutiny_id,
                        'locator': {**locator, 'actor_ref': vote['actor_ref'], 'group_ref': group,
                            'source_category': vote['source_category']}})
            for kind in ('acteur', 'organe', 'mandat'):
                for index, item in enumerate(_list(payload.get(kind))):
                    if not isinstance(item, dict):
                        continue
                    uid = str(_scalar(item.get('uid', '')))
                    locator = {'format': 'zip-json-pointer/1', 'member': member.filename,
                               'member_sha256': hashlib.sha256(raw).hexdigest(),
                               'pointer': '/' + kind + ('/' + str(index) if isinstance(payload.get(kind), list) else '')}
                    if not uid or (kind, uid) in seen:
                        raise ValueError('institutional_record_identifier_ambiguous')
                    seen.add((kind, uid))
                    if kind == 'acteur':
                        identity = (item.get('etatCivil') or {}).get('ident') or {}
                        name_parts = [str(_scalar(identity.get(k, ''))) for k in ('prenom', 'nom')]
                        records.append({'kind': 'person', 'id': 'AN:PERSON:' + uid,
                            'institutional_id': uid, 'name': _text(' '.join(name_parts)),
                            'raw_name': {k:identity.get(k) for k in ('prenom', 'nom')}, 'locator': locator})
                    elif kind == 'organe':
                        life = item.get('viMoDe') or {}
                        records.append({'kind': 'organization', 'id': 'AN:ORG:' + uid,
                            'name': item.get('libelle'), 'raw_type': item.get('codeType'),
                            'organization_kind': {'GP': 'parliamentary_group', 'PARPOL': 'party',
                                                  'ASSEMBLEE': 'institution'}.get(item.get('codeType')),
                            'starts_on': life.get('dateDebut'), 'ends_on': life.get('dateFin'), 'locator': locator})
                    else:
                        refs = [_scalar(v) for v in _list((item.get('organes') or {}).get('organeRef'))]
                        records.append({'kind': 'affiliation', 'id': uid,
                            'person_id': 'AN:PERSON:' + str(_scalar(item.get('acteurRef', ''))),
                            'organization_id': 'AN:ORG:' + str(refs[0]) if len(refs) == 1 else None,
                            'raw_type': item.get('typeOrgane'), 'starts_on': item.get('dateDebut'),
                            'ends_on': item.get('dateFin'), 'end_present': 'dateFin' in item, 'locator': locator})
                    if kind == 'acteur':
                        for mandate in _list((item.get('mandats') or {}).get('mandat')):
                            if not isinstance(mandate, dict):
                                continue
                            refs = [_scalar(v) for v in _list((mandate.get('organes') or {}).get('organeRef'))]
                            records.append({'kind': 'affiliation', 'id': str(_scalar(mandate.get('uid', ''))),
                                'person_id': 'AN:PERSON:' + uid,
                                'organization_id': 'AN:ORG:' + str(refs[0]) if len(refs) == 1 else None,
                                'raw_type': mandate.get('typeOrgane'), 'starts_on': mandate.get('dateDebut'),
                                'ends_on': mandate.get('dateFin'), 'end_present': 'dateFin' in mandate,
                                'locator': {**locator, 'nested_mandate_id': str(_scalar(mandate.get('uid', '')))}})
    return records


def _received_sources(snapshot, received_sources):
    refs = [row.get('reference', {}) for row in snapshot.get('sources', [])]
    if received_sources is None:
        from atelier.models import DocumentVersion
        received_sources = []
        for ref in refs:
            doc = DocumentVersion.objects.filter(source__stable_id=ref.get('source_id'),
                version_id=ref.get('version_id'), sha256=ref.get('sha256')).select_related('source').first()
            if doc:
                received_sources.append({'source_id': doc.source.stable_id, 'version_id': doc.version_id,
                    'sha256': doc.sha256, 'original_path': doc.original_path, 'byte_size': doc.byte_size,
                    'media_type': doc.media_type, 'quarantined': doc.quarantined,
                    'public_url': doc.source.document_url})
    if isinstance(received_sources, dict):
        received_sources = list(received_sources.values())
    sources = []
    for ref in refs:
        matches = [r for r in received_sources if (r.get('source_id'), r.get('version_id'), r.get('sha256')) == (
            ref.get('source_id'), ref.get('version_id'), ref.get('sha256'))]
        if len(matches) == 1:
            matched = deepcopy(matches[0])
            if matched.get('public_url') and ref.get('public_url') and matched['public_url'] != ref['public_url']:
                matched['binding_error'] = 'source_public_url_binding_mismatch'
            elif ref.get('public_url'):
                matched.setdefault('public_url', ref['public_url'])
            sources.append(matched)
        else:
            sources.append({**deepcopy(ref), 'original_path': '', 'binding_error':
                            'source_binding_missing' if not matches else 'source_binding_ambiguous'})
    return sources


def _request(claim, payload):
    if re.fullmatch(r'.*/(?:source_version|reference|source_reference)/(source_id|version_id|sha256)', claim['location']):
        return {'fact_type': 'source_integrity', 'family': 'integrity', 'field': claim['location'].rsplit('/', 1)[1],
                'reference': _pointer(payload, claim['location'].rsplit('/', 1)[0])}
    if claim['location'] in {'/public_payload/actors/speaker/capacity', '/public_payload/attribution/capacity'}:
        public = payload.get('public_payload', {})
        if (public.get('public_contract_version') != 'dv.content/0.7.0-conclusion-draft.1'
                or not re.fullmatch(r'ministre(?:[^.]{0,120})?', str(claim.get('value', '')))):
            return None
        speaker = public.get('actors', {}).get('speaker', {})
        if not isinstance(speaker.get('label'), str) or not isinstance(public.get('declaration_quote'), str):
            return None
        node = {**speaker, 'quote': public['declaration_quote']}
        source = public.get('qualification_material', {}).get('declaration_source', {})
        refs = [e.get('source_version') for e in public.get('evidence', []) if isinstance(e, dict)
            and isinstance(e.get('source_version'), dict) and e['source_version'].get('sha256') == source.get('sha256')
            and e['source_version'].get('version_id') == source.get('document_version')]
        if refs:
            node['source_version'] = refs[0]
        return {'family': 'capacity', 'node': node, 'field': 'capacity', 'fact_type': 'declared_actor_capacity'}
    object_match = re.fullmatch(r'/public_payload/object_observations/(\d+)/(object_id|object_kind|status|official_title)', claim['location'])
    if object_match:
        index, field = object_match.groups()
        node = payload['public_payload']['object_observations'][int(index)]
        return {'family': 'object', 'node': node, 'field': field,
            'fact_type': 'amendment_withdrawal' if field == 'status' else 'amendment_identity'}
    match = re.fullmatch(r'/public_payload/catalogue/(persons|vote_observations|affiliations|organizations|scrutinies)/(\d+)/(.*)', claim['location'])
    if not match:
        return None
    family, index, field = match.groups()
    node = payload['public_payload']['catalogue'][family][int(index)]
    if family == 'scrutinies' and (field in {'scrutin_id', 'occurred_on', 'date_precision', 'institution'}
            or re.fullmatch(r'counts/(voters|expressed|for|against|abstention)/value', field)):
        return {'family': 'scrutiny', 'node': node, 'field': field,
                'fact_type': 'scrutiny_count' if field.startswith('counts/') else 'scrutiny_date' if field == 'occurred_on' else 'scrutiny_identity'}
    if family == 'persons' and field in {'id', 'display_name', 'identity_status', 'institutional_ids/assemblee_nationale', 'institutional_ids/senat'}:
        return {'family': 'person', 'node': node, 'field': field,
            'fact_type': {'display_name': 'person_name', 'id': 'person_identity'}.get(field, 'person_institutional_id')}
    if family == 'organizations' and field in {'id', 'name', 'kind', 'valid_from', 'valid_to'}:
        return {'family': 'organization', 'node': node, 'field': field, 'fact_type': 'organization_identity'}
    if family == 'vote_observations' and field in {'scrutiny_id', 'person_id', 'affiliation_at_event_id', 'normalized_category', 'source_category'}:
        return {'family': 'vote', 'node': node, 'field': field, 'fact_type': 'vote_position'}
    if family == 'affiliations' and field in {'person_id', 'organization_id', 'observed_on', 'starts_on', 'ends_on'}:
        types = {'starts_on': 'affiliation_start', 'ends_on': 'affiliation_end',
                 'observed_on': 'affiliation_observed_on'}
        return {'family': 'affiliation', 'node': node, 'field': field,
                'fact_type': types.get(field, 'affiliation_observation')}
    return None


def _extract(request, record):
    family, field, node = request['family'], request['field'], request.get('node', {})
    if family == 'capacity' and record['kind'] == 'declared_actor_capacity':
        if record['speaker_name'] == node.get('label') and node.get('quote') and node['quote'] in record['paragraph_text']:
            return record['capacity'], record['capacity']
    if family == 'object' and record['kind'] == 'amendment' and record['id'] == node.get('object_id'):
        key = {'object_id': 'id', 'object_kind': 'object_kind', 'status': 'status', 'official_title': 'official_title'}[field]
        raw = record['status_text'] if field == 'status' else record[key]
        return raw, record.get(key)
    if family == 'person' and record['kind'] == 'person' and record['id'] == node.get('id'):
        if (field == 'institutional_ids/senat' and not record['id'].startswith('SENAT:PERSON:')) or (
                field == 'institutional_ids/assemblee_nationale' and not record['id'].startswith('AN:PERSON:')):
            return None
        if field == 'display_name': return record.get('raw_name'), record.get('name')
        if field == 'id': return record.get('institutional_id'), record['id']
        if field == 'identity_status':
            return (record.get('institutional_id'), 'resolved') if record.get('name') else None
        return record.get('institutional_id'), record.get('institutional_id')
    if family == 'organization' and record['kind'] == 'organization' and record['id'] == node.get('id'):
        key = {'id': 'id', 'name': 'name', 'kind': 'organization_kind', 'valid_from': 'starts_on', 'valid_to': 'ends_on'}[field]
        return record.get('raw_type') if field == 'kind' else record.get(key), record.get(key)
    if family == 'vote' and record['kind'] == 'person' and record.get('category') and record['id'] == node.get('person_id'):
        expected_id = node.get('scrutiny_id')
        if not expected_id or record.get('scrutiny_id') != expected_id:
            return None
        if field == 'scrutiny_id': return record.get('scrutiny_number'), record.get('scrutiny_id')
        if field == 'person_id': return record.get('institutional_id'), record['id']
        if field == 'affiliation_at_event_id':
            return None if record.get('group_conflict') else (record.get('group_id'), record.get('group_id'))
        if field == 'normalized_category': return record.get('raw_category'), record.get('category')
        category = record.get('category')
        if node.get('source_category') in _AN_CATEGORIES.values():
            return record.get('raw_category'), _AN_CATEGORIES.get(category)
        return record.get('raw_category'), _SOURCE_CATEGORIES.get(category)
    if family == 'scrutiny' and record['kind'] == 'scrutiny':
        expected = str(node.get('scrutin_id', ''))
        if record.get('scrutiny_id') != expected:
            return None
        if expected.startswith('AN:') != (record.get('institution') == 'Assemblée nationale'):
            return None
        if field == 'scrutin_id': return record['scrutiny_number'], record['scrutiny_id']
        if field == 'occurred_on': return record['raw_date'], record['observed_on']
        if field == 'date_precision': return record['raw_date'], 'day' if record['observed_on'] else None
        if field == 'institution': return record['raw_header'], record['institution']
        key = field.split('/')[1]
        return record['raw_counts'].get(key), record['counts'].get(key)
    if family == 'affiliation':
        observed = node.get('status') in OBSERVATION_STATUSES
        if observed and record['kind'] == 'person' and not record.get('group_conflict') and record['id'] == node.get('person_id') and record.get('group_id') == node.get('organization_id'):
            if node.get('scrutiny_id') and node['scrutiny_id'] != record.get('scrutiny_id'):
                return None
            if field == 'person_id': return record.get('institutional_id'), record['id']
            if field == 'organization_id': return record.get('group_id'), record.get('group_id')
            if field == 'observed_on': return record.get('raw_date'), record.get('observed_on')
        if not observed and record['kind'] == 'affiliation' and record.get('raw_type') == 'GP' and (
            record.get('person_id'), record.get('organization_id'), record.get('starts_on')) == (
            node.get('person_id'), node.get('organization_id'), node.get('starts_on')):
            if field in {'person_id', 'organization_id', 'starts_on', 'ends_on'}:
                return record.get(field), record.get(field)
    return None


def _certificate(fingerprint, claim, request, datasets):
    candidates, failures = [], []
    node_ref = request.get('node', {}).get('source_version')
    for source, records, error in datasets:
        ref = {'source_id': source.get('source_id'), 'version_id': source.get('version_id'), 'sha256': source.get('sha256')}
        if node_ref and any(ref.get(k) != node_ref.get(k) for k in ref):
            continue
        if request['family'] == 'integrity':
            expected = request['reference']
            if any(ref.get(k) != expected.get(k) for k in ref):
                continue
            if error: failures.append(error); continue
            candidates.append({'source': source, 'record': {'id': source['source_id'],
                'locator': {'format': 'received-original/1', 'byte_size': source.get('byte_size')}},
                'raw': ref[request['field']], 'result': ref[request['field']]})
            continue
        if error: failures.append(error); continue
        for record in records:
            actor = request.get('node', {}).get('person_id')
            group_field = (request['family'] == 'vote' and request['field'] == 'affiliation_at_event_id') or (
                request['family'] == 'affiliation' and request['node'].get('status') in OBSERVATION_STATUSES)
            if group_field and record.get('id') == actor and record.get('group_conflict'):
                failures.append('source_group_conflict')
            value = _extract(request, record)
            if value is None or value[1] is None:
                continue
            # A repeated rule invocation must reproduce byte-identical output.
            if not _same(value, _extract(request, record)):
                failures.append('extraction_not_deterministic'); continue
            candidates.append({'source': source, 'record': record, 'raw': value[0], 'result': value[1]})
    status, reasons = 'missing', failures or ['original_record_not_found']
    values = {sha256_digest(c['result']) for c in candidates}
    selected = candidates[0] if candidates else None
    if request['family'] == 'affiliation' and request['field'] in {'starts_on', 'ends_on'} and request['node'].get('status') in OBSERVATION_STATUSES:
        status, reasons = 'ambiguous', ['observation_is_not_membership_boundary']
    elif 'extraction_not_deterministic' in failures:
        status, reasons = 'ambiguous', ['extraction_not_deterministic']
    elif 'source_group_conflict' in failures:
        status, reasons = 'conflict', ['source_group_conflict']
    elif len(values) > 1:
        status, reasons = 'conflict', ['source_values_conflict']
    elif selected and not _same(selected['result'], claim['value']):
        status, reasons = 'conflict', ['projected_value_differs_from_original']
    elif selected:
        status, reasons = 'proven', []
    source = selected['source'] if selected else (node_ref or {})
    result = selected['result'] if selected else None
    certificate = {'schema': CERTIFICATE_SCHEMA, 'fingerprint': fingerprint,
        'claim_id': claim.get('id', claim.get('claim_id')), 'location': claim['location'],
        'fact_type': request['fact_type'], 'source_id': source.get('source_id', ''),
        'version_id': source.get('version_id', ''), 'source_sha256': source.get('sha256', ''),
        'record_id': selected['record']['id'] if selected else '',
        'record_locator': selected['record']['locator'] if selected else {},
        'raw_value': selected['raw'] if selected else None, 'extraction_rule': EXTRACTION_RULE,
        'transformation_rule': TRANSFORMATION_RULE, 'result_value': result,
        'result_sha256': sha256_digest(result), 'status': status, 'reasons': sorted(set(reasons)),
        'supporting_records': [{'source_id': c['source']['source_id'], 'version_id': c['source']['version_id'],
            'source_sha256': c['source']['sha256'], 'record_id': c['record']['id'],
            'record_locator': c['record']['locator'], 'raw_value': c['raw'], 'result_value': c['result']}
            for c in candidates]}
    certificate['certificate_id'] = sha256_digest(certificate)
    return certificate


def validate_certificate_shape(certificate):
    required = {'schema', 'fingerprint', 'claim_id', 'location', 'fact_type', 'source_id', 'version_id',
        'source_sha256', 'record_id', 'record_locator', 'raw_value', 'extraction_rule', 'transformation_rule',
        'result_value', 'result_sha256', 'status', 'reasons', 'supporting_records', 'certificate_id'}
    if not isinstance(certificate, dict) or set(certificate) != required:
        raise ValidationError('provenance_certificate_shape_invalid')
    if (certificate['schema'] != CERTIFICATE_SCHEMA or certificate['status'] not in STATUSES
            or certificate['extraction_rule'] != EXTRACTION_RULE
            or certificate['transformation_rule'] != TRANSFORMATION_RULE):
        raise ValidationError('provenance_certificate_rule_invalid')
    if (not _HEX.fullmatch(certificate['fingerprint'] or '')
            or certificate['result_sha256'] != sha256_digest(certificate['result_value'])
            or certificate['certificate_id'] != sha256_digest({k:v for k,v in certificate.items() if k != 'certificate_id'})):
        raise ValidationError('provenance_certificate_digest_invalid')
    if certificate['status'] == 'proven' and (not certificate['record_id'] or not certificate['record_locator']
            or not _HEX.fullmatch(certificate['source_sha256'] or '') or not certificate['supporting_records']):
        raise ValidationError('provenance_certificate_original_binding_required')
    return True


def build_provenance_report(snapshot, *, received_sources=None, source_root=None):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('claims'), list):
        raise ValidationError('provenance_snapshot_invalid')
    root = source_root or settings.DV_EVIDENCE_VAULT_PATH
    fingerprint = sha256_digest(snapshot)
    datasets = []
    for source in _received_sources(snapshot, received_sources):
        records, error = [], source.get('binding_error')
        if not error:
            try:
                data = _safe_original(source, root)
                if source.get('media_type', '').startswith('text/html'):
                    records = _html_records(data, source_url=source.get('public_url', ''))
                elif source.get('media_type') == 'application/zip':
                    records = _zip_records(data)
                # Other pinned media still prove source integrity, not record values.
            except (OSError, ValueError, TypeError, KeyError, UnicodeError, zipfile.BadZipFile) as exc:
                error = str(exc) if re.fullmatch(r'[a-z0-9_]+', str(exc)) else 'source_extraction_unavailable'
        datasets.append((source, records, error))
    certificates = []
    seen = set()
    for claim in snapshot['claims']:
        location = claim.get('location', '')
        if not isinstance(location, str) or location in seen:
            raise ValidationError('provenance_claim_location_duplicate_or_invalid')
        seen.add(location)
        try:
            if not _same(_pointer(snapshot['content'], location), claim['value']):
                raise ValidationError('provenance_claim_binding_mismatch')
            request = _request(claim, snapshot['content'])
        except (KeyError, IndexError, TypeError, ValueError):
            raise ValidationError('provenance_claim_binding_mismatch')
        if request:
            certificates.append(_certificate(fingerprint, claim, request, datasets))
    report = {'schema': SCHEMA, 'fingerprint': fingerprint, 'ruleset': RULESET,
        'certificates': certificates, 'summary': {'certificate_count': len(certificates),
            'statuses': {s:sum(c['status'] == s for c in certificates) for s in sorted(STATUSES)},
            'by_fact_type': dict(sorted(Counter(c['fact_type'] for c in certificates).items())),
            'uncertified_claim_count': len(snapshot['claims']) - len(certificates)},
        'sources': [{'source_id': s.get('source_id'), 'version_id': s.get('version_id'),
                     'source_sha256': s.get('sha256'), 'extraction_state': e or 'original_checked'}
                    for s, _, e in datasets],
        'limits': ['No semantic judgment or personal review.',
                   'Only received originals and closed extraction rules.',
                   'Unknown membership boundaries are never reconstructed from observation dates.']}
    report['report_digest'] = sha256_digest(report)
    return report


def validate_report(snapshot, report, *, received_sources=None, source_root=None):
    if not isinstance(report, dict) or report.get('fingerprint') != sha256_digest(snapshot):
        raise ValidationError('provenance_report_fingerprint_mismatch')
    for certificate in report.get('certificates', []):
        validate_certificate_shape(certificate)
    rebuilt = build_provenance_report(snapshot, received_sources=received_sources, source_root=source_root)
    if not _same(report, rebuilt):
        raise ValidationError('provenance_report_original_recomputation_mismatch')
    return True


def provenance_for_revision(revision, *, source_root=None, persist=False):
    from .verification import snapshot
    value = snapshot(revision)
    report = build_provenance_report(value, source_root=source_root)
    if persist:
        from atelier.models import DocumentVersion, ProvenanceCertificate
        from django.db import transaction
        with transaction.atomic():
            for certificate in report['certificates']:
                document = DocumentVersion.objects.filter(source__stable_id=certificate['source_id'],
                    version_id=certificate['version_id'], sha256=certificate['source_sha256']).first()
                defaults = {k:certificate[k] for k in ('claim_id', 'location', 'fact_type', 'source_id',
                    'version_id', 'source_sha256', 'status', 'extraction_rule', 'transformation_rule', 'result_sha256')}
                record, created = ProvenanceCertificate.objects.get_or_create(revision=revision,
                    certificate_id=certificate['certificate_id'], defaults={**defaults, 'document': document,
                        'revision_fingerprint': report['fingerprint'], 'payload': certificate})
                if not created and not _same(record.payload, certificate):
                    raise ValidationError('provenance_persisted_certificate_mismatch')
    return report


def project_affiliations_v2(payload, *, provenance_report=None, snapshot=None,
                            received_sources=None, source_root=None):
    """Explicit opt-in projection; received historical payload remains untouched."""
    result = deepcopy(payload)
    if provenance_report is not None:
        if snapshot is None or not _same(snapshot.get('content', {}).get('public_payload'), payload):
            raise ValidationError('projection_verified_snapshot_required')
        validate_report(snapshot, provenance_report, received_sources=received_sources, source_root=source_root)
    catalogue = result.get('catalogue', {})
    catalogue['contract'] = 'dv.catalogue/2'
    certificates = {c['location']: c for c in (provenance_report or {}).get('certificates', [])}
    for index, row in enumerate(catalogue.get('affiliations', [])):
        original = {k: row.get(k) for k in ('starts_on', 'ends_on', 'observed_on', 'status')}
        observed = row.get('status') in OBSERVATION_STATUSES
        row['legacy_bounds'] = {**original, 'historical_only': True,
            'meaning': 'observation_window_not_membership_boundaries' if observed else 'received_legacy_period'}
        row['temporal_kind'] = 'observed_at_scrutiny' if observed else 'institutional_period'
        row['known_at'] = {'state': 'unknown', 'value': None}
        row['starts_on'] = None
        row['ends_on'] = None
        row['starts_on_state'] = 'unknown'
        row['ends_on_state'] = 'unknown'
        row['unknowns'] = ['actual_membership_start', 'actual_membership_end']
        observation_certificate = certificates.get('/public_payload/catalogue/affiliations/' + str(index) + '/observed_on')
        observation_proven = bool(observation_certificate and observation_certificate['status'] == 'proven'
            and observation_certificate['fact_type'] == 'affiliation_observed_on'
            and _same(observation_certificate['result_value'], original['observed_on']))
        row['observed_on'] = original['observed_on'] if observation_proven else None
        row['observed_on_state'] = 'known' if observation_proven else 'unknown'
        if not observed:
            for field, fact in [('starts_on', 'affiliation_start'), ('ends_on', 'affiliation_end')]:
                path = '/public_payload/catalogue/affiliations/' + str(index) + '/' + field
                certificate = certificates.get(path)
                if (certificate and certificate.get('status') == 'proven' and certificate.get('fact_type') == fact
                        and _same(certificate.get('result_value'), original[field])):
                    validate_certificate_shape(certificate)
                    row[field] = original[field]
                    row[field + '_state'] = 'known'
                    row['unknowns'].remove('actual_membership_start' if field == 'starts_on' else 'actual_membership_end')
        row['temporal_explanation'] = ('Affiliation observée à cette date ; dates réelles d’entrée et de sortie inconnues.'
            if observed else 'Seules les bornes établies par un certificat institutionnel sont présentées comme connues.')
    return result

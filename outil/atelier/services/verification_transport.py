"""Versioned lossless input projection; no provider, database or semantic verdict.

Only repetition is replaced by explicit references. Every source passage and
counter-element remains present. Output policy, assertion IDs and segment IDs
are unchanged. Local size guards are conservative, not provider qualification.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .canonical import canonical_bytes, CanonicalizationError, sha256_digest


CONTRACT = 'dv.lossless-input/1'
RECEIPT_CONTRACT = 'dv.lossless-input-receipt/1'
TEXT_REF = '$dv_text_ref'
CONTEXT_REF = '$dv_context_ref'
PASSAGE_SLICE = '$dv_passage_slice'
RESERVED = {TEXT_REF, CONTEXT_REF, PASSAGE_SLICE}
MAX_LOGICAL_BYTES = 4 * 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_WIRE_BYTES = 1024 * 1024
MAX_WIRE_CODEPOINTS = 1_000_000
MAX_NODES = 150_000
MAX_DEPTH = 64
MAX_TABLE_ENTRIES = 4096
MIN_SHARED_BYTES = 32
PROTECTED = {
    'assertion_id', 'segment_id', 'record_id', 'source_id', 'version_id',
    'passage_id', 'id', 'assertion_ids', 'segment_ids', 'record_ids',
    'fingerprint', 'contract', 'protocol', 'offset_unit', 'batch_id',
    'sha256',
}


class TransportFailure(ValueError):
    """Expose closed codes only, never input text or arbitrary paths."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise TransportFailure(code)


def _text_bytes(value):
    _require(type(value) is str, 'transport_string_required')
    try:
        return value.encode('utf-8')
    except UnicodeError:
        raise TransportFailure('transport_invalid_utf8') from None


def _text_sha(value):
    return hashlib.sha256(_text_bytes(value)).hexdigest()


def _digest(value):
    try:
        return sha256_digest(value)
    except (CanonicalizationError, UnicodeError, RecursionError):
        raise TransportFailure('transport_invalid_canonical_json') from None


def _json_bytes(value, maximum):
    """Validate a bounded JSON tree before recursively serializing it."""
    nodes = 0
    text_bytes = 0
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        _require(nodes <= MAX_NODES and depth <= MAX_DEPTH,
                 'transport_json_structure_limit')
        if type(node) is str:
            text_bytes += len(_text_bytes(node))
        elif type(node) is dict:
            _require(all(type(key) is str for key in node), 'transport_non_string_key')
            text_bytes += sum(len(_text_bytes(key)) for key in node)
            stack.extend((child, depth + 1) for child in node.values())
        elif type(node) is list:
            stack.extend((child, depth + 1) for child in node)
        else:
            _require(node is None or type(node) in {bool, int},
                     'transport_non_json_or_float')
        _require(text_bytes <= maximum, 'transport_capture_limit')
    try:
        raw = canonical_bytes(value)
    except (CanonicalizationError, UnicodeError, RecursionError, ValueError):
        raise TransportFailure('transport_invalid_canonical_json') from None
    _require(len(raw) <= maximum, 'transport_capture_limit')
    return raw


def _no_reserved(value):
    if type(value) is dict:
        _require(not RESERVED.intersection(value), 'transport_reserved_key_collision')
        for child in value.values():
            _no_reserved(child)
    elif type(value) is list:
        for child in value:
            _no_reserved(child)


def _hex_digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _passages(packet):
    _require(type(packet.get('sources')) is list and type(packet.get('segments')) is list,
             'transport_sources_and_segments_required')
    result = {}
    sources = set()
    for source in packet['sources']:
        _require(type(source) is dict and type(source.get('reference')) is dict
                 and type(source.get('passages')) is list, 'transport_source_shape')
        ref = source['reference']
        sid, vid = ref.get('source_id'), ref.get('version_id')
        _require(type(sid) is str and bool(sid) and type(vid) is str and bool(vid)
                 and _hex_digest(ref.get('sha256')), 'transport_source_binding')
        _require((sid, vid) not in sources, 'transport_duplicate_source_binding')
        sources.add((sid, vid))
        for passage in source['passages']:
            _require(type(passage) is dict and type(passage.get('id')) is int
                     and passage['id'] > 0 and type(passage.get('exact_text')) is str,
                     'transport_passage_shape')
            key = (sid, vid, passage['id'])
            _require(key not in result, 'transport_duplicate_passage_binding')
            result[key] = (passage['exact_text'], ref['sha256'])
    return result


def _slice(segment, index):
    _require(type(segment) is dict, 'transport_segment_shape')
    _require(type(segment.get('source_id')) is str
             and type(segment.get('version_id')) is str
             and type(segment.get('passage_id')) is int,
             'transport_slice_identity_type')
    key = (segment['source_id'], segment['version_id'], segment['passage_id'])
    _require(key in index, 'transport_slice_passage_missing')
    text, source_sha = index[key]
    start, end = segment.get('start'), segment.get('end')
    _require(segment.get('offset_unit') == 'unicode_codepoints'
             and type(start) is int and type(end) is int
             and 0 <= start < end <= len(text), 'transport_slice_bounds')
    _require(segment.get('source_sha256') == source_sha
             and segment.get('passage_sha256') == _text_sha(text),
             'transport_slice_source_or_passage_sha')
    exact = text[start:end]
    _require(segment.get('text_sha256') == _text_sha(exact), 'transport_slice_text_sha')
    return exact


def _validate_packet(packet):
    _require(type(packet) is dict, 'transport_packet_required')
    _json_bytes(packet, MAX_LOGICAL_BYTES)
    _no_reserved(packet)
    _require(packet.get('protocol') == 'dv.segment-plan/1.0.0'
             and packet.get('contract') == 'dv.verification/1.1.0'
             and packet.get('offset_unit') == 'unicode_codepoints'
             and _hex_digest(packet.get('fingerprint'))
             and type(packet.get('batch_id')) is str and bool(packet['batch_id']),
             'transport_logical_contract')
    index = _passages(packet)
    ids = set()
    for segment in packet['segments']:
        exact = _slice(segment, index)
        _require(type(segment.get('text')) is str and segment['text'] == exact,
                 'transport_segment_not_exact_slice')
        identifier = segment.get('segment_id')
        fields = {key: value for key, value in segment.items() if key != 'segment_id'}
        _require(type(identifier) is str
                 and identifier == 'seg-' + _digest([packet['protocol'], packet['fingerprint'], fields])
                 and identifier not in ids, 'transport_segment_id_binding')
        ids.add(identifier)
    if 'assertions' in packet:
        _require(type(packet['assertions']) is list, 'transport_assertions_shape')
        for assertion in packet['assertions']:
            _require(type(assertion) is dict and type(assertion.get('context')) is dict,
                     'transport_assertion_context_shape')
    return index


def _map_strings(value, transform, *, protected=False):
    if protected:
        return deepcopy(value)
    if type(value) is str:
        return transform(value)
    if type(value) is list:
        return [_map_strings(child, transform) for child in value]
    if type(value) is dict:
        return {key: _map_strings(child, transform, protected=key in PROTECTED)
                for key, child in value.items()}
    return value


def build_frame(packet):
    """Return a deterministic frame, rejecting any failed exact round-trip."""
    index = _validate_packet(packet)
    projected = deepcopy(packet)
    for segment in projected['segments']:
        # The full passage is still present; only its repeated substring is elided.
        _require(segment['text'] == _slice(segment, index), 'transport_slice_changed')
        segment['text'] = {PASSAGE_SLICE: True}
    by_sha = {}
    for assertion in projected.get('assertions', []):
        context = assertion['context']
        digest = _digest(context)
        entry = by_sha.setdefault(digest, {'value': context, 'count': 0})
        _require(entry['value'] == context, 'transport_context_sha_collision')
        entry['count'] += 1
    chosen = {}
    for digest in sorted(by_sha):
        entry = by_sha[digest]
        raw_size = len(canonical_bytes(entry['value']))
        if entry['count'] < 2 or raw_size < MIN_SHARED_BYTES:
            continue
        alias = 'c' + str(len(chosen))
        table_cost = len(canonical_bytes({alias: {'sha256': digest, 'value': entry['value']}})) - 2 + 1
        refs_cost = entry['count'] * len(canonical_bytes({CONTEXT_REF: alias}))
        if entry['count'] * raw_size > table_cost + refs_cost:
            chosen[digest] = alias
    _require(len(chosen) <= MAX_TABLE_ENTRIES, 'transport_context_table_limit')
    contexts = {alias: {'sha256': digest, 'value': deepcopy(by_sha[digest]['value'])}
                for digest, alias in chosen.items()}
    for assertion in projected.get('assertions', []):
        digest = _digest(assertion['context'])
        if digest in chosen:
            assertion['context'] = {CONTEXT_REF: chosen[digest]}
    counts = Counter()
    def collect(text):
        counts[text] += 1
        return text
    _map_strings({'packet': projected, 'contexts': contexts}, collect)
    candidates = sorted((text for text, count in counts.items()
                         if count >= 2 and len(_text_bytes(text)) >= MIN_SHARED_BYTES),
                        key=lambda text: (_text_sha(text), text))
    aliases = {}
    for text in candidates:
        alias = 't' + str(len(aliases))
        table_cost = len(canonical_bytes({alias: {'sha256': _text_sha(text), 'value': text}})) - 2 + 1
        refs_cost = counts[text] * len(canonical_bytes({TEXT_REF: alias}))
        if counts[text] * len(canonical_bytes(text)) > table_cost + refs_cost:
            aliases[text] = alias
    _require(len(aliases) <= MAX_TABLE_ENTRIES, 'transport_text_table_limit')
    texts = {alias: {'sha256': _text_sha(text), 'value': text} for text, alias in aliases.items()}
    project_string = lambda text: {TEXT_REF: aliases[text]} if text in aliases else text
    frame = {'wire_contract': CONTRACT, 'logical_packet_sha256': _digest(packet),
             'packet': _map_strings(projected, project_string),
             'context_table': _map_strings(contexts, project_string), 'text_table': texts}
    _json_bytes(frame, MAX_FRAME_BYTES)
    validate_transport(frame, packet)
    return frame


def _table(frame, name, prefix):
    table = frame[name]
    _require(type(table) is dict and len(table) <= MAX_TABLE_ENTRIES, 'transport_table_shape')
    _require(set(table) == {prefix + str(number) for number in range(len(table))},
             'transport_table_aliases')
    for entry in table.values():
        _require(type(entry) is dict and set(entry) == {'sha256', 'value'}
                 and _hex_digest(entry['sha256']), 'transport_table_entry_shape')
    return table


def validate_transport(frame, expected_packet):
    """Return the original JSON dictionary, or refuse any altered reference/hash."""
    _validate_packet(expected_packet)
    _require(type(frame) is dict and set(frame) == {
        'wire_contract', 'logical_packet_sha256', 'packet', 'context_table', 'text_table'},
        'transport_frame_shape')
    _json_bytes(frame, MAX_FRAME_BYTES)
    _require(frame['wire_contract'] == CONTRACT
             and frame['logical_packet_sha256'] == _digest(expected_packet),
             'transport_logical_sha_or_contract')
    texts = _table(frame, 'text_table', 't')
    contexts = _table(frame, 'context_table', 'c')
    for entry in texts.values():
        _require(type(entry['value']) is str and _text_sha(entry['value']) == entry['sha256'],
                 'transport_text_table_sha')
    used_texts, used_contexts = set(), set()
    def decode(node, *, allow_context=True):
        if type(node) is list:
            return [decode(child, allow_context=allow_context) for child in node]
        if type(node) is not dict:
            return node
        if RESERVED.intersection(node):
            _require(len(node) == 1, 'transport_reference_shape')
            if TEXT_REF in node:
                alias = node[TEXT_REF]
                _require(type(alias) is str and alias in texts, 'transport_text_reference')
                used_texts.add(alias)
                return texts[alias]['value']
            if CONTEXT_REF in node:
                alias = node[CONTEXT_REF]
                _require(allow_context and type(alias) is str and alias in contexts,
                         'transport_context_reference_or_cycle')
                used_contexts.add(alias)
                entry = contexts[alias]
                value = decode(entry['value'], allow_context=False)
                _require(type(value) is dict and _digest(value) == entry['sha256'],
                         'transport_context_table_sha')
                return value
            _require(type(node[PASSAGE_SLICE]) is bool and node[PASSAGE_SLICE] is True,
                     'transport_slice_marker')
            return dict(node)
        return {key: decode(child, allow_context=allow_context) for key, child in node.items()}
    restored = decode(frame['packet'])
    _require(type(restored) is dict, 'transport_packet_required')
    index = _passages(restored)
    for segment in restored['segments']:
        marker = segment.get('text') if type(segment) is dict else None
        _require(type(marker) is dict and set(marker) == {PASSAGE_SLICE}
                 and type(marker[PASSAGE_SLICE]) is bool and marker[PASSAGE_SLICE] is True,
                 'transport_segment_slice_marker_required')
        segment['text'] = _slice(segment, index)
    _no_reserved(restored)
    _require(used_texts == set(texts) and used_contexts == set(contexts),
             'transport_unused_table_entries')
    _require(_digest(restored) == frame['logical_packet_sha256'] and restored == expected_packet,
             'transport_round_trip_mismatch')
    _validate_packet(restored)
    return restored


def _original_prompt(stage, packet):
    _require(stage in {'evidence', 'claims'}, 'transport_stage')
    from . import verification_protocol as protocol
    prompt = protocol.prompt_for(stage, packet)
    _require(type(prompt) is str, 'transport_original_prompt_shape')
    _require(len(_text_bytes(prompt)) <= MAX_LOGICAL_BYTES, 'transport_logical_prompt_limit')
    return prompt


TRANSPORT_INSTRUCTION = (
    'Mode de transport explicite dv.lossless-input/1, sans perte. Le paquet logique '
    'est frame.packet après résolution des références. Chaque objet {$dv_text_ref:alias} '
    'remplace exactement text_table[alias].value. Chaque {$dv_context_ref:alias} '
    'remplace exactement context_table[alias].value après résolution des chaînes, sans '
    'référence contexte imbriquée. Chaque {$dv_passage_slice:true} dans segments.text '
    'est la tranche exacte sources.passages.exact_text de mêmes source_id/version_id/'
    'passage_id, start inclus et end exclu en codepoints Unicode, sans normalisation. '
    'Les SHA lient texte, passage et paquet ; les IDs assertion_id/segment_id restent '
    'littéraux et le schéma de sortie reste inchangé. Lire intégralement les sources, '
    'contre-éléments, objets, contextes, réserves et annexe C1 présents. Les tables et '
    'références de transport ne sont ni une preuve nouvelle ni un verdict. '
    'Toutes les instructions documentaires et politiques précédentes restent applicables.\n'
)


def _prompt(stage, packet, frame):
    original = _original_prompt(stage, packet)
    suffix = json.dumps(packet, ensure_ascii=False, sort_keys=True)
    _require(original.endswith(suffix), 'transport_original_json_suffix_mismatch')
    prompt = original[:-len(suffix)] + TRANSPORT_INSTRUCTION + _json_bytes(frame, MAX_FRAME_BYTES).decode('utf-8')
    _require(len(_text_bytes(prompt)) <= MAX_WIRE_BYTES
             and len(prompt) <= MAX_WIRE_CODEPOINTS, 'transport_wire_prompt_limit')
    return original, prompt


def build_prompt(stage, packet):
    """Keep original instructions; send a compact explicitly documented frame."""
    frame = build_frame(packet)
    return _prompt(stage, packet, frame)[1]


def _receipt(stage, packet, frame):
    original, prompt = _prompt(stage, packet, frame)
    result = {'contract': RECEIPT_CONTRACT, 'transport_contract': CONTRACT, 'stage': stage,
              'logical_packet_sha256': _digest(packet), 'logical_input_sha256': _text_sha(original),
              'input_sha256': _text_sha(prompt), 'frame_sha256': _digest(frame),
              'logical_input_bytes': len(_text_bytes(original)), 'logical_input_codepoints': len(original),
              'input_bytes': len(_text_bytes(prompt)), 'input_codepoints': len(prompt),
              'round_trip_exact': True, 'sources': len(packet['sources']),
              'passages': sum(len(source['passages']) for source in packet['sources']),
              'segments': len(packet['segments']), 'assertions': len(packet.get('assertions', [])),
              'shared_contexts': len(frame['context_table']), 'shared_texts': len(frame['text_table']),
              'limits': {'logical_bytes': MAX_LOGICAL_BYTES, 'wire_bytes': MAX_WIRE_BYTES,
                         'wire_codepoints': MAX_WIRE_CODEPOINTS},
              'provider_capacity_attested': False}
    return result


def receipt(stage, packet):
    """Pure receipt; logical and actually transmitted input have separate hashes."""
    return _receipt(stage, packet, build_frame(packet))


def _directory(directory):
    path = Path(directory)
    _require(path.is_absolute() and '..' not in path.parts, 'transport_absolute_directory_required')
    for parent in [*reversed(path.parents), path]:
        _require(not parent.is_symlink() and not getattr(parent, 'is_junction', lambda: False)(),
                 'transport_link_refused')
    _require(path.resolve(strict=False) == path.absolute(), 'transport_directory_resolution')
    path.mkdir(parents=True, exist_ok=True)
    _require(path.is_dir(), 'transport_directory_required')
    return path


def _write_once(path, value):
    raw = _json_bytes(value, MAX_FRAME_BYTES)
    _require(not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)(),
             'transport_link_refused')
    if path.exists():
        _require(path.is_file() and path.stat().st_size == len(raw), 'transport_saved_file_changed')
        with path.open('rb') as stream:
            _require(stream.read(len(raw) + 1) == raw, 'transport_saved_file_changed')
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.dv-transport-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Parent owns the execution lock. Refuse an existing receipt before replacement.
        _require(not path.exists() and not path.is_symlink(), 'transport_saved_file_changed')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_receipt(stage, packet, directory):
    """Write attributed immutable transport files under the caller's locked directory."""
    frame = build_frame(packet)
    result = _receipt(stage, packet, frame)
    path = _directory(directory)
    _write_once(path / 'transport-frame.json', frame)
    _write_once(path / 'transport-receipt.json', result)
    return result

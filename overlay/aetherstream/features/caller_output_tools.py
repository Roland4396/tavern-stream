"""Reuse caller-owned text tools on explicitly selected output-envelope routes.

The extraction contract is checked against the installed st-chatu8
extractToolCallArguments function: configured field order, two-newline joins,
optional tags, then its text fallbacks. No tool implementation is executed.
Schema descriptions and message bodies are never copied into decoder state.
"""
from __future__ import annotations

import copy
import json
import re


KIND = 'caller_output_tools'
DEFAULT_FIELDS = [
    {'name': 'thought_and_context', 'wrapTag': ''},
    {'name': 'thought', 'wrapTag': 'thinking'},
    {'name': 'content', 'wrapTag': ''},
]


def _truthy(value):
    # JavaScript object/array truthiness differs from Python.
    return value is not None and value is not False and value != '' and value != 0


def _string(value):
    if isinstance(value, str):
        return value
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, dict):
        return '[object Object]'
    if isinstance(value, list):
        return ','.join('' if item is None else _string(item) for item in value)
    return str(value)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def extract_arguments(raw, fields=None):
    """Match chatu8's nonstream extraction, including partial-JSON fallback."""
    if not isinstance(raw, str) or not raw.strip():
        return ''
    raw = raw.strip()
    fields = fields if isinstance(fields, list) and fields else DEFAULT_FIELDS
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        assembled = ''
        for field in fields:
            value = parsed.get(field['name'])
            if value is None:
                continue
            text = value if isinstance(value, str) else _json(value)
            tag = str(field.get('wrapTag') or '').strip()
            if tag and not (text.strip().startswith('<' + tag + '>') or
                            tag == 'thinking' and text.strip().startswith('<think')):
                assembled += f'<{tag}>{text}</{tag}>\n\n'
            else:
                assembled += ('\n\n' if assembled else '') + text
        if assembled:
            return assembled
        fallback = next((parsed[k] for k in ('content', 'reply', 'text', 'answer')
                         if _truthy(parsed.get(k))), '')
        thought = next((parsed[k] for k in ('thought', 'reasoning')
                        if _truthy(parsed.get(k))), '')
        if _truthy(thought) and _truthy(fallback):
            text = _string(thought).strip()
            if not text.startswith('<think'):
                text = '<thinking>' + text + '</thinking>'
            return text + '\n\n' + _string(fallback)
        if _truthy(fallback):
            return _string(fallback)
        if _truthy(thought):
            return _string(thought)
    assembled = ''
    for field in fields:
        prefix = '"' + re.escape(field['name']) + r'"\s*:\s*"'
        value_pattern = r'((?:[^"\\]|\\.)*)'
        match = re.search(prefix + value_pattern + '"', raw)
        if match is None:
            match = re.search(prefix + value_pattern + '$', raw)
        if match is None:
            continue
        try:
            text = json.loads('"' + match[1] + '"')
        except ValueError:
            text = match[1]
        tag = str(field.get('wrapTag') or '').strip()
        if tag:
            assembled += f'<{tag}>{text}</{tag}>\n\n'
        else:
            assembled += ('\n\n' if assembled else '') + text
    return assembled or (raw if not raw.startswith('{') else '')


def validate_descriptor(config):
    if (not isinstance(config, dict) or set(config) != {'kind', 'tools', 'required', 'selected_name'}
            or config.get('kind') != KIND or not isinstance(config['required'], bool)
            or not isinstance(config['selected_name'], str)
            or not isinstance(config['tools'], list) or not 1 <= len(config['tools']) <= 128):
        raise ValueError('Invalid caller-output descriptor')
    names = []
    for tool in config['tools']:
        if (not isinstance(tool, dict) or set(tool) != {'name', 'fields'}
                or not isinstance(tool['name'], str) or not 0 < len(tool['name']) <= 256
                or not isinstance(tool['fields'], list) or len(tool['fields']) > 128):
            raise ValueError('Invalid caller-output tool')
        names.append(tool['name'])
        for field in tool['fields']:
            if (not isinstance(field, dict) or set(field) != {'name', 'wrapTag'}
                    or not isinstance(field['name'], str) or not 0 < len(field['name']) <= 256
                    or not isinstance(field['wrapTag'], str) or len(field['wrapTag']) > 64):
                raise ValueError('Invalid caller-output field')
    if len(names) != len(set(names)) or config['selected_name'] and config['selected_name'] not in names:
        raise ValueError('Invalid caller-output tool selection')
    if len(_json(config).encode()) > 65536:
        raise ValueError('Caller-output descriptor too large')
    return config


def prepare_existing_tools(payload, *, require, log, trace_prefix=''):
    """The caller's schemas/messages survive byte-for-byte; only AUTO changes."""
    tools = payload.get('tools')
    if not isinstance(tools, list) or not tools:
        return None
    choice = payload.get('tool_choice')
    if choice == 'none':
        log(f'{trace_prefix}output_tools reused=disabled_by_caller count={len(tools)}')
        return None
    if choice not in (None, 'auto', 'required') and not isinstance(choice, dict):
        return None  # Normal request validation owns malformed choices.
    selected = choice.get('function', {}).get('name', '') if isinstance(choice, dict) else ''
    rules = []
    for tool in tools:
        function = tool.get('function') if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get('name'), str):
            return None
        schema = function.get('parameters', {})
        properties = schema.get('properties', {}) if isinstance(schema, dict) else {}
        if not isinstance(properties, dict):
            return None
        # The plugin builds these properties in the configured field order.
        # wrapTag is not a standard wire-schema field. Do not invent tags.
        fields = [{'name': name, 'wrapTag': ''} for name, value in properties.items()
                  if isinstance(name, str) and isinstance(value, dict) and value.get('type') == 'string']
        rules.append({'name': function['name'], 'fields': fields})
    forced = require and choice in (None, 'auto')
    active = validate_descriptor({'kind': KIND, 'tools': rules,
                                 'required': forced or choice == 'required' or bool(selected),
                                 'selected_name': selected})
    if forced:
        payload['tool_choice'] = 'required'
    log(f'{trace_prefix}output_tools reused=true injected=false count={len(tools)} '
        f'choice={"named" if selected else payload.get("tool_choice", "auto")} '
        f'text_fields={sum(len(rule["fields"]) for rule in rules)}')
    return active


def _rule(name, config, *, sole_call=False):
    if not isinstance(name, str):
        return None
    rules = config['tools']
    match = next((rule for rule in rules if rule['name'] == name), None)
    if match is not None:
        return match
    bare = name.rsplit(':', 1)[-1]
    matches = [rule for rule in rules if rule['name'] == bare]
    if len(matches) == 1:
        return matches[0]
    # Same single-output fallback as chatu8; never merge separate tools.
    if sole_call and len(rules) == 1:
        return rules[0]
    return None


def unwrap_completion(data, config):
    if not isinstance(data, dict) or not isinstance(data.get('choices'), list):
        return data
    result = copy.deepcopy(data)
    for choice in result['choices']:
        message = choice.get('message', {})
        if not isinstance(message, dict):
            continue
        calls = message.get('tool_calls') or []
        if not isinstance(calls, list):
            continue
        texts, kept = [], []
        for call in calls:
            function = call.get('function', {}) if isinstance(call, dict) else {}
            rule = _rule(function.get('name'), config, sole_call=len(calls) == 1)
            if rule is None or not rule['fields']:
                kept.append(call)
                continue
            text = extract_arguments(function.get('arguments'), rule['fields'])
            if text:
                texts.append(text)
            else:
                kept.append(call)  # No text must not silently delete a call.
        if texts:
            message['content'] = '\n\n'.join(texts)
            # chatu8 returns the matched tool text rather than prepending the
            # provider's separate reasoning/content fallback a second time.
            message.pop('reasoning_content', None)
            if kept:
                message['tool_calls'] = kept
            else:
                message.pop('tool_calls', None)
                if choice.get('finish_reason') in ('tool_calls', 'function_call'):
                    choice['finish_reason'] = 'stop'
    return result


class CallerOutputStreamDecoder:
    """Accumulate each indexed call separately, then use the same JSON decoder.

    Field ordering is deterministic even when a provider reorders its JSON or
    interleaves different calls. Control/usage/heartbeat events still pass
    through; there is no timer, retry or detached work.
    """
    def __init__(self, config):
        self.config = config
        self.states = {}

    def feed(self, data):
        result = copy.deepcopy(data)
        for choice in result.get('choices', []):
            delta = choice.get('delta')
            if delta is None and choice.get('finish_reason') is not None:
                delta = choice['delta'] = {}
            if not isinstance(delta, dict):
                continue
            state = self.states.setdefault(choice.get('index', 0),
                                           {'calls': {}, 'content': [], 'reasoning_content': []})
            for key in ('content', 'reasoning_content'):
                text = delta.pop(key, None)
                if isinstance(text, str):
                    state[key].append(text)
            for call in delta.pop('tool_calls', []) or []:
                index = call.get('index', 0)
                item = state['calls'].setdefault(index, {'index': index, 'type': 'function',
                                                         'function': {'name': '', 'arguments': ''}})
                if call.get('id'):
                    item['id'] = call['id']
                function = call.get('function', {})
                name = function.get('name') or ''
                current = item['function']['name']
                full_names = {rule['name'] for rule in self.config['tools']}
                if name and not (name == current and current in full_names):
                    item['function']['name'] += name
                item['function']['arguments'] += function.get('arguments') or ''
            if choice.get('finish_reason') is not None:
                message = {'role': 'assistant', 'tool_calls': list(state['calls'].values()),
                           'content': ''.join(state['content']),
                           'reasoning_content': ''.join(state['reasoning_content'])}
                decoded = unwrap_completion({'choices': [{'message': message,
                    'finish_reason': choice['finish_reason']}]}, self.config)['choices'][0]
                converted = decoded['message']
                for key in ('content', 'reasoning_content'):
                    if converted.get(key):
                        delta[key] = converted[key]
                if converted.get('tool_calls'):
                    delta['tool_calls'] = converted['tool_calls']
                choice['finish_reason'] = decoded['finish_reason']
                state['calls'].clear()
                state['content'].clear()
                state['reasoning_content'].clear()
        return result

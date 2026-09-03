#!/usr/bin/env python3
"""Sync the upstream provider's model catalog into CLIProxyAPI and Open WebUI.

Every cycle:
1. Fetch the upstream model list (OpenAI-compatible relay).
2. Enrich each non-GPT model with metadata from models.dev (display name,
   reasoning effort levels, input modalities, context limit).
3. Push the `openai-compatibility` section to CLIProxyAPI via its Management
   API (only when something actually changed).
4. Seed NEW models into Open WebUI as hidden workspace models with per-model
   reasoning levels (create-only: existing rows and access grants are never
   modified).

GPT-family models (gpt-*/codex-*) are skipped: they are served by the
codex-api-key route with its native reasoning handling.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

GPT_FAMILY_RE = re.compile(r'^(gpt-|codex-)')
EXCLUDED_LEVELS = {'none'}
REQUEST_TIMEOUT = int(os.getenv('SYNC_HTTP_TIMEOUT', '30'))


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


def env(name, default=None):
    value = os.getenv(name)
    if value is None or value == '':
        return default
    return value


def http_json(url, method='GET', body=None, headers=None):
    req = urllib.request.Request(url, method=method)
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'model-syncer/1.0')
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def fetch_upstream_models(base_url, api_key):
    data = http_json(
        base_url.rstrip('/') + '/models',
        headers={'Authorization': f'Bearer {api_key}'},
    )
    models = data.get('data', []) if isinstance(data, dict) else []
    return [m['id'] for m in models if isinstance(m, dict) and m.get('id')]


def fetch_models_dev():
    try:
        return http_json('https://models.dev/api.json')
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log(f'WARNING: models.dev unavailable, proceeding without metadata: {exc}')
        return {}


def find_catalog_entry(catalog, model_id):
    """Exact id match first, then the shortest id that extends ours (e.g. -preview)."""
    best = None
    for provider in catalog.values():
        for mid, entry in (provider.get('models') or {}).items():
            if not isinstance(entry, dict):
                continue
            if mid == model_id:
                return entry
            if mid.startswith(model_id) and (best is None or len(mid) < len(best[0])):
                best = (mid, entry)
    return best[1] if best else None


def extract_levels(entry):
    """Effort values from models.dev reasoning_options, minus excluded levels."""
    if not entry or not entry.get('reasoning'):
        return []
    levels = []
    for option in entry.get('reasoning_options') or []:
        if option.get('type') != 'effort':
            continue
        for value in option.get('values') or []:
            if value not in EXCLUDED_LEVELS and value not in levels:
                levels.append(value)
    return levels


def build_model_entries(upstream_ids, catalog):
    entries = []
    for model_id in sorted(upstream_ids):
        if GPT_FAMILY_RE.match(model_id):
            continue
        meta = find_catalog_entry(catalog, model_id) or {}
        levels = extract_levels(meta)
        input_modalities = [
            modality
            for modality in ((meta.get('modalities') or {}).get('input') or [])
            if isinstance(modality, str)
        ]
        if 'text' not in input_modalities:
            input_modalities.insert(0, 'text')

        model_entry = {'name': model_id, 'alias': model_id}
        display_name = meta.get('name')
        if display_name and display_name != model_id:
            model_entry['display-name'] = display_name
        if input_modalities:
            model_entry['input-modalities'] = input_modalities
        context_limit = (meta.get('limit') or {}).get('context')
        if isinstance(context_limit, int) and context_limit > 0:
            model_entry['max-context-length'] = context_limit
        if levels:
            model_entry['thinking'] = {'levels': levels}
        entries.append(
            {
                'model': model_entry,
                'levels': levels,
                'display_name': display_name or model_id,
            }
        )
    return entries


def desired_compat_section(cfg, entries):
    return {
        'openai-compatibility': [
            {
                'name': cfg['provider_name'],
                'base-url': cfg['upstream_base'].rstrip('/'),
                'api-key-entries': [{'api-key': cfg['upstream_key']}],
                # Single credential: a cooldown would block ALL models for
                # minutes after one upstream flap.
                'disable-cooling': True,
                'models': [item['model'] for item in entries],
            }
        ]
    }


def section_signature(section, provider_name):
    """Comparable subset of an openai-compatibility section (ignores extra keys)."""
    for entry in section.get('openai-compatibility') or []:
        if entry.get('name') != provider_name:
            continue
        models = tuple(
            (
                model.get('alias'),
                model.get('display-name'),
                tuple((model.get('thinking') or {}).get('levels') or []),
            )
            for model in entry.get('models') or []
        )
        return (
            entry.get('base-url'),
            bool(entry.get('disable-cooling')),
            models,
        )
    return None


def sync_proxy(cfg, section):
    current = http_json(
        cfg['proxy_base'].rstrip('/') + '/v0/management/openai-compatibility',
        headers={'Authorization': f'Bearer {cfg["management_key"]}'},
    )
    if section_signature(section, cfg['provider_name']) == section_signature(
        current or {}, cfg['provider_name']
    ):
        log('proxy: openai-compatibility already up to date')
        save_cached_section(cfg, section)
        return False

    # The Management API accepts both the wrapped object and a bare list.
    last_error = None
    for body in (section, section['openai-compatibility']):
        try:
            http_json(
                cfg['proxy_base'].rstrip('/') + '/v0/management/openai-compatibility',
                method='PUT',
                body=body,
                headers={'Authorization': f'Bearer {cfg["management_key"]}'},
            )
        except urllib.error.HTTPError as exc:
            last_error = exc
            continue
        models = section['openai-compatibility'][0]['models']
        log(f'proxy: pushed {len(models)} models')
        save_cached_section(cfg, section)
        return True
    raise RuntimeError(f'proxy update failed: {last_error}')


def cached_section_path(cfg):
    return os.path.join(cfg['state_path'].rsplit('/', 1)[0], 'last_section.json')


def save_cached_section(cfg, section):
    try:
        with open(cached_section_path(cfg), 'w') as f:
            json.dump(section, f)
    except OSError as exc:
        log(f'WARNING: cannot cache section: {exc}')


def load_cached_section(cfg):
    try:
        with open(cached_section_path(cfg)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def quick_restore_check(cfg):
    """Re-push the cached section if the proxy lost it (e.g. after a restart)."""
    section = load_cached_section(cfg)
    if not section:
        return
    current = http_json(
        cfg['proxy_base'].rstrip('/') + '/v0/management/openai-compatibility',
        headers={'Authorization': f'Bearer {cfg["management_key"]}'},
    )
    if section_signature(section, cfg['provider_name']) != section_signature(
        current or {}, cfg['provider_name']
    ):
        log('proxy: compat section lost, restoring from cache')
        sync_proxy(cfg, section)


def load_state(path):
    try:
        with open(path) as f:
            return set(json.load(f))
    except (OSError, json.JSONDecodeError):
        return set()


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(sorted(state), f)


def sync_openwebui(cfg, entries):
    if not cfg['owui_key']:
        log('open-webui: OPENWEBUI_API_KEY not set, skipping seeding')
        return
    headers = {'Authorization': f'Bearer {cfg["owui_key"]}'}
    state = load_state(cfg['state_path'])

    created, failed = 0, 0
    for item in entries:
        model_id = item['model']['alias']
        if model_id in state:
            continue
        # No 'hidden' flag: the model lands in the "private" state — visible to
        # the key owner only, never to regular users until they are granted.
        meta = {}
        if item['levels']:
            meta['reasoning_effort_settings'] = {'available': item['levels']}
        try:
            http_json(
                cfg['owui_base'].rstrip('/') + '/api/v1/models/create',
                method='POST',
                body={
                    'id': model_id,
                    'name': item['display_name'],
                    'base_model_id': None,
                    'meta': meta,
                    'params': {},
                    'access_grants': [],
                    'is_active': True,
                },
                headers=headers,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors='replace').lower()
            # Duplicate id: the API answers 401 with MODEL_ID_TAKEN ("already
            # registered") — the row exists, keep it untouched.
            if 'already registered' in body or 'already exist' in body:
                state.add(model_id)
                continue
            failed += 1
            log(f'open-webui: failed to seed "{model_id}": HTTP {exc.code}')
            continue
        except (urllib.error.URLError, OSError) as exc:
            failed += 1
            log(f'open-webui: failed to seed "{model_id}": {exc}')
            continue
        state.add(model_id)
        created += 1
        log(
            f'open-webui: seeded "{model_id}" ({item["display_name"]}) private, '
            f'levels={item["levels"] or "none"}'
        )

    save_state(cfg['state_path'], state)
    if created == 0 and failed == 0:
        log('open-webui: nothing new to seed')
    else:
        log(f'open-webui: seeded {created}, skipped existing, failed {failed}')
    return created


def run_cycle(cfg):
    upstream_ids = fetch_upstream_models(cfg['upstream_base'], cfg['upstream_key'])
    log(f'upstream: {len(upstream_ids)} models')

    catalog = fetch_models_dev()
    entries = build_model_entries(upstream_ids, catalog)
    log(f'candidates for openai-compatibility: {len(entries)} (gpt-family excluded)')

    proxy_changed = sync_proxy(cfg, desired_compat_section(cfg, entries))
    seeded = sync_openwebui(cfg, entries)

    if (proxy_changed or seeded) and cfg['owui_key']:
        # Refresh Open WebUI's base-model cache so users see new models
        # without a manual admin refresh.
        try:
            http_json(
                cfg['owui_base'].rstrip('/') + '/api/models?refresh=true',
                headers={'Authorization': f'Bearer {cfg["owui_key"]}'},
            )
            log('open-webui: model cache refreshed')
        except (urllib.error.URLError, OSError) as exc:
            log(f'WARNING: cache refresh failed: {exc}')


def load_config():
    cfg = {
        'proxy_base': env('CLIPROXYAPI_BASE_URL', 'http://cliproxyapi:8317'),
        'client_key': env('CLIPROXYAPI_API_KEY'),
        'management_key': env('CLIPROXYAPI_MANAGEMENT_KEY'),
        'upstream_base': env('CLIPROXYAPI_UPSTREAM_BASE_URL'),
        'upstream_key': env('CLIPROXYAPI_UPSTREAM_API_KEY'),
        'provider_name': env('SYNC_PROVIDER_NAME', 'cheapvibecode'),
        'owui_base': env('OPENWEBUI_BASE_URL', 'http://open-webui:8080'),
        'owui_key': env('OPENWEBUI_API_KEY'),
        'state_path': env('SYNC_STATE_PATH', '/app/data/seeded.json'),
        'interval': int(env('SYNC_INTERVAL_SECONDS', '900')),
        'check_interval': int(env('SYNC_CHECK_INTERVAL_SECONDS', '60')),
        'once': env('SYNC_ONCE', '0') == '1',
    }
    missing = [
        name
        for name in ('client_key', 'management_key', 'upstream_base', 'upstream_key')
        if not cfg[name]
    ]
    if missing:
        sys.exit(f'Missing required env vars: {", ".join(missing)}')
    return cfg


def main():
    cfg = load_config()
    last_full = 0.0
    while True:
        try:
            if time.time() - last_full >= cfg['interval']:
                run_cycle(cfg)
                last_full = time.time()
            else:
                # Fast recovery loop: re-push the cached section if the proxy
                # restarted and its generated config lost the compat section.
                quick_restore_check(cfg)
        except Exception as exc:  # keep the loop alive; the next cycle retries
            log(f'ERROR: {exc}')
        if cfg['once']:
            break
        time.sleep(cfg['check_interval'])


if __name__ == '__main__':
    main()

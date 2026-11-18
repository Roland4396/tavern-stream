"""CLI transport under the existing keeper lease, never a second scheduler."""
import math
import re
import time
from pathlib import Path

import httpx

from .client import GproxyClient, ProbeError
from .policy import account_groups


class ControlBusy(ProbeError):
    """A definite pre-dispatch deferral, not a failed quota or generation."""


def settings(value):
    if value is None:
        return {'enabled': False, 'refresh_interval_sec': 300}
    if not isinstance(value, dict) or set(value) - {'enabled', 'refresh_interval_sec'}:
        raise ValueError('Invalid CLI quota settings')
    enabled = value.get('enabled', False)
    interval = value.get('refresh_interval_sec', 300)
    if not isinstance(enabled, bool) or type(interval) not in (int, float) or not math.isfinite(interval):
        raise ValueError('Invalid CLI quota settings')
    return {'enabled': enabled, 'refresh_interval_sec': min(3600, max(60, interval))}


def cli_members(members):
    return [m for m in members if m.get('transport') == 'cli']


def family_members(members):
    # Prefer one transport for the same physical identity, before dispatch.
    # A failed CLI call never triggers a second request through GProxy.
    return cli_members(members) or members


class UnifiedClient(GproxyClient):
    def __init__(self, base_url, credentials_file, *, cli_settings, cli_base_url, cli_token_file):
        super().__init__(base_url, credentials_file)
        self.cli_settings = cli_settings
        self.cli_base = cli_base_url.rstrip('/') + '/internal/quota-keeper/'
        self.cli_token_file = Path(cli_token_file)
        self.cli_session = None
        self.cli_accounts = {}
        self.quota_cache = {}

    async def __aenter__(self):
        await super().__aenter__()
        try:
            if self.cli_settings['enabled']:
                url = httpx.URL(self.cli_base)
                if url.scheme != 'http' or url.host not in ('agy-hybrid-gateway', '127.0.0.1'):
                    raise ValueError('CLI control requires the private gateway')
                path = self.cli_token_file
                if path.is_symlink() or path.stat().st_mode & 0o077:
                    raise ValueError('CLI control token must be private')
                token = path.read_text().strip()
                if len(token) < 24:
                    raise ValueError('Invalid CLI control token')
                self.cli_session = httpx.AsyncClient(trust_env=False, follow_redirects=False,
                    headers={'Authorization': 'Bearer ' + token},
                    timeout=httpx.Timeout(55, connect=5))
            return self
        except BaseException:
            await super().__aexit__()
            raise

    async def __aexit__(self, *args):
        try:
            if self.cli_session:
                await self.cli_session.aclose()
        finally:
            await super().__aexit__(*args)

    async def cli_api(self, path, data=None):
        if not self.cli_session:
            raise ProbeError('CLI control is not configured')
        kwargs = {}
        if path == 'generate':
            # Paid work has no read deadline; shutdown keeps the keeper lease
            # until this one attempt finishes. No HTTP retry is configured.
            kwargs['timeout'] = httpx.Timeout(None, connect=5)
        response = await self.cli_session.request('GET' if data is None else 'POST',
            self.cli_base + path, json=data, **kwargs)
        if response.status_code == 409:
            raise ControlBusy('CLI foreground work is active')
        response.raise_for_status()
        return response.json()

    async def groups(self):
        providers = await self.api('providers')
        provider_ids = {p['id'] for p in providers if p['channel'] == 'antigravity' and p.get('enabled', True)}
        credentials = await self.api('credentials')
        if self.cli_settings['enabled']:
            payload = await self.cli_api('accounts')
            items = payload.get('accounts')
            if not isinstance(items, list):
                raise ProbeError('Invalid CLI account inventory')
            for item in items:
                alias, email = item.get('account'), item.get('email')
                if (not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', alias)
                        or not isinstance(email, str) or not re.fullmatch(r'[\w.+-]+@[\w.-]+', email)):
                    raise ProbeError('Unverified CLI account identity')
                cid = 'cli:' + alias
                if cid in self.cli_accounts:
                    raise ProbeError('Duplicate CLI account identity')
                self.cli_accounts[cid] = alias
                credentials.append({'id': cid, 'provider_id': 'agy-cli', 'enabled': True,
                    'label': email.lower(), 'transport': 'cli', 'dispatch_status': item.get('status')})
            provider_ids.add('agy-cli')
        return account_groups(credentials, provider_ids)

    async def refresh_cli(self, cid):
        if cid in self.quota_cache:
            return self.quota_cache[cid]
        started = time.time()
        result = await self.cli_api('quota', {'account': self.cli_accounts[cid]})
        if result.get('status') == 'busy':
            raise ControlBusy('CLI metadata yielded to foreground work')
        if result.get('ok') is not True or result.get('worker_stopped') is not True or result.get('generation_requests') != 0:
            raise ProbeError('CLI quota metadata unavailable')
        data = result.get('diagnostic') or {}
        observed = data.get('observed_at_ms')
        if (type(observed) not in (int, float) or not math.isfinite(observed)
                or observed < started * 1000 - 2000 or observed > time.time() * 1000 + 2000
                or not isinstance(data.get('models'), list) or not all(isinstance(m, str) for m in data['models'])
                or not isinstance(data.get('buckets'), list) or not all(isinstance(b, dict) for b in data['buckets'])):
            raise ProbeError('Invalid CLI quota diagnostic')
        self.quota_cache[cid] = (observed, set(data['models']), data['buckets'])
        return self.quota_cache[cid]

    async def diagnostic(self, credential_id):
        if credential_id in self.cli_accounts:
            return await self.refresh_cli(credential_id)
        return await super().diagnostic(credential_id)

    async def generate_argon(self, member, model):
        if member['id'] in self.cli_accounts:
            return await self.cli_api('generate', {'account': self.cli_accounts[member['id']], 'model': model})
        return await super().generate_argon(member, model)

    async def generate_hydrogen(self, member, model):
        if member['id'] in self.cli_accounts:
            return await self.cli_api('generate', {'account': self.cli_accounts[member['id']], 'model': model})
        return await super().generate_hydrogen(member, model)


async def refresh_observations(api, members, row, persist, *, now, interval):
    """Part of the one sweep; metadata is separate from paid-cycle pause state."""
    selected = cli_members(members)
    if not selected:
        return True
    state = row.setdefault('cli_quota', {})
    if now < state.get('next_at', 0):
        return state.get('status') == 'ok'
    member = min(selected, key=lambda m: m['id'])
    state.update(last_attempt_at=now, credential_id=member['id'])
    try:
        observed, _, buckets = await api.refresh_cli(member['id'])
        from .client import bucket_reading
        windows = {b['bucketId']: bucket_reading(b) for b in buckets}
        state.update(status='ok', last_success_at=now, observed_at_ms=observed,
                     windows=windows, next_at=now + interval, error=None)
    except ControlBusy:
        state.update(status='busy', next_at=now + 15, error=None)
    except (httpx.HTTPError, ProbeError, ValueError, KeyError) as exc:
        state.update(status='error', next_at=now + interval, error=type(exc).__name__)
    persist()
    return state.get('status') == 'ok'

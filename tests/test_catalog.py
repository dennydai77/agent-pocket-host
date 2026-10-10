import asyncio
from unittest.mock import AsyncMock

import pytest

from pocket.catalog import refresh_pi_models


@pytest.mark.asyncio
async def test_refresh_updates_only_catalog_and_preserves_on_failure(monkeypatch):
    config = {'pi': 'pi', 'models': ['old/model'], 'default_model': 'old/model'}
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b'ollama new-model 4096 1024 no no\nollama new-model 4096 1024 no no\n', b'')
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    await refresh_pi_models(config)
    assert config['models'] == ['ollama/new-model']
    assert config['default_model'] == 'ollama/new-model'
    assert '--no-extensions' in spawn.call_args.args
    process.communicate.return_value = (b'', b'')
    with pytest.raises(ValueError):
        await refresh_pi_models(config)
    assert config['models'] == ['ollama/new-model']
    process.returncode = 1
    with pytest.raises(ValueError):
        await refresh_pi_models(config)


@pytest.mark.asyncio
async def test_catalog_endpoint_owner_only(aiohttp_client, tmp_path, monkeypatch):
    from pocket.server import make_app
    config = {'backend': 'pi', 'pi': 'pi', 'agent': 'pi', 'models': ['old'], 'default_model': 'old'}
    refresh = AsyncMock()
    monkeypatch.setattr('pocket.server.refresh_pi_models', refresh)
    app = make_app(tmp_path, config, runner=AsyncMock())
    client = await aiohttp_client(app)
    assert (await client.get('/settings?refresh_models=1')).status == 401
    store = app['store']
    member = store.pair(store.invite(scope='member')[0], 'member')
    assert (await client.get('/settings?refresh_models=1', headers={'Authorization': 'Bearer '+member['token']})).status == 403
    refresh.assert_not_awaited()
    owner = store.pair(store.invite()[0], 'owner')
    headers = {'Authorization': 'Bearer '+owner['token']}
    assert (await client.get('/settings', headers=headers)).status == 200
    refresh.assert_not_awaited()
    assert (await client.get('/settings?refresh_models=1', headers=headers)).status == 200
    refresh.assert_awaited_once()
    refresh.side_effect = ValueError('failed')
    assert (await client.get('/settings?refresh_models=1', headers=headers)).status == 503
    assert config['models'] == ['old']

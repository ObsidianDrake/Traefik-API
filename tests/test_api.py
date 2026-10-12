import asyncio
import errno
import ssl
from contextlib import asynccontextmanager
import httpx
import pytest
import yaml
from route_api.main import create_app
from route_api.settings import Settings
TOKEN = 'test-token-with-at-least-24-characters'
HEADERS = {'Authorization': 'Bearer ' + TOKEN}
BODY = {'pathPrefix': '/app', 'upstreams': ['http://old:3000'], 'stripPrefix': True}
pytestmark = pytest.mark.anyio


@pytest.mark.parametrize('hosts,cidrs,url,expected', [
    ('*', '', 'http://any-service:3000', 200),
    ('*', '', 'http://192.168.1.5:3000', 200),
    ('*', '', 'http://[2001:db8::1]:3000', 200),
    ('', '*', 'http://192.168.1.5:3000', 200),
    ('', '*', 'http://[2001:db8::1]:3000', 200),
    ('', '*', 'http://any-service:3000', 422),
    ('', '', 'http://any-service:3000', 422),
    ('', '', 'http://192.168.1.5:3000', 422),
    ('*', '*', 'file:///etc/passwd', 422),
    ('known,*', '10.0.0.0/8,*', 'http://any-service:3000', 200),
])
async def test_upstream_wildcards_from_env(tmp_path, monkeypatch, hosts, cidrs, url, expected):
    monkeypatch.setenv('API_TOKEN', TOKEN)
    monkeypatch.setenv('TRAEFIK_CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('ALLOWED_UPSTREAM_HOSTS', hosts)
    monkeypatch.setenv('ALLOWED_UPSTREAM_CIDRS', cidrs)
    async with client_for(Settings.from_env()) as client:
        response = await client.post('/v1/routes/preview', json={**BODY, 'upstreams': [url]})
        assert response.status_code == expected

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.fixture
def settings(tmp_path):
    return Settings(config_dir=tmp_path, token=TOKEN, allowed_hosts=['old', 'new'], probe_attempts=2, probe_interval=0, probe_timeout=0.1)

def refused(request):
    try:
        raise ConnectionRefusedError(errno.ECONNREFUSED, 'refused')
    except OSError as exc:
        raise httpx.ConnectError('connection failed', request=request) from exc

@asynccontextmanager
async def client_for(settings, handler=refused, traefik_handler=None):
    traefik_handler = traefik_handler or (lambda request: httpx.Response(200, json=[]))
    app = create_app(settings, transport=httpx.MockTransport(handler),
                     traefik_transport=httpx.MockTransport(traefik_handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=HEADERS) as client:
            yield client

async def test_crud_preview_restart_and_yaml(settings):
    async with client_for(settings) as client:
        preview = await client.post('/v1/routes/preview', json=BODY)
        assert preview.status_code == 200
        assert not list(settings.config_dir.glob('*.yml'))
        result = await client.put('/v1/routes/demo', json=BODY)
        assert result.status_code == 201
        assert result.json()['status'] == 'written'
        doc = yaml.safe_load((settings.config_dir / 'demo.yml').read_text())
        assert doc['http']['routers']['demo']['rule'] == '(Path(`/app`) || PathPrefix(`/app/`))'
        assert doc['http']['middlewares']['demo-strip']['stripPrefix']['prefixes'] == ['/app']
        # A file write alone does not mean Traefik has loaded the router.
        assert (await client.get('/v1/routes')).json()['routes'] == []
    async with client_for(settings) as client:
        assert (await client.get('/v1/routes/demo')).json()['route']['upstreams'] == BODY['upstreams']
        assert (await client.delete('/v1/routes/demo')).status_code == 204
        assert (await client.get('/v1/routes/demo')).status_code == 404
        assert (await client.delete('/v1/routes/demo')).status_code == 404

@pytest.mark.parametrize('status', [200, 301, 401, 404, 500, 503])
async def test_any_response_protects_even_identical_request(settings, status):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(status, headers={'Location': 'http://new:3000/'})
    async with client_for(settings, handler) as client:
        await client.put('/v1/routes/demo', json=BODY)
        before = (settings.config_dir / 'demo.yml').read_bytes()
        response = await client.put('/v1/routes/demo', json=BODY)
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'existing_service_responding'
        assert seen == ['http://old:3000/']
        assert (settings.config_dir / 'demo.yml').read_bytes() == before

@pytest.mark.parametrize('failure', ['refused', 'timeout'])
async def test_takeover_probes_original_and_keeps_canonical_name(settings, failure):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if failure == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        return refused(request)
    async with client_for(settings, handler) as client:
        await client.put('/v1/routes/demo', json={**BODY, 'probePath': '/ready'})
        response = await client.put('/v1/routes/new-id', json={**BODY, 'upstreams': ['http://new:3000']})
        assert response.status_code == 200
        assert response.json()['name'] == 'demo'
        assert seen == ['http://old:3000/ready'] * 2
        assert not (settings.config_dir / 'new-id.yml').exists()
        assert (await client.get('/v1/routes/demo')).json()['route']['upstreams'] == ['http://new:3000']

@pytest.mark.parametrize('kind', ['dns', 'tls', 'protocol'])
async def test_inconclusive_probe_preserves_file(settings, kind):

    def handler(request):
        if kind == 'tls':
            try:
                raise ssl.SSLCertVerificationError('bad certificate')
            except ssl.SSLError as exc:
                raise httpx.ConnectError('TLS failed', request=request) from exc
        if kind == 'protocol':
            raise httpx.RemoteProtocolError('invalid response', request=request)
        raise httpx.ConnectError('DNS failed', request=request)
    async with client_for(settings, handler) as client:
        await client.put('/v1/routes/demo', json=BODY)
        before = (settings.config_dir / 'demo.yml').read_bytes()
        assert (await client.put('/v1/routes/demo', json=BODY)).status_code == 503
        assert (settings.config_dir / 'demo.yml').read_bytes() == before

async def test_one_responding_upstream_prevents_takeover(settings):

    def handler(request):
        if request.url.host == 'new':
            return httpx.Response(500)
        return refused(request)
    async with client_for(settings, handler) as client:
        await client.put('/v1/routes/demo', json={**BODY, 'upstreams': ['http://old:3000', 'http://new:3000']})
        assert (await client.put('/v1/routes/demo', json=BODY)).status_code == 409

@pytest.mark.parametrize('patch', [{'host': 'a`) || Host(`evil'}, {'pathPrefix': '/a`'}, {'pathPrefix': '/../x'}, {'pathPrefix': None}, {'stripPrefix': 'true'}, {'extra': 'ignored?'}, {'upstreams': ['file:///etc/passwd']}, {'upstreams': ['http://user:pass@old']}, {'upstreams': ['http://old:99999']}, {'upstreams': ['http://old/a']}, {'upstreams': ['http://unknown:3000']}, {'probePath': '//new/'}])
async def test_bad_input_never_writes(settings, patch):
    async with client_for(settings) as client:
        assert (await client.put('/v1/routes/demo', json={**BODY, **patch})).status_code == 422
        assert not list(settings.config_dir.glob('*.yml'))

async def test_auth_size_limit_ids_and_openapi(settings):
    async with client_for(settings) as client:
        assert (await client.get('/healthz', headers={'Authorization': ''})).status_code == 200
        for (method, url) in [('get', '/v1/routes'), ('get', '/v1/routes/demo'), ('put', '/v1/routes/demo'), ('delete', '/v1/routes/demo'), ('post', '/v1/routes/preview')]:
            assert (await client.request(method, url, json=BODY, headers={'Authorization': 'Bearer wrong'})).status_code == 401
        assert (await client.put('/v1/routes/Bad_ID', json=BODY)).status_code == 422
        assert (await client.put('/v1/routes/demo', content=b'x' * 17000)).status_code == 413
        spec = (await client.get('/openapi.json')).json()
        assert 'security' in spec['paths']['/v1/routes/{name}']['put']

@pytest.mark.parametrize('suffix,content', [('yml', 'http:\n  routers:\n    manual:\n      rule: "PathPrefix(`/app`)"\n'), ('toml', '[http.routers.manual]\nrule = "PathPrefix(`/app`)"\n')])
async def test_manual_files_protected(settings, suffix, content):
    file = settings.config_dir / f'manual.{suffix}'
    file.write_text(content)
    async with client_for(settings) as client:
        response = await client.put('/v1/routes/demo', json=BODY)
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'manual_route_conflict'
        assert file.read_text() == content
        assert (await client.delete('/v1/routes/manual')).status_code == 404

async def test_unrelated_manual_rule_allowed_and_unmanaged_filename_protected(settings):
    (settings.config_dir / 'manual.yml').write_text('http:\n  routers:\n    other:\n      rule: "PathPrefix(`/other`)"\n')
    (settings.config_dir / 'demo.yml').write_text('http: {}\n')
    async with client_for(settings) as client:
        assert (await client.put('/v1/routes/demo', json=BODY)).status_code == 409
        assert (await client.put('/v1/routes/free', json=BODY)).status_code == 201

async def test_change_during_probe_prevents_write(settings):

    def handler(request):
        (settings.config_dir / 'manual.yml').write_text('http: {}\n')
        return refused(request)
    async with client_for(settings, handler) as client:
        await client.put('/v1/routes/demo', json=BODY)
        response = await client.put('/v1/routes/demo', json=BODY)
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'config_changed'

async def test_single_instance_lock(settings):
    async with client_for(settings):
        with pytest.raises(BlockingIOError):
            async with client_for(settings):
                pass

async def test_tampered_managed_yaml_fails_closed(settings):
    async with client_for(settings) as client:
        await client.put('/v1/routes/demo', json=BODY)
        file = settings.config_dir / 'demo.yml'
        file.write_text(file.read_text() + 'unexpected: true\n')
        assert (await client.get('/v1/routes')).status_code == 503
        assert (await client.delete('/v1/routes/demo')).status_code == 503

async def test_concurrent_takeovers_recheck_new_owner(settings):

    async def handler(request):
        await asyncio.sleep(0.01)
        if request.url.host == 'new':
            return httpx.Response(200)
        return refused(request)
    async with client_for(settings, handler) as client:
        await client.put('/v1/routes/demo', json=BODY)

        async def update():
            return await client.put('/v1/routes/demo', json={**BODY, 'upstreams': ['http://new:3000']})
        results = await asyncio.gather(update(), update())
        assert sorted((result.status_code for result in results)) == [200, 409]

async def test_failed_replace_preserves_original(settings, monkeypatch):
    async with client_for(settings) as client:
        await client.put('/v1/routes/demo', json=BODY)
        file = settings.config_dir / 'demo.yml'
        before = file.read_bytes()

        def fail(*args):
            raise OSError('disk failure')
        monkeypatch.setattr('route_api.store.os.replace', fail)
        assert (await client.put('/v1/routes/demo', json=BODY)).status_code == 503
        assert file.read_bytes() == before
        assert not list(settings.config_dir.glob('*.tmp'))

async def test_allowlist_cidr_and_host_normalization(settings):
    settings.allowed_cidrs = ['10.0.0.0/8']
    async with client_for(settings) as client:
        response = await client.put('/v1/routes/demo', json={'host': 'App.Example.COM.', 'upstreams': ['http://10.2.3.4:8000']})
        assert response.status_code == 201
        assert response.json()['route']['host'] == 'app.example.com'
        assert (await client.put('/v1/routes/other', json={'host': 'other.example.com', 'upstreams': ['http://192.168.1.1']})).status_code == 422


async def test_probe_total_deadline(settings):
    async def stalled(request):
        await asyncio.sleep(10)
        return httpx.Response(200)

    settings.probe_timeout = 0.01
    async with client_for(settings, stalled) as client:
        await client.put('/v1/routes/demo', json=BODY)
        response = await client.put('/v1/routes/demo', json=BODY)
        assert response.status_code == 200
        assert response.json()['probes'][0]['attempts'] == ['timeout', 'timeout']


async def test_response_body_is_not_consumed(settings):
    class EndlessBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('probe must not read the response body')
            yield b''

    async with client_for(settings, lambda request: httpx.Response(200, stream=EndlessBody())) as client:
        await client.put('/v1/routes/demo', json=BODY)
        assert (await client.put('/v1/routes/demo', json=BODY)).status_code == 409


async def test_pool_exhaustion_is_not_offline(settings):
    def exhausted(request):
        raise httpx.PoolTimeout('pool exhausted', request=request)

    async with client_for(settings, exhausted) as client:
        await client.put('/v1/routes/demo', json=BODY)
        assert (await client.put('/v1/routes/demo', json=BODY)).status_code == 503


async def test_streaming_body_limit_without_content_length(settings):
    async def body():
        for _ in range(20):
            yield b'x' * 1000

    async with client_for(settings) as client:
        response = await client.put('/v1/routes/demo', content=body())
        assert response.status_code == 413
        assert not list(settings.config_dir.glob('*.yml'))


async def test_host_only_and_root_share_identity(settings):
    async with client_for(settings, lambda request: httpx.Response(200)) as client:
        body = {'host': 'app.example.com', 'upstreams': ['http://old:3000']}
        assert (await client.put('/v1/routes/demo', json=body)).status_code == 201
        assert (await client.put('/v1/routes/other', json={**body, 'pathPrefix': '/'})).status_code == 409


async def test_same_name_with_new_entry_still_protects_original(settings):
    async with client_for(settings, lambda request: httpx.Response(200)) as client:
        await client.put('/v1/routes/demo', json=BODY)
        assert (await client.put('/v1/routes/demo', json={**BODY, 'pathPrefix': '/different'})).status_code == 409


async def test_symlink_cannot_be_deleted_or_overwritten(settings, tmp_path):
    async with client_for(settings) as client:
        target = tmp_path / 'preserved.txt'
        target.write_text('preserve this')
        (tmp_path / 'demo.yml').symlink_to(target)
        assert (await client.put('/v1/routes/demo', json=BODY)).status_code == 503
        assert (await client.delete('/v1/routes/demo')).status_code == 503
        assert target.read_text() == 'preserve this'


@pytest.mark.parametrize('content', ['http: []', 'http:\n  routers: []', 'http:\n  routers:\n    broken: true'])
async def test_malformed_external_config_fails_closed(settings, content):
    async with client_for(settings) as client:
        (settings.config_dir / 'manual.yml').write_text(content)
        assert (await client.put('/v1/routes/demo', json=BODY)).status_code == 503
        assert not (settings.config_dir / 'demo.yml').exists()

from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlsplit
import httpx
import pytest
from app.config import Settings
from app.auth.github import GitHubOAuth, AuthDenied

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)

def configured(**changes):
    return Settings(github_client_id='client', github_client_secret='secret-sentinel',
                    github_allowed_ids=(123,), auth_loopback_development=True, **changes)

def oauth(identity=None, settings=None):
    clock = [NOW]
    requests = []
    def handle(request):
        requests.append(request)
        if request.url == 'https://github.com/login/oauth/access_token':
            return httpx.Response(200, json={'access_token':'token-sentinel','token_type':'bearer','scope':''})
        assert request.url == 'https://api.github.com/user'
        return httpx.Response(200, json=identity or {'id':123,'type':'User','login':'private-login-sentinel','email':'private-email-sentinel'})
    service = GitHubOAuth(settings or configured(), clock=lambda:clock[0], transport=httpx.MockTransport(handle))
    return service, clock, requests

def finish(service, binding='browser'):
    auth = service.begin(binding)
    state = parse_qs(urlsplit(auth.url).query)['state'][0]
    return service.complete('code-sentinel', state, binding)

def test_oauth_state_browser_binding_pkce_single_use():
    service, _, requests = oauth()
    auth = service.begin('browser')
    query = parse_qs(urlsplit(auth.url).query, keep_blank_values=True)
    assert query['code_challenge_method'] == ['S256']
    assert query['redirect_uri'] == ['http://127.0.0.1:8765/auth/github/callback']
    assert query['scope'] == ['']
    assert query['code_challenge'][0]
    state = query['state'][0]
    assert state not in repr(auth)
    assert service.complete('code-sentinel', state, 'browser').account_id == 123
    with pytest.raises(AuthDenied): service.complete('code-sentinel', state, 'browser')
    body = parse_qs(requests[0].content.decode())
    assert body['code_verifier'] and body['redirect_uri'] == query['redirect_uri']
    import base64, hashlib
    assert query['code_challenge'] == [base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()]
    assert requests[1].headers['X-GitHub-Api-Version'] == '2026-03-10'
    bad = service.begin('a')
    with pytest.raises(AuthDenied): service.complete('code', parse_qs(urlsplit(bad.url).query)['state'][0], 'b')


def test_callback_expired_replayed_or_wrong_host_denied():
    service, clock, requests = oauth()
    auth = service.begin('browser')
    clock[0] += timedelta(seconds=300)
    with pytest.raises(AuthDenied): service.complete('code',parse_qs(urlsplit(auth.url).query)['state'][0],'browser')
    assert requests == []


def test_account_numeric_id_not_rename():
    service, _, _ = oauth({'id':123,'type':'User','login':'renamed'})
    assert finish(service).account_id == 123

@pytest.mark.parametrize('identity',[{'id':124,'type':'User','login':'same'}, {'id':123,'type':'Bot','login':'bot'}, {'id':123,'type':'Organization','login':'org'}, {'id':True,'type':'User','login':'bad'}])
def test_wrong_id_or_bot_denied(identity):
    service, _, _ = oauth(identity)
    with pytest.raises(AuthDenied): finish(service)


def test_no_first_login_auto_enrollment():
    service, _, requests = oauth(settings=Settings())
    with pytest.raises(AuthDenied): service.begin('browser')
    assert requests == []

@pytest.mark.parametrize('missing', ['github_client_id','github_client_secret','github_allowed_ids'])
def test_partial_setup_denies_login(missing):
    settings=configured().model_copy(update={missing:() if missing=='github_allowed_ids' else None})
    service,_,requests=oauth(settings=settings)
    with pytest.raises(AuthDenied): service.begin('browser')
    assert not requests


def test_auth_settings_never_enter_execution_hash_or_repr(monkeypatch):
    baseline=configured()
    changed=baseline.model_copy(update={'github_client_id':'different','github_client_secret':None,'github_allowed_ids':(456,)})
    assert baseline.config_hash() == changed.config_hash()
    assert 'secret-sentinel' not in repr(baseline)
    assert 'github_allowed_ids' not in baseline.model_dump()
    monkeypatch.setenv('FINANCE_GITHUB_ALLOWED_IDS','123, 456')
    monkeypatch.setenv('FINANCE_AUTH_LOOPBACK_DEVELOPMENT','true')
    assert Settings.from_env().github_allowed_ids == (123,456)
    assert Settings.from_env().auth_loopback_development is True

@pytest.mark.parametrize('which', ['token_redirect','user_redirect','token_error','bad_json','no_token'])
def test_provider_errors_and_redirects_are_generic_denials(which):
    requests=[]
    def handle(request):
        requests.append(request)
        if request.url.host == 'github.com':
            if which=='token_redirect': return httpx.Response(302,headers={'Location':'https://evil.test/'})
            if which=='token_error': return httpx.Response(400,json={'error':'secret-sentinel'})
            if which=='bad_json': return httpx.Response(200,text='secret-sentinel')
            if which=='no_token': return httpx.Response(200,json={})
            return httpx.Response(200,json={'access_token':'token-sentinel','token_type':'bearer'})
        return httpx.Response(302,headers={'Location':'https://evil.test/'})
    service=GitHubOAuth(configured(),clock=lambda:NOW,transport=httpx.MockTransport(handle))
    with pytest.raises(AuthDenied,match='authentication_denied') as error: finish(service)
    assert 'sentinel' not in str(error.value)
    assert all(request.url.host in {'github.com','api.github.com'} for request in requests)

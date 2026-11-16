from datetime import timedelta
import sqlite3
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from app.auth.github import GitHubIdentity, GitHubOAuth, AuthDenied
from app.auth.sessions import SessionService
from app.auth.dependencies import require_reviewer
from app.storage.db import migrate
from app.web import Services, create_app
from tests.unit.test_github_auth import configured, oauth, NOW

@pytest.fixture
def setup():
    conn = sqlite3.connect(':memory:',check_same_thread=False)
    migrate(conn)
    service, clock, _ = oauth()
    sessions = SessionService(conn, configured(), clock=lambda:clock[0])
    app = create_app(configured(), Services(oauth=service, sessions=sessions, clock=lambda:clock[0]))
    @app.get('/test-identity')
    async def identity(principal=Depends(require_reviewer)):
        return {'account_id':principal.account_id}
    yield conn, sessions, clock, TestClient(app, base_url='http://127.0.0.1:8765')
    conn.close()

def identity():
    return GitHubIdentity(account_id=123, account_type='User', display_login='private-login-sentinel')

def test_session_fixation_rotation_logout(setup):
    _, sessions, clock, client = setup
    first=sessions.create(identity())
    client.cookies.set('finance_session',first.value)
    url=client.get('/auth/login',follow_redirects=False).headers['location']
    from urllib.parse import parse_qs,urlsplit
    state=parse_qs(urlsplit(url).query)['state'][0]
    response=client.get('/auth/github/callback',params={'state':state,'code':'code-sentinel'},follow_redirects=False)
    assert response.status_code == 303
    with pytest.raises(AuthDenied): sessions.require(first.value,clock[0])
    assert client.get('/test-identity').json() == {'account_id':123}
    csrf=client.cookies['finance_csrf']
    assert client.post('/auth/logout',headers={'Origin':'http://127.0.0.1:8765','X-CSRF-Token':csrf}).status_code == 204
    assert client.get('/test-identity').status_code == 401

@pytest.mark.parametrize('seconds',[1800,28800])
def test_idle_1800_absolute_28800_expiry(setup,seconds):
    _, sessions, clock, _ = setup
    cookie=sessions.create(identity())
    if seconds==28800:
        for tick in range(900,28800,900): sessions.require(cookie.value,NOW+timedelta(seconds=tick))
    with pytest.raises(AuthDenied): sessions.require(cookie.value,NOW+timedelta(seconds=seconds))

def test_allowlist_change_invalidates_existing_session(setup):
    conn,sessions,clock,_=setup
    cookie=sessions.create(identity())
    changed=SessionService(conn,configured().model_copy(update={'github_allowed_ids':(456,)}),clock=lambda:clock[0])
    with pytest.raises(AuthDenied): changed.require(cookie.value,clock[0])

def test_forged_principal_header_and_csrf_denied(setup):
    _,sessions,_,client=setup
    assert client.get('/test-identity',headers={'X-Principal':'123'}).status_code == 401
    client.cookies.set('finance_session',sessions.create(identity()).value)
    assert client.post('/auth/logout',headers={'Origin':'http://127.0.0.1:8765'}).status_code == 403
    assert client.get('/auth/login',headers={'Host':'evil.test'}).status_code == 400
    assert client.get('/auth/login',headers={'X-Forwarded-Host':'evil.test'}).status_code == 400

def test_session_verifier_preserves_business_transaction(setup):
    conn,sessions,clock,_=setup
    principal=sessions.require(sessions.create(identity()).value,clock[0])
    before=conn.total_changes
    conn.execute('BEGIN IMMEDIATE')
    assert sessions.verify_principal(principal) is True
    assert conn.in_transaction and conn.total_changes == before
    conn.rollback()
    sessions.logout('unknown')
    assert sessions.verify_principal(principal) is True

def test_tokens_discarded_and_callback_not_traced(setup,caplog,tmp_path,monkeypatch):
    conn,_,_,client=setup
    import logging
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter=InMemorySpanExporter()
    provider=TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace,'_TRACER_PROVIDER',provider)
    caplog.set_level(logging.DEBUG,logger='app')
    caplog.set_level(logging.DEBUG,logger='httpx')
    from urllib.parse import parse_qs,urlsplit
    response=client.get('/auth/login',follow_redirects=False)
    state=parse_qs(urlsplit(response.headers['location']).query)['state'][0]
    response=client.get('/auth/github/callback',params={'state':state,'code':'code-sentinel'},follow_redirects=False)
    assert response.status_code == 303
    exported='\n'.join(conn.iterdump())
    snapshot=tmp_path/'auth-snapshot.sqlite'
    with sqlite3.connect(snapshot) as output:
        conn.backup(output)
    raw=snapshot.read_bytes()
    assert exporter.get_finished_spans() == (), [(span.name,dict(span.attributes)) for span in exporter.get_finished_spans()]
    for sentinel in ('code-sentinel','token-sentinel','secret-sentinel','private-login-sentinel','private-email-sentinel',state,client.cookies['finance_session'],client.cookies['finance_csrf']):
        assert sentinel not in exported
        assert sentinel.encode() not in raw
        assert sentinel not in caplog.text
    assert conn.execute('SELECT count(*) FROM release_approvals').fetchone()[0] == 0

@pytest.mark.parametrize('headers',[{'Host':'127.0.0.1:8766'},{'Forwarded':'host=evil.test'},{'X-Forwarded-Proto':'https'}])
def test_wrong_callback_host_rejected_before_exchange(setup,headers):
    conn,_,_,client=setup
    response=client.get('/auth/github/callback?code=code-sentinel&state=state-sentinel',headers=headers)
    assert response.status_code == 400
    assert conn.execute('SELECT count(*) FROM reviewer_sessions').fetchone()[0] == 0


def test_cookie_security_and_cross_origin_logout(setup):
    _,sessions,_,client=setup
    from urllib.parse import parse_qs,urlsplit
    response=client.get('/auth/login',follow_redirects=False)
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=lax' in response.headers['set-cookie']
    state=parse_qs(urlsplit(response.headers['location']).query)['state'][0]
    response=client.get('/auth/github/callback',params={'code':'code','state':state},follow_redirects=False)
    headers=response.headers.get_list('set-cookie')
    assert any('finance_session=' in h and 'HttpOnly' in h and 'SameSite=lax' in h for h in headers)
    assert response.headers['cache-control']=='no-store'
    assert response.headers['referrer-policy']=='no-referrer'
    token=client.cookies['finance_csrf']
    assert client.post('/auth/logout',headers={'Origin':'https://evil.test','X-CSRF-Token':token}).status_code==403
    assert client.post('/auth/logout',headers={'Origin':'http://127.0.0.1:8765','X-CSRF-Token':'forged'}).status_code==403
    assert client.get('/auth/session').status_code==200


def test_logged_out_principal_and_agent_forgery_denied(setup):
    _,sessions,clock,_=setup
    cookie=sessions.create(identity())
    principal=sessions.require(cookie.value,clock[0])
    assert not sessions.verify_principal(principal.model_copy(update={'account_id':456}))
    assert not sessions.verify_principal(principal.model_copy(update={'provider':'forged'}))
    assert not sessions.verify_principal(principal.model_copy(update={'kind':'agent'}))
    sessions.logout(cookie.value)
    assert not sessions.verify_principal(principal)


def test_missing_configuration_is_clear_default_deny(setup):
    conn,_,clock,_=setup
    from app.config import Settings
    settings=Settings(auth_loopback_development=True)
    github,_,_=oauth(settings=settings)
    app=create_app(settings,Services(oauth=github,sessions=SessionService(conn,settings),clock=lambda:clock[0]))
    client=TestClient(app,base_url='http://127.0.0.1:8765')
    assert client.get('/auth/status').json()=={'configured':False,'status':'authentication_setup_required'}
    assert client.get('/auth/login').status_code==503
    assert client.get('/auth/session').status_code==401
    with pytest.raises(ValueError): create_app(Settings(),Services(oauth=github,sessions=SessionService(conn,settings)))
    with pytest.raises(ValueError): Settings(auth_callback_url='http://evil.test/auth/github/callback')


def test_server_entrypoint_binds_loopback_without_access_or_proxy_logs(setup,monkeypatch):
    import uvicorn
    from app.web import serve
    _,_,_,client=setup
    calls=[]
    monkeypatch.setattr(uvicorn,'run',lambda app,**options:calls.append(options))
    serve(configured(),client.app.state.services)
    assert calls == [{'host':'127.0.0.1','port':8765,'proxy_headers':False,'access_log':False,'workers':1}]


def test_duplicate_origin_mutation_is_denied(setup):
    _,sessions,_,client=setup
    cookie=sessions.create(identity()).value
    client.cookies.set('finance_session',cookie)
    headers=[('Origin','http://127.0.0.1:8765'),('Origin','https://evil.test'),
             ('X-CSRF-Token',sessions.csrf_token(cookie))]
    assert client.post('/auth/logout',headers=headers).status_code==403


def test_noncanonical_callback_never_redirects_credentials(setup):
    conn,_,_,client=setup
    response=client.get('/auth/github/callback/?code=code-sentinel&state=state-sentinel',follow_redirects=False)
    assert response.status_code == 404
    assert 'location' not in response.headers
    assert not any('finance_session=' in header for header in response.headers.get_list('set-cookie'))
    assert conn.execute('SELECT count(*) FROM reviewer_sessions').fetchone()[0] == 0


def test_non_ascii_csrf_is_generic_denial(setup):
    _,sessions,_,client=setup
    cookie=sessions.create(identity()).value
    client.cookies.set('finance_session',cookie)
    response=client.post('/auth/logout',headers=[(b'origin',b'http://127.0.0.1:8765'),(b'x-csrf-token',b'\xe9')])
    assert response.status_code == 403
    assert response.json() == {'detail':'csrf_required'}


def test_non_ascii_browser_binding_is_generic_denial_without_exchange(setup):
    from urllib.parse import parse_qs,urlsplit
    _,_,_,client=setup
    requests=[]
    import httpx
    client.app.state.services.oauth.transport=httpx.MockTransport(lambda request:requests.append(request))
    response=client.get('/auth/login',follow_redirects=False)
    state=parse_qs(urlsplit(response.headers['location']).query)['state'][0]
    response=client.get('/auth/github/callback',params={'state':state,'code':'code-sentinel'},
                        headers=[(b'cookie',b'finance_oauth_binding=\xe9')],follow_redirects=False)
    assert response.status_code == 401
    assert response.json() == {'detail':'authentication_denied'}
    assert requests == []


def test_slow_provider_exchange_allows_logout_on_sqlite_owning_loop():
    import asyncio
    import threading
    import httpx
    from urllib.parse import parse_qs,urlsplit

    async def exercise():
        entered,release=threading.Event(),threading.Event()
        def handle(request):
            if request.url.host == 'github.com':
                entered.set()
                if not release.wait(2):
                    return httpx.Response(503)
                return httpx.Response(200,json={'access_token':'token-sentinel','token_type':'bearer'})
            return httpx.Response(200,json={'id':123,'type':'User','login':'reviewer'})
        # Keep SQLite's default owning-thread protection enabled.
        conn=sqlite3.connect(':memory:')
        migrate(conn)
        github=GitHubOAuth(
            configured(),clock=lambda:NOW,transport=httpx.MockTransport(handle))
        sessions=SessionService(conn,configured(),clock=lambda:NOW)
        app=create_app(configured(),Services(oauth=github,sessions=sessions,clock=lambda:NOW))
        callback=None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:8765') as client:
                cookie=sessions.create(identity()).value
                client.cookies.set('finance_session',cookie)
                login=await client.get('/auth/login')
                state=parse_qs(urlsplit(login.headers['location']).query)['state'][0]
                callback=asyncio.create_task(client.get('/auth/github/callback',params={'state':state,'code':'code'}))
                assert await asyncio.to_thread(entered.wait,1)
                assert not callback.done(), 'provider exchange blocked the serving event loop until timeout'
                assert (await asyncio.wait_for(client.get('/auth/session'),0.5)).status_code == 200
                csrf=sessions.csrf_token(cookie)
                response=await asyncio.wait_for(client.post('/auth/logout',headers={
                    'Origin':'http://127.0.0.1:8765','X-CSRF-Token':csrf}),0.5)
                assert response.status_code == 204
                assert (await client.get('/auth/session')).status_code == 401
                assert not release.is_set() and not callback.done()
                release.set()
                assert (await callback).status_code == 303
                assert conn.execute('SELECT count(*) FROM reviewer_sessions WHERE revoked=0').fetchone()[0] == 1
        finally:
            release.set()
            if callback is not None and not callback.done():
                await callback
            conn.close()
    asyncio.run(exercise())


def test_services_preserves_third_positional_clock():
    clock=lambda:NOW
    services=Services(None,None,clock)
    assert services.clock is clock and services.review is None

from pathlib import Path

def test_review_routes_exist():
    from app.review.routes import router
    assert '/review/{story_id}' in {r.path for r in router.routes}

import sqlite3
import re
import pytest
from fastapi.testclient import TestClient
from app.auth.sessions import SessionService
from app.auth.github import GitHubIdentity
from app.review.service import ReviewService
from app.web import Services,create_app
from app.workflow.state import StateService
from tests.integration.task4_fixtures import setup,awaiting,NOW
from tests.unit.test_github_auth import configured,oauth

@pytest.fixture
def browser(tmp_path):
    conn,store,engine,state=setup(tmp_path)
    conn.close()
    conn=sqlite3.connect(tmp_path/'state.db',check_same_thread=False)
    conn.execute('PRAGMA foreign_keys=ON');store.conn=conn
    state=StateService(conn,store,engine,clock=lambda:NOW)
    snapshot,manifest=awaiting(state,store)
    sessions=SessionService(conn,configured(),clock=lambda:NOW)
    cookie=sessions.create(GitHubIdentity(account_id=123,account_type='User',display_login='reviewer'))
    principal=sessions.require(cookie.value,NOW)
    service=ReviewService(state,current_session_verifier=sessions.verify_principal)
    github,_,_=oauth()
    app=create_app(configured(),Services(oauth=github,sessions=sessions,review=service,clock=lambda:NOW))
    client=TestClient(app,base_url='http://127.0.0.1:8765')
    client.cookies.set('finance_session',cookie.value)
    yield client,service,snapshot,manifest,principal,cookie
    conn.close()

def fields(response):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">',response.text))

def test_authenticated_escaped_preview_and_form_retry(browser):
    client,svc,s,_,_,_=browser
    page=client.get(f'/review/{s.story_id}')
    assert page.status_code==200 and 'Synthetic fact' in page.text
    assert 'Content review' in page.text and 'synthetic_foundation' in page.text
    assert "form-action 'self'" in page.headers['content-security-policy']
    payload=fields(page)|{'decision':'approve','notes':'<script>alert(1)</script>'}
    for _ in range(2):
        response=client.post(f'/review/{s.story_id}/decision',data=payload,headers={'Origin':'http://127.0.0.1:8765'})
        assert response.status_code==200
    assert svc.conn.execute('SELECT count(*) FROM human_decisions').fetchone()[0]==1
    assert svc.conn.execute("SELECT count(*) FROM state_events WHERE reason_code='human_approve'").fetchone()[0]==1

@pytest.mark.parametrize('mutation',['duplicate','actor','csrf','origin','get'])
def test_csrf_get_request_and_cross_origin_cannot_decide(browser,mutation):
    from urllib.parse import urlencode
    client,svc,s,_,_,_=browser
    path=f'/review/{s.story_id}/decision'
    payload=fields(client.get(f'/review/{s.story_id}'))|{'decision':'approve','notes':''}
    headers={'Origin':'http://127.0.0.1:8765','Content-Type':'application/x-www-form-urlencoded'}
    if mutation=='csrf':payload['csrf_token']='forged'
    if mutation=='actor':payload['reviewer_id']='123'
    if mutation=='origin':headers['Origin']='https://evil.test'
    body=urlencode(payload)
    if mutation=='duplicate':body+='&expected_version=8'
    response=client.get(path) if mutation=='get' else client.post(path,content=body,headers=headers)
    assert response.status_code in {400,403,405}
    assert svc.conn.execute('SELECT count(*) FROM human_decisions').fetchone()[0]==0


def test_anonymous_backend_and_media_require_session(browser):
    client,_,s,m,_,_=browser
    client.cookies.clear()
    assert client.get(f'/review/{s.story_id}').status_code==401
    assert client.get(f'/review/{s.story_id}/media/{m.render.sha256}').status_code==401


def test_media_hash_resolution_type_and_unsafe_content(browser):
    from app.contracts.workflow import ChangeKind
    client,svc,s,m,_,_=browser
    image=svc.state.store.put(b'\x89PNG\r\n\x1a\n','image/png','1.0')
    new=m.model_copy(update={'revision':2,'render':image})
    svc.state.revise(s.story_id,new,ChangeKind.VISUAL,s.version,'image')
    response=client.get(f'/review/{s.story_id}/media/{image.sha256}')
    assert response.status_code==200 and response.content==b'\x89PNG\r\n\x1a\n'
    assert response.headers['content-type']=='image/png'
    assert client.get(f'/review/{s.story_id}/media/{m.render.sha256}').status_code==404
    assert client.get(f'/review/{s.story_id}/media/..%2f..%2f.env').status_code==404


def test_source_text_links_inert():
    from app.review.routes import inert
    rendered=inert({'text':'<script>alert(1)</script>','url':'javascript:alert(1)','source':'https://example.com/?x="&y=2'})
    assert '<script>' not in rendered and '&lt;script&gt;' in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="https://example.com/?x=&quot;&amp;y=2"' in rendered


def test_no_publish_route_or_adapter(browser):
    client,svc,s,_,principal,_=browser
    svc.decide(s.story_id,'approve',s.manifest_hash,s.version,'approval',principal,'')
    assert client.post(f'/publish/{s.story_id}').status_code==404
    assert not any('publish' in getattr(route,'path','') for route in client.app.routes)


def test_readonly_navigation_cannot_write_or_supply_identity(browser,tmp_path):
    from app.ui.review import summaries
    result=summaries(tmp_path/'state.db')
    assert result['stories'][0]['review_url'].startswith('http://127.0.0.1:8765/review/')
    assert result['stories'][0]['synthetic'] is True
    assert 'principal' not in repr(result) and 'finance_session' not in repr(result)


def test_malformed_source_url_is_inert_text():
    from app.review.routes import inert
    assert '&lt;script&gt;' in inert({'url':'http://[','text':'<script>'})


def test_corrupt_preview_fails_closed(browser):
    client,svc,s,m,_,_=browser
    svc.state.store._path(m.dossier).write_bytes(b'corrupt')
    response=client.get(f'/review/{s.story_id}')
    assert response.status_code==409
    assert 'form' not in response.text


def test_readonly_navigation_does_not_create_database(tmp_path):
    from app.ui.review import summaries
    missing=tmp_path/'missing.sqlite'
    with pytest.raises(sqlite3.OperationalError): summaries(missing)
    assert not missing.exists()


def test_duplicate_csrf_header_is_denied(browser):
    client,svc,s,_,_,_=browser
    payload=fields(client.get(f'/review/{s.story_id}'))|{'decision':'approve','notes':''}
    headers=[('Origin','http://127.0.0.1:8765'),('X-CSRF-Token',payload['csrf_token']),('X-CSRF-Token','forged')]
    response=client.post(f'/review/{s.story_id}/decision',data=payload,headers=headers)
    assert response.status_code==403
    assert svc.conn.execute('SELECT count(*) FROM human_decisions').fetchone()[0]==0


@pytest.mark.parametrize('media_type,payload',[
    ('application/json',b'{"private-preview-sentinel":'),
    ('application/json',b'\xffprivate-preview-sentinel'),
    ('application/json',b'{"text":"\\ud800private-preview-sentinel"}'),
    ('text/plain',b'\xffprivate-preview-sentinel'),
])
def test_readable_malformed_preview_keeps_safe_review_actions(browser,payload,media_type):
    from app.contracts.workflow import ChangeKind
    client,svc,snapshot,manifest,principal,_=browser
    # The ordinary revised artifact remains registered and hash-valid.
    member=svc.state.store.put(payload,media_type,'1.0')
    changed=manifest.model_copy(update={'revision':2,'render':member})
    current=svc.state.revise(snapshot.story_id,changed,ChangeKind.VISUAL,snapshot.version,'malformed-render')
    assert svc.state.store.read(member)==payload
    response=client.get(f'/review/{snapshot.story_id}')
    assert response.status_code==200
    assert 'Preview content unavailable.' in response.text
    assert 'private-preview-sentinel' not in response.text
    assert 'value="reject"' in response.text and 'value="revise"' in response.text
    assert 'value="approve"' not in response.text
    payload=fields(response)|{'decision':'approve','notes':''}
    headers={'Origin':'http://127.0.0.1:8765'}
    assert client.post(f'/review/{snapshot.story_id}/decision',data=payload,headers=headers).status_code==403
    assert svc.state.get(snapshot.story_id)==current
    assert svc.conn.execute('SELECT count(*) FROM human_decisions').fetchone()[0]==0
    payload['decision']='reject'
    assert client.post(f'/review/{snapshot.story_id}/decision',data=payload,headers=headers).status_code==200
    assert svc.state.get(snapshot.story_id).state.value=='REJECTED'

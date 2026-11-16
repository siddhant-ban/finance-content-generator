def test_release_browser_decision_exists():
    from app.review.service import ReviewService
    assert callable(ReviewService.release_decide)

from tests.integration.test_review_ui import browser,fields
from tests.release_fixtures import evidence,candidate_for


def test_bootstrap_release_needs_distinct_human_action(browser,evidence,tmp_path):
    client,svc,s,_,principal,_=browser
    candidate=candidate_for(evidence,tmp_path/'evidence')
    gate=svc.releases.check(candidate)
    svc.decide(s.story_id,'approve',s.manifest_hash,s.version,'content',principal,'')
    assert svc.releases.active('foundation') is None
    path=f'/releases/{candidate.record_hash()}'
    page=client.get(path)
    assert page.status_code==200 and 'Explicit first release bootstrap' in page.text
    payload=fields(page)|{'decision':'approve'}
    assert client.post(path+'/decision',data=payload,headers={'Origin':'http://127.0.0.1:8765'}).status_code==403
    assert svc.releases.active('foundation') is None


def test_release_form_retry_reuses_key_without_new_event(browser,evidence,tmp_path):
    client,svc,_,_,principal,_=browser
    candidate=candidate_for(evidence,tmp_path/'evidence');svc.releases.check(candidate)
    path=f'/releases/{candidate.record_hash()}'
    payload=fields(client.get(path))|{'decision':'approve','bootstrap':'true'}
    for _ in range(2):
        response=client.post(path+'/decision',data=payload,headers={'Origin':'http://127.0.0.1:8765'})
        assert response.status_code==200
    assert svc.conn.execute('SELECT count(*) FROM release_events').fetchone()[0]==1
    active=svc.releases.active('foundation')
    svc.releases.revoke('foundation',principal,active.version)
    response=client.post(path+'/decision',data=payload,headers={'Origin':'http://127.0.0.1:8765'})
    assert response.status_code==200 and 'Currently active: false' in response.text
    assert svc.releases.active('foundation') is None
    payload['bootstrap']='false'
    assert client.post(path+'/decision',data=payload,headers={'Origin':'http://127.0.0.1:8765'}).status_code==409
    assert svc.conn.execute('SELECT count(*) FROM release_events').fetchone()[0]==2

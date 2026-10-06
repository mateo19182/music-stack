"""Advice and agent credentials cannot bypass the human publication decision."""
import time
from types import SimpleNamespace
from test_main import backend, signin, seed_review


def agent_token(client, **extra):
    response = client.post('/api/agents', json={'name': 'test-agent', **extra})
    assert response.status_code == 200
    data = response.json()
    return data, {'Authorization': 'Bearer ' + data['token']}


def test_agent_credentials_are_scoped_revocable_and_not_browser_sessions(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    data, headers = agent_token(client)
    assert client.get('/api/review', headers=headers).status_code == 200
    assert client.get('/api/jobs/job', headers=headers).status_code == 200
    for endpoint, body in [('/api/jobs/job/approve', {}), ('/api/jobs/job/reject', {}), ('/api/sharing', {'enabled': False}), ('/api/files/file/sharing', {'shared': False}), ('/api/agents', {'name': 'unauthorized'})]:
        assert client.post(endpoint, json=body, headers=headers).status_code == 403
    assert main.store.get('jobs', 'job')['stage'] == 'review'
    assert main.store.get('sessions', data['id']).get('token') is None
    listed = client.get('/api/agents').json()
    assert listed['agents'][0]['id'] == data['id']
    assert 'token' not in listed['agents'][0]
    client.cookies.clear()
    client.cookies.set('acquire_session', data['token'])
    assert client.get('/api/me').status_code == 401
    signin(main, client, monkeypatch)
    assert client.request('DELETE', '/api/agents/'+data['id'], json={}).status_code == 200
    assert client.get('/api/review', headers=headers).status_code == 401


def test_agent_tokens_expire_and_cannot_be_minted_by_regular_users(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    data, headers = agent_token(client)
    credential = main.store.get('sessions', data['id'])
    main.store.put('sessions', data['id'], {key: value for key,value in credential.items() if key not in {'id','expires'}}, expires=time.time()-1)
    assert client.get('/api/me', headers=headers).status_code == 401
    signin(main, client, monkeypatch, name='paulo', admin=False)
    assert client.post('/api/agents', json={'name': 'test'}).status_code == 403


def test_advice_is_cached_and_worker_never_publishes_or_edits(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    data, headers = agent_token(client)
    assert client.post('/api/jobs/job/advice', json={}, headers=headers).status_code == 503
    calls = []
    def review(payload):
        calls.append(payload)
        main.stop.set()
        return {'status': 'looks_fine', 'summary': 'Harmless tag differences.', 'files': [{'id': 'file', 'status': 'looks_fine', 'reason': 'Tags match.', 'suggested_tags': {'title': 'Suggested title'}, 'innocuous_warnings': []}], 'advisory': True}
    monkeypatch.setattr(main, 'reviewer', SimpleNamespace(configured=True, model='test-model', review=review))
    first = client.post('/api/jobs/job/advice', json={}, headers=headers).json()
    second = client.post('/api/jobs/job/advice', json={}, headers=headers).json()
    assert first == second and first['advice']['status']=='queued'
    main.stop.clear()
    try:
        main.advice_worker()
    finally:
        main.stop.clear()
    assert len(calls)==1
    job = main.store.get('jobs','job')
    assert job['stage']=='review' and not job.get('edits')
    assert main.store.get('files','file')['title']=='Proposed'
    cached = client.post('/api/jobs/job/advice', json={}, headers=headers).json()
    assert cached['advice']['status']=='complete'
    assert len(calls)==1
    response = client.get('/api/review',headers=headers).json()
    assert response['reviewer']['manual_approval_required']
    assert response['jobs'][0]['advice']['result']['files'][0]['suggested_tags']['title']=='Suggested title'
    assert client.post('/api/jobs/job/approve', json={}).status_code==200
    assert main.store.get('jobs','job')['stage']=='publish_queued'


def test_advice_respects_ownership_and_job_stage(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    monkeypatch.setattr(main, 'reviewer', SimpleNamespace(configured=True, model='test'))
    signin(main, client, monkeypatch, name='paulo', admin=False)
    assert client.post('/api/jobs/job/advice',json={}).status_code==404
    assert client.get('/api/jobs/job').status_code==404
    signin(main, client, monkeypatch)
    client.post('/api/jobs/job/reject',json={})
    assert client.post('/api/jobs/job/advice',json={}).status_code==409


def test_advice_provider_failure_keeps_manual_review_available(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    def failed(payload):
        main.stop.set()
        raise main.ReviewError('OpenRouter is unavailable.')
    monkeypatch.setattr(main, 'reviewer', SimpleNamespace(configured=True, model='test', review=failed))
    client.post('/api/jobs/job/advice',json={})
    main.stop.clear()
    try:
        main.advice_worker()
    finally:
        main.stop.clear()
    job=main.store.get('jobs','job')
    assert job['stage']=='review' and job['advice']['status']=='failed'
    assert client.post('/api/jobs/job/approve',json={}).status_code==200


def test_model_switch_invalidates_advice_cache(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    monkeypatch.setattr(main, 'reviewer', SimpleNamespace(configured=True, model='old-model'))
    old = client.post('/api/jobs/job/advice', json={}).json()['advice']
    main.store.update_job('job', advice={**old, 'status': 'complete', 'result': {'summary': 'Old advice'}})
    monkeypatch.setattr(main, 'reviewer', SimpleNamespace(configured=True, model='meta/muse-spark-1.3-contributor'))
    new = client.post('/api/jobs/job/advice', json={}).json()['advice']
    assert new['status'] == 'queued'
    assert new['model'] == 'meta/muse-spark-1.3-contributor'
    assert new['fingerprint'] != old['fingerprint']
    assert client.post('/api/jobs/job/advice', json={}).json()['advice'] == new
    assert main.store.get('jobs', 'job')['stage'] == 'review'


def test_approving_agent_publishes_without_editing_and_cannot_reject(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    data, headers = agent_token(client, can_approve=True)
    assert data['can_approve'] is True
    assert client.get('/api/agents').json()['agents'][0]['can_approve'] is True
    assert client.post('/api/jobs/job/reject', json={}, headers=headers).status_code == 403
    assert client.post('/api/sharing', json={'enabled': False}, headers=headers).status_code == 403
    assert client.post('/api/agents', json={'name': 'x'}, headers=headers).status_code == 403
    edit = {'files': [{'id': 'file', 'metadata': {'title': 'Changed'}}]}
    assert client.post('/api/jobs/job/approve', json=edit, headers=headers).status_code == 403
    assert main.store.get('jobs', 'job')['stage'] == 'review'
    assert client.post('/api/jobs/job/approve', json={}, headers=headers).status_code == 200
    job = main.store.get('jobs', 'job')
    assert job['stage'] == 'publish_queued' and job['approved_by'] == 'agent:test-agent'
    assert client.post('/api/jobs/job/approve', json={}, headers=headers).status_code == 409


def test_job_view_exposes_source_url(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    job = main.store.get('jobs', 'job')
    job['candidate'] = {'source': 'youtube', 'url': 'https://www.youtube.com/watch?v=abc'}
    main.store.put('jobs', 'job', job, owner='mateo', stage='review', created_at='2026-01-01')
    assert client.get('/api/jobs/job').json()['request']['source_url'] == 'https://www.youtube.com/watch?v=abc'

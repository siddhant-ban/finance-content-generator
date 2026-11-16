"""No-script authenticated HTML forms; source content is always inert text."""
from html import escape
import json
import re
import secrets
from urllib.parse import parse_qsl,urlsplit
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import HTMLResponse,Response
from app.auth.dependencies import require_reviewer
from app.workflow.state import StateConflict,PolicyDenied
from app.release.service import ReleaseConflict,ReleaseDenied
from app.release.models import ReleaseCandidate
from app.workflow.policy import MEMBERS

router=APIRouter()

async def parse_form(request):
    if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':
        raise HTTPException(415,'urlencoded_form_required')
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>16384: raise HTTPException(413,'form_too_large')
    try:
        text=body.decode('ascii')
        if re.search(r'%(?![a-fA-F0-9]{2})',text): raise ValueError()
        pairs=parse_qsl(text,keep_blank_values=True,strict_parsing=True,max_num_fields=12,encoding='utf-8',errors='strict')
        if len({k for k,v in pairs})!=len(pairs): raise ValueError()
        values=dict(pairs)
        if set(values)-{'csrf_token','idempotency_key','manifest_hash','expected_version','decision','notes','bootstrap'}: raise ValueError()
        request.state.review_form=values
        return values
    except (UnicodeError,ValueError):
        raise HTTPException(400,'invalid_form') from None

def service(request):
    value=request.app.state.services.review
    if value is None: raise HTTPException(503,'review_not_configured')
    return value

def hidden(name,value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value),quote=True)}">'

def form(request,path,fields,buttons):
    token=request.app.state.services.sessions.csrf_token(request.cookies['finance_session'])
    values={'csrf_token':token,'idempotency_key':secrets.token_urlsafe(32),**fields}
    return f'<form method="post" action="{path}">'+''.join(hidden(k,v) for k,v in values.items())+buttons+'</form>'

def inert(value):
    # JSON preserves every source field and locator without executing retrieved HTML.
    text=json.dumps(value,ensure_ascii=False,indent=2)
    urls=[]
    def links(v):
        if isinstance(v,dict):
            for item in v.values(): links(item)
        elif isinstance(v,list):
            for item in v: links(item)
        elif isinstance(v,str):
            try:
                parsed=urlsplit(v)
                if parsed.scheme in {'http','https'} and parsed.netloc:
                    urls.append(f'<a rel="noreferrer noopener" href="{escape(v,quote=True)}">Source</a>')
            except ValueError:
                pass
    links(value)
    return '<pre>'+escape(text)+'</pre>'+''.join(urls)

@router.get('/review/{story_id}',response_class=HTMLResponse)
async def review(story_id: UUID,request: Request,principal=Depends(require_reviewer)):
    svc=service(request)
    try: snapshot=svc.state.get(story_id)
    except StateConflict: raise HTTPException(404,'story_not_found') from None
    body=f'<h1>Content review</h1><p>Current status: {snapshot.state.value}</p>'
    if snapshot.manifest is None: return body+'<p>No review manifest available.</p>'
    body+=f'<p>{escape(snapshot.manifest.kind)} — synthetic artifacts are not publishable media.</p>'
    preview_available=True
    for name in MEMBERS:
        ref=getattr(snapshot.manifest,name)
        body+=f'<h2>{name}</h2>'
        try:
            data=svc.state.store.read(ref)
        except ValueError: raise HTTPException(409,'preview_integrity_conflict') from None
        try:
            if ref.media_type=='application/json':
                fragment=inert(json.loads(data.decode('utf-8')))
                fragment.encode('utf-8')  # Reject unpaired surrogate escapes before response encoding.
                body+=fragment
            elif ref.media_type.startswith('text/'): body+='<pre>'+escape(data.decode('utf-8'))+'</pre>'
        except (json.JSONDecodeError,UnicodeError):
            body+='<p>Preview content unavailable.</p>'
            preview_available=False
        if ref.media_type in SAFE_MEDIA:
            url=f'/review/{story_id}/media/{ref.sha256}'
            if ref.media_type.startswith('image/'): body+=f'<img src="{url}" alt="{name}">'
            elif ref.media_type.startswith('audio/'): body+=f'<audio controls src="{url}"></audio>'
            else: body+=f'<video controls src="{url}"></video>'
    body+=form(request,f'/review/{story_id}/decision',{'manifest_hash':snapshot.manifest_hash,'expected_version':snapshot.version},
        '<label>Notes<textarea name="notes" maxlength="8000"></textarea></label>'+''.join(f'<button name="decision" value="{action}">{action}</button>' for action in (('approve','reject','revise') if preview_available else ('reject','revise'))))
    return body

SAFE_MEDIA={'image/png','image/jpeg','image/webp','audio/mpeg','audio/wav','video/mp4','video/webm'}

@router.get('/review/{story_id}/media/{digest}')
async def media(story_id: UUID,digest: str,request: Request,principal=Depends(require_reviewer)):
    svc=service(request)
    try:
        snapshot=svc.state.get(story_id)
        refs=[getattr(snapshot.manifest,name) for name in MEMBERS] if snapshot.manifest else []
        ref=next((ref for ref in refs if ref.sha256==digest),None)
        if ref is None or ref.media_type not in SAFE_MEDIA: raise ValueError()
        return Response(svc.state.store.read(ref),media_type=ref.media_type)
    except (ValueError,StateConflict): raise HTTPException(404,'media_unavailable') from None

@router.post('/review/{story_id}/decision',response_class=HTMLResponse)
async def decision(story_id: UUID,request: Request,principal=Depends(require_reviewer)):
    svc=service(request); fields=request.state.review_form
    if set(fields)!={'csrf_token','idempotency_key','manifest_hash','expected_version','decision','notes'}: raise HTTPException(400,'invalid_form')
    try:
        receipt=svc.decide(story_id,fields['decision'],fields['manifest_hash'],int(fields['expected_version']),fields['idempotency_key'],principal,fields['notes'])
        current=svc.state.get(story_id)
        return f'<p>Historical decision receipt: {receipt.decision_id}</p><p>Current status: {current.state.value}</p>'
    except StateConflict: raise HTTPException(409,'review_conflict') from None
    except PolicyDenied: raise HTTPException(403,'review_denied') from None
    except ValueError: raise HTTPException(400,'invalid_form') from None

@router.get('/releases/{candidate_hash}',response_class=HTMLResponse)
async def release(candidate_hash: str,request: Request,principal=Depends(require_reviewer)):
    svc=service(request)
    row=svc.conn.execute('SELECT candidate FROM release_candidates WHERE candidate_hash=?',(candidate_hash,)).fetchone()
    if row is None: raise HTTPException(404,'release_not_found')
    candidate=ReleaseCandidate.model_validate_json(row[0]); gate,_=svc.releases._evaluate(candidate)
    buttons='<button name="decision" value="approve">Approve release</button>'
    bootstrap=gate.status=='REQUIRES_BOOTSTRAP_APPROVAL'
    if bootstrap: buttons='<label><input type="checkbox" name="bootstrap" value="true" required>Explicit first release bootstrap approval</label>'+buttons
    return '<h1>Separate release approval</h1><p>Current status: '+gate.status+'</p>'+inert(candidate.model_dump(mode='json'))+form(request,f'/releases/{candidate_hash}/decision',{'manifest_hash':candidate_hash,'expected_version':gate.current_version},buttons)

@router.post('/releases/{candidate_hash}/decision',response_class=HTMLResponse)
async def release_decision(candidate_hash: str,request: Request,principal=Depends(require_reviewer)):
    svc=service(request); fields=request.state.review_form
    if (set(fields)-{'csrf_token','idempotency_key','manifest_hash','expected_version','decision','bootstrap'} or fields.get('manifest_hash')!=candidate_hash or fields.get('decision')!='approve' or fields.get('bootstrap','false') not in {'false','true'}): raise HTTPException(400,'invalid_form')
    try:
        receipt=svc.release_decide(candidate_hash,int(fields['expected_version']),fields.get('bootstrap')=='true',principal,fields['idempotency_key'])
        active=svc.releases.active(receipt.agent_name)
        return f'<p>Historical receipt: {receipt.approval_id}</p><p>Currently active: {str(active is not None and active.approval_id==receipt.approval_id).lower()}</p>'
    except ReleaseConflict: raise HTTPException(409,'release_conflict') from None
    except (ReleaseDenied,PolicyDenied): raise HTTPException(403,'release_denied') from None
    except (ValueError,KeyError): raise HTTPException(400,'invalid_form') from None

"""Local browser authentication only. Decision routes belong to the next slice."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
import secrets
from urllib.parse import parse_qs
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from app.auth.github import AuthDenied, GitHubOAuth
from app.auth.sessions import SessionService
from app.auth.dependencies import require_reviewer
from app.config import Settings

@dataclass(repr=False)
class Services:
    oauth: GitHubOAuth
    sessions: SessionService
    review: object | None = field(default=None, kw_only=True)
    clock: Callable = field(default=lambda:datetime.now(timezone.utc))


def create_app(settings: Settings, services: Services) -> FastAPI:
    if not settings.auth_loopback_development:
        raise ValueError('explicit HTTP loopback development opt-in required')
    if services.oauth.settings != settings or services.sessions.settings != settings:
        raise ValueError('authentication service configuration mismatch')
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,redirect_slashes=False,
                telemetry={'tracing':False,'metrics':False,'logs':False,
                           'operation_spans':False,'auto_configure':False})
    app.state.services=services

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        # The raw callback query is consumed once, then removed from the ASGI scope.
        # No request logging, tracing middleware or provider instrumentation is installed.
        if request.scope['path'] == '/auth/github/callback':
            request.state.callback_query=parse_qs(request.scope.get('query_string',b'').decode('ascii',errors='replace'))
            request.scope['query_string']=b''
        hosts=request.headers.getlist('host')
        if (hosts != ['127.0.0.1:8765'] or request.url.scheme != 'http'
                or any(name.lower()=='forwarded' or name.lower().startswith('x-forwarded-') for name in request.headers)):
            response=JSONResponse({'detail':'untrusted_request'},status_code=400)
        else:
            response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'none'; img-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'"
        return response

    def cookie(response, name, value, *, http_only=True, max_age=28800):
        response.set_cookie(name,value,httponly=http_only,samesite='lax',secure=False,
                            max_age=max_age,path='/')

    @app.get('/auth/status')
    async def status():
        return {'configured':settings.auth_configured,
                'status':'ready' if settings.auth_configured else 'authentication_setup_required'}

    @app.get('/auth/login')
    async def login():
        binding=secrets.token_urlsafe(32)
        try:
            authorization=services.oauth.begin(binding)
        except AuthDenied:
            raise HTTPException(503,'authentication_setup_required') from None
        response=RedirectResponse(authorization.url,status_code=303)
        cookie(response,'finance_oauth_binding',binding,max_age=300)
        return response

    @app.get('/auth/github/callback')
    async def callback(request: Request):
        query=request.state.callback_query
        response=RedirectResponse('/auth/status',status_code=303)
        try:
            if set(query) != {'code','state'} or any(len(value)!=1 for value in query.values()):
                raise AuthDenied('authentication_denied')
            # Only the provider exchange leaves this event loop; SQLite remains
            # on its owning thread for verification, revocation and creation.
            identity=await run_in_threadpool(services.oauth.complete,query['code'][0],
                query['state'][0],request.cookies.get('finance_oauth_binding',''))
            old=request.cookies.get('finance_session')
            if old:
                services.sessions.logout(old)
            session=services.sessions.create(identity)
            cookie(response,'finance_session',session.value)
            cookie(response,'finance_csrf',services.sessions.csrf_token(session.value),http_only=False)
        except AuthDenied:
            response=JSONResponse({'detail':'authentication_denied'},status_code=401)
        response.delete_cookie('finance_oauth_binding',path='/')
        return response

    @app.get('/auth/session')
    async def session(request: Request, principal=Depends(require_reviewer)):
        response=JSONResponse({'authenticated':True})
        cookie(response,'finance_csrf',services.sessions.csrf_token(request.cookies['finance_session']),http_only=False)
        return response

    @app.post('/auth/logout',status_code=204)
    async def logout(request: Request, principal=Depends(require_reviewer)):
        services.sessions.logout(request.cookies['finance_session'])
        response=Response(status_code=204)
        response.delete_cookie('finance_session',path='/')
        response.delete_cookie('finance_csrf',path='/')
        return response

    from app.review.routes import router
    app.include_router(router)
    return app


def serve(settings: Settings, services: Services) -> None:
    """Explicit embedding entry point: loopback, one process, no access query logs."""
    import uvicorn
    uvicorn.run(create_app(settings,services),host='127.0.0.1',port=8765,
                proxy_headers=False,access_log=False,workers=1)

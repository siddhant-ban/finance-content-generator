"""Resolve reviewer identity exclusively from an authenticated browser cookie."""
from fastapi import Request, HTTPException
from app.contracts.common import Principal
from app.auth.github import AuthDenied

async def require_reviewer(request: Request) -> Principal:
    services=request.app.state.services
    cookie=request.cookies.get('finance_session','')
    if request.method not in {'GET','HEAD','OPTIONS'}:
        if len(request.headers.getlist('x-csrf-token'))>1: raise HTTPException(403,'csrf_required')
        token=request.headers.get('x-csrf-token','')
        is_review_form=(request.url.path.startswith('/review/') or request.url.path.startswith('/releases/')) and request.url.path.endswith('/decision')
        if is_review_form:
            from app.review.routes import parse_form
            fields=await parse_form(request)
            form_token=fields.get('csrf_token','')
            if token and token!=form_token: raise HTTPException(403,'csrf_required')
            token=form_token
        if (request.headers.getlist('origin') != ['http://127.0.0.1:8765']
                or not services.sessions.verify_csrf(cookie,token)):
            raise HTTPException(403, 'csrf_required')
    try:
        return services.sessions.require(cookie,services.clock())
    except AuthDenied:
        raise HTTPException(401,'authentication_required') from None

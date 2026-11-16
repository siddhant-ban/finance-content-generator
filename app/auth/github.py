"""One-time GitHub authorization-code flow, with no persisted OAuth credentials."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import secrets
import re
import threading
import httpx
from pydantic import Field
from authlib.oauth2.rfc6749.parameters import prepare_grant_uri
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from app.contracts.common import StrictModel
from app.config import Settings

class AuthDenied(ValueError):
    """Generic denial; never attach provider responses or credentials."""

class GitHubIdentity(StrictModel):
    account_id: int = Field(gt=0, repr=False)
    account_type: str
    display_login: str = Field(repr=False, exclude=True)

@dataclass(frozen=True)
class AuthorizationRequest:
    url: str = field(repr=False)

@dataclass(repr=False)
class _Pending:
    binding: str
    verifier: str
    created: float

class GitHubOAuth:
    def __init__(self, settings: Settings, *, clock=None, transport=None):
        self.settings = settings
        self.clock = clock or (lambda:datetime.now(timezone.utc))
        self.transport = transport
        self._pending = {}
        self._lock = threading.Lock()

    def begin(self, browser_binding: str) -> AuthorizationRequest:
        if not self.settings.auth_configured or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',browser_binding):
            raise AuthDenied('authentication_setup_required')
        now = self.clock().timestamp()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with self._lock:
            self._pending = {key:value for key,value in self._pending.items() if now-value.created < 300}
            if len(self._pending) >= 1000:
                raise AuthDenied('authentication_unavailable')
            self._pending[state] = _Pending(browser_binding, verifier, now)
        return AuthorizationRequest(prepare_grant_uri(
            'https://github.com/login/oauth/authorize', self.settings.github_client_id,
            response_type='code', redirect_uri=self.settings.auth_callback_url,
            state=state, scope='', code_challenge=create_s256_code_challenge(verifier),
            code_challenge_method='S256') + '&scope=')

    def complete(self, code: str, state: str, browser_binding: str) -> GitHubIdentity:
        with self._lock:
            pending = self._pending.pop(state, None)
        if (not self.settings.auth_configured or pending is None or not code
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',browser_binding)
                or not secrets.compare_digest(pending.binding, browser_binding)
                or not 0 <= self.clock().timestamp()-pending.created < 300):
            raise AuthDenied('authentication_denied')
        # No provider payload escapes this boundary. No proxies/netrc/redirects/cookies.
        try:
            with httpx.Client(transport=self.transport, trust_env=False,
                              follow_redirects=False, timeout=10.0) as client:
                response = client.post('https://github.com/login/oauth/access_token',
                    headers={'Accept':'application/json'}, data={
                        'client_id':self.settings.github_client_id,
                        'client_secret':self.settings.github_client_secret.get_secret_value(),
                        'code':code, 'code_verifier':pending.verifier,
                        'redirect_uri':self.settings.auth_callback_url})
                if response.status_code != 200:
                    raise ValueError()
                payload = response.json()
                token = payload.get('access_token')
                if not isinstance(token,str) or not token or payload.get('token_type','').lower() != 'bearer':
                    raise ValueError()
                client.cookies.clear()
                response = client.get('https://api.github.com/user', headers={
                    'Authorization':'Bearer '+token, 'Accept':'application/vnd.github+json',
                    'X-GitHub-Api-Version':'2026-03-10'})
                if response.status_code != 200:
                    raise ValueError()
                user = response.json()
                identity = GitHubIdentity(account_id=user['id'], account_type=user['type'], display_login=user['login'])
                if identity.account_type != 'User' or identity.account_id not in self.settings.github_allowed_ids:
                    raise ValueError()
                return identity
        except Exception:
            raise AuthDenied('authentication_denied') from None

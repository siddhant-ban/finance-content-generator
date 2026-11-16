"""Hashed server-side sessions sharing the business database and its write lock."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
from app.contracts.common import Principal
from app.auth.github import AuthDenied, GitHubIdentity

@dataclass(frozen=True)
class SessionCookie:
    value: str = field(repr=False)

class SessionService:
    def __init__(self, conn, settings, *, clock=None):
        self.conn, self.settings = conn, settings
        self.clock = clock or (lambda:datetime.now(timezone.utc))
        self._csrf_key = secrets.token_bytes(32)

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def _idle(self):
        if self.conn.in_transaction:
            raise AuthDenied('idle_connection_required')

    def create(self, identity: GitHubIdentity) -> SessionCookie:
        self._idle()
        if (not self.settings.auth_configured or identity.account_type != 'User'
                or identity.account_id not in self.settings.github_allowed_ids):
            raise AuthDenied('authentication_denied')
        cookie = SessionCookie(secrets.token_urlsafe(32))
        now = self.clock().timestamp()
        with self.conn:
            self.conn.execute('INSERT INTO reviewer_sessions VALUES (?,?,?,?,?,?,0)',
                (self._hash(cookie.value), secrets.token_urlsafe(32), identity.account_id, now, now, now+28800))
        return cookie

    def _valid(self, row, now):
        return bool(self.settings.auth_configured and row and not row[5]
                    and row[1] in self.settings.github_allowed_ids
                    and row[2] <= now < row[4] and 0 <= now-row[3] < 1800)

    def require(self, session_cookie: str, now: datetime) -> Principal:
        self._idle()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute('SELECT session_id,account_id,created,last_seen,expires,revoked FROM reviewer_sessions WHERE cookie_hash=?',
                                    (self._hash(session_cookie),)).fetchone()
            if not self._valid(row, now.timestamp()):
                raise AuthDenied('authentication_required')
            self.conn.execute('UPDATE reviewer_sessions SET last_seen=? WHERE session_id=?',(now.timestamp(),row[0]))
            self.conn.commit()
            return Principal(provider='github',account_id=row[1],kind='human',session_id=row[0])
        except BaseException:
            self.conn.rollback()
            raise

    def verify_principal(self, principal: Principal) -> bool:
        """Read-only, no commit: safe before and inside ReleaseService transactions."""
        if principal.provider != 'github' or principal.kind != 'human' or not principal.session_id:
            return False
        row=self.conn.execute('SELECT session_id,account_id,created,last_seen,expires,revoked FROM reviewer_sessions WHERE session_id=?',
                              (principal.session_id,)).fetchone()
        return self._valid(row,self.clock().timestamp()) and row[1] == principal.account_id

    def logout(self, session_cookie: str) -> None:
        self._idle()
        with self.conn:
            self.conn.execute('UPDATE reviewer_sessions SET revoked=1 WHERE cookie_hash=?',(self._hash(session_cookie),))

    def csrf_token(self, cookie: str) -> str:
        return hmac.new(self._csrf_key,cookie.encode(),hashlib.sha256).hexdigest()

    def verify_csrf(self, cookie: str, token: str) -> bool:
        return bool(cookie and re.fullmatch(r'[0-9a-f]{64}',token)
                    and secrets.compare_digest(self.csrf_token(cookie),token))

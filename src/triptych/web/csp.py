"""The Content-Security-Policy both apps (web and admin) send. Every script and stylesheet is a
Vite-built file under /static and every image is ours (destination photos are proxied through
/trips/<id>/photo), so nothing needs another origin or anything inline: no inline <script>, <style>,
style="" or on*="" handlers in the templates (frontend/src does that work instead), and htmx is told
not to eval or inject its indicator <style> (live.ts)."""

from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",  # the SSE stream and passkey ceremony fetches
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)


async def content_security_policy(request: Request, call_next: RequestResponseEndpoint) -> Response:
    resp = await call_next(request)
    resp.headers.setdefault("content-security-policy", POLICY)
    return resp

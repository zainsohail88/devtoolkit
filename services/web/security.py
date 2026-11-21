"""The security headers and cache rules every response gets."""

from __future__ import annotations

import base64
import hashlib

# Applies a saved theme choice before first paint, so the page never flashes the wrong theme.
# It must be inline (a deferred script is too late), so the CSP allows exactly this script by
# hash. Change the text here and the hash follows.
THEME_SCRIPT = (
    'try{var t=localStorage.getItem("theme");'
    'if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}'
    "catch(e){}"
)
THEME_SCRIPT_HASH = base64.b64encode(hashlib.sha256(THEME_SCRIPT.encode()).digest()).decode()

# Scripts: only our own files plus the theme script by hash. Styles: the code editor positions
# its cursor and layers with inline style attributes and injects its own stylesheet, which a
# nonce cannot cover, so inline styles are allowed; every piece of user content is escaped.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'self'; script-src 'self' 'sha256-{THEME_SCRIPT_HASH}'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
# Static URLs carry a content hash, so files can be cached for a year and a new build still
# shows up at once. HTML is always revalidated: its URL stays the same while its content changes.
STATIC_CACHE = "public, max-age=31536000, immutable"
HTML_CACHE = "public, max-age=0, must-revalidate"
STATIC_PREFIX = "/static/"
HTML_CONTENT_TYPE = "text/html"

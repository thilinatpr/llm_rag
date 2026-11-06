"""Plain-language text for a failed model call: users never see provider errors, billing states or exception class names.

The real error stays in the logs (chat log `error`, container stdout); only the reply to the browser is replaced.
`public(exc)` -> (HTTP status, message). Credit/auth/quota problems are the owner's to fix, not the user's, so they all read
"temporarily unavailable"; a rate limit or timeout says to try again shortly.
"""
import re

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again later."
BUSY = "The assistant is busy right now. Please try again in a minute."
FAILED = "Sorry, the answer could not be produced. Please try again."

_ACCOUNT = re.compile(r"insufficient\s+(?:balance|quota|credit|funds)|\b402\b|payment\s+required|billing|exceeded\s+your\s+(?:current\s+)?quota"
                      r"|invalid[\s_-]*api[\s_-]*key|incorrect\s+api\s+key|authentication|unauthori[sz]ed|\b40[13]\b|api\s*key|permission", re.I)
_BUSY = re.compile(r"rate[\s_-]*limit|too\s+many\s+requests|\b429\b|overloaded|\b50[234]\b|server\s+is\s+busy|capacity", re.I)
_DOWN = re.compile(r"timed?\s*out|timeout|connection|unreachable|name\s+or\s+service|temporar|ssl|reset\s+by\s+peer|remote\s+end", re.I)


def public(exc):
    """(status, message) for the browser. 503 for anything the user can retry or wait out (the public lane then refunds the quota)."""
    text = f"{type(exc).__name__}: {exc}"
    code = getattr(exc, "status_code", None)
    if code in (401, 402, 403) or _ACCOUNT.search(text):
        return 503, UNAVAILABLE
    if code in (408, 429, 500, 502, 503, 504, 529) or _BUSY.search(text) or _DOWN.search(text):
        return 503, BUSY
    return 500, FAILED

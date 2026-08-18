"""The self-declaration marker, as a thing a program WRITES.

`_capture_exclusions.py` is the read side: it decides whether a session was
declared. This module is the write side, and it is a separate file for one
reason - vendoring. Foundry's jobs and the fleet control-plane have to carry the
marker without carrying the reader, and the reader imports `_jsonl_format.py`
(467 lines of session parsing they have no use for). A writer that had to vendor
the reader would either drag that along or, far more likely, hand-type the
marker string. So the format lives here, in a file with no local imports at all,
and the reader imports it from this side.

**Never hand-type the marker.** The reader fails OPEN by construction: an
unrecognised marker means the session is captured exactly as if no marker had
been written. A near-miss is therefore silent - it looks done and is not, and
the session lands in the corpus as if a human had thought it. One formatter,
used by both sides, is what keeps writer and reader from drifting apart.

**Invalid fields raise here, unlike the TypeScript mirror.** Vault-sentinel's
`declareCapture` normalises its `reason` instead, because its call sites pass
free-text log labels ("Claude FB-clip") that must not be allowed to produce no
marker at all. Python call sites pass a literal the job knows about itself
(`reason="deep-compile"`), so an illegal value is a typo in code rather than a
user string - and a normaliser would quietly rename the declaring program, which
is the one field an operator reads the marker for.

The format itself is a locked contract owned by SPEC-capture-self-declaration.
"""
from __future__ import annotations

import re

#: The authoritative marker format, written as the first line of the first user
#: message of any machine-driven `claude` invocation.
DECLARATION_MARKER = "<!-- claude-capture: exclude program={program} reason={reason} -->"

#: The date the marker went live in production. Before it, a `legacy` hit is the
#: expected shape of a machine session (grandfathered, cleaned up by its own
#: plan). On or after it, a `legacy` hit is a program that lost or never had the
#: marker - an undeclared headless job - and memory-capture reports those
#: separately from the plain layer count.
DECLARATION_LIVE_DATE = "2026-08-12"

#: Both fields are required, `program` comes before `reason`, and both are
#: constrained to a plain identifier charset. A marker that misses one, or
#: carries something else, does not match - and the session is captured, which
#: is the direction this whole mechanism fails in.
DECLARATION_RE = re.compile(
    r"<!--\s*claude-capture:\s*exclude\s+"
    r"program=([A-Za-z0-9][A-Za-z0-9_.-]*)\s+"
    r"reason=([A-Za-z0-9][A-Za-z0-9_.-]*)\s*-->"
)

#: The same charset as the two capture groups above, anchored whole so a value
#: cannot pass validation on a legal prefix and then be read as that prefix.
_FIELD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def capture_marker(program: str, reason: str) -> str:
    """The marker line alone, with no trailing newline.

    For callers whose transport does not treat a newline as inert. The
    control-plane's interactive REPL lane is the case that forced this out:
    it TYPES the prompt into a live pane and then presses Enter, so an
    embedded newline submits the turn halfway through. Prefixing the marker
    plus a space still matches - the reader normalises whitespace before it
    anchors at the start.

    Raises ``ValueError`` when ``program`` or ``reason`` falls outside the
    contract's charset, rather than emitting a marker the reader will not
    recognise. The caller is a job naming itself, so the only way to get here
    is a mistake worth stopping on.
    """
    for field, value in (("program", program), ("reason", reason)):
        if not _FIELD_RE.match(value or ""):
            raise ValueError(
                f"{field}={value!r} is not a legal declaration field: expected "
                r"[A-Za-z0-9][A-Za-z0-9_.-]*"
            )
    return DECLARATION_MARKER.format(program=program, reason=reason)


def declare_capture(prompt: str, program: str, reason: str) -> str:
    """``prompt`` with the declaration marker as its first line."""
    return capture_marker(program, reason) + "\n" + prompt

"""robots.txt as RFC 9309 defines it — not as `urllib.robotparser` reads it.

The standard library's parser predates the RFC and gets two things wrong that
matter here:

* **Wildcards.** It compares paths literally, so `Disallow: /*&page=` — the
  rule Himalayas uses to keep crawlers off its pagination — matches nothing,
  and a client that trusted it would page straight through a refusal.
* **Precedence.** It applies the FIRST rule whose prefix matches. RFC 9309
  §2.2.2 says the MOST SPECIFIC one wins (the longest pattern), and that an
  allow beats a disallow of the same length. `Disallow: /` followed by
  `Allow: /jobs/` means /jobs/ is allowed; first-match says it is not.

This module does only the reading and matching. Fetching, caching and the
"unreachable means allowed" rule stay in `RobotsGate` (`base.py`).
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse


@dataclass
class Group:
    """One `User-agent:` block (or several merged, per §2.2.1)."""

    agents: list[str] = field(default_factory=list)
    #: (allow, pattern) in file order. Order does not decide anything; length does.
    rules: list[tuple[bool, str]] = field(default_factory=list)
    crawl_delay: float | None = None


@dataclass
class Robots:
    groups: list[Group] = field(default_factory=list)

    def group_for(self, user_agent: str) -> Group | None:
        """The rules that apply to this client, merged.

        Matching is on the product token — `PitWall` in `PitWall/2.0 (…)` —
        case-insensitively. Every group naming it is merged; only when none
        does are the `*` groups used (§2.2.1).
        """
        token = product_token(user_agent)
        named = [g for g in self.groups if any(a != "*" and a == token for a in g.agents)]
        chosen = named or [g for g in self.groups if "*" in g.agents]
        if not chosen:
            return None
        merged = Group(agents=[token])
        for group in chosen:
            merged.rules.extend(group.rules)
            if group.crawl_delay is not None:
                merged.crawl_delay = max(merged.crawl_delay or 0.0, group.crawl_delay)
        return merged

    def allows(self, user_agent: str, url: str) -> bool:
        group = self.group_for(user_agent)
        if group is None:
            return True
        target = _path_of(url)
        if target == "/robots.txt":
            return True     # §2.2.2: the file itself is always fetchable
        best_len = -1
        best_allow = True
        for allow, pattern in group.rules:
            if not _matches(pattern, target):
                continue
            length = len(pattern)
            # Longest wins; on a tie the least restrictive rule — allow — wins.
            if length > best_len or (length == best_len and allow and not best_allow):
                best_len, best_allow = length, allow
        return best_allow

    def crawl_delay(self, user_agent: str) -> float | None:
        group = self.group_for(user_agent)
        return group.crawl_delay if group else None


def product_token(user_agent: str) -> str:
    """`PitWall/2.0 (personal job search)` -> `pitwall`."""
    head = (user_agent or "").strip().split("/")[0].split(" ")[0]
    return head.lower() or "*"


def _path_of(url: str) -> str:
    parts = urlparse(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return _normalise(path)


def _normalise(text: str) -> str:
    """Percent-decoding both sides lets `/caf%C3%A9` and `/café` meet."""
    return unquote(text)


def _matches(pattern: str, target: str) -> bool:
    """`*` is any run of characters, a trailing `$` anchors the end, and the
    pattern is otherwise a prefix — §2.2.3."""
    pattern = _normalise(pattern)
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    body = ".*".join(re.escape(piece) for piece in pattern.split("*"))
    return re.match(body + ("$" if anchored else ""), target) is not None


def parse(text: str) -> Robots:
    """Read a robots.txt body. Unknown lines are ignored, never fatal."""
    robots = Robots()
    current: Group | None = None
    # A group's user-agent lines are consecutive; a rule line closes the list,
    # so the next user-agent line starts a new group.
    collecting_agents = False
    for raw_line in (text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not collecting_agents:
                current = Group()
                robots.groups.append(current)
                collecting_agents = True
            current.agents.append(value.lower().split("/")[0] or "*")
            continue
        if current is None:
            continue    # rules before any user-agent apply to nobody
        collecting_agents = False
        if key in ("allow", "disallow"):
            if not value:
                continue    # an empty Disallow means "nothing is disallowed"
            if not value.startswith(("/", "*")):
                value = "/" + value
            current.rules.append((key == "allow", value))
        elif key == "crawl-delay":
            with contextlib.suppress(ValueError):
                current.crawl_delay = float(value)
    return robots

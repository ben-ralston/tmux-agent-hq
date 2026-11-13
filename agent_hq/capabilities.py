"""Probe the installed launch.sh for which agent harnesses it supports.

Stage 1A (#205) gave ``~/.claude/skills/worktree/scripts/launch.sh`` a
``--capabilities`` flag that prints versioned JSON describing which agent
harnesses (claude, codex, ...) and CLI flags it supports:

    {"contract_version":1,"agents":["claude","codex"],
     "flags":{"claude":["--model","--effort"],"codex":["--model","--effort"]}}

Agent HQ must only offer non-Claude dispatch (e.g. codex) when the installed
launcher actually advertises it — an older launch.sh predating this contract
must never be treated as codex-capable. ``probe_launcher_capabilities`` is
the single read boundary: it shells out to the launcher, validates the
contract, and degrades to ``CLAUDE_ONLY`` on *any* failure (missing binary,
non-zero exit, timeout, malformed JSON, wrong contract version, wrong
field types) — this function never raises.

Probing means an extra subprocess spawn, so results are cached in-process
against the resolved launcher path with a monotonic-clock TTL deadline
(default 300s), mirroring the other in-process caches in this codebase
(e.g. ``github.py``). ``clear_capabilities_cache()`` resets it
for tests.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_hq import eventlog

log = logging.getLogger(__name__)

DEFAULT_LAUNCHER_PATH = Path.home() / ".claude/skills/worktree/scripts/launch.sh"

_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class LauncherCapabilities:
    """What an installed launch.sh advertises via ``--capabilities``."""

    agents: frozenset[str] = field(default_factory=frozenset)
    flags: dict[str, tuple[str, ...]] = field(default_factory=dict)


CLAUDE_ONLY = LauncherCapabilities(agents=frozenset({"claude"}), flags={})

# (resolved path) -> (capabilities, monotonic deadline)
_cache: dict[Path, tuple[LauncherCapabilities, float]] = {}


def clear_capabilities_cache() -> None:
    """Drop all cached probe results. Tests must call this so cached
    results from one test never leak into the next."""
    _cache.clear()


def _parse_capabilities(stdout: str) -> LauncherCapabilities | None:
    """Parse and validate the ``--capabilities`` JSON contract.

    Returns ``None`` (never raises) if the payload doesn't satisfy the
    contract: wrong/missing ``contract_version``, ``agents`` not a list of
    strings, or malformed JSON. ``flags`` is optional; a missing or
    malformed per-agent entry degrades to an empty tuple for that agent
    rather than failing the whole probe.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("contract_version") != _CONTRACT_VERSION:
        return None
    agents = data.get("agents")
    if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
        return None

    raw_flags = data.get("flags")
    flags: dict[str, tuple[str, ...]] = {}
    if isinstance(raw_flags, dict):
        for agent in agents:
            agent_flags = raw_flags.get(agent)
            if isinstance(agent_flags, list) and all(isinstance(f, str) for f in agent_flags):
                flags[agent] = tuple(agent_flags)
            else:
                flags[agent] = ()
    else:
        flags = {agent: () for agent in agents}

    return LauncherCapabilities(agents=frozenset(agents), flags=flags)


def probe_launcher_capabilities(
    launcher: Path | None = None, *, ttl: float = 300.0
) -> LauncherCapabilities:
    """Probe ``launcher --capabilities`` (default: ``DEFAULT_LAUNCHER_PATH``).

    Cached in-process against the resolved launcher path for ``ttl``
    seconds (monotonic clock). Every failure path — the launcher binary
    missing, a non-zero exit, a hang past the 3s subprocess timeout,
    non-UTF-8 output, unparseable stdout, or a contract mismatch — returns
    ``CLAUDE_ONLY`` instead of raising, and that negative result is cached
    too (so a persistently-broken launcher doesn't get re-probed every
    call). The timeout is short (3s, down from an original 10s -- Fix 7,
    multi-review wave) because the launcher prints its JSON instantly; this
    call can run synchronously on a web request thread or the TUI's event
    loop (see the ``issues``/``agents`` route call sites, and
    ``AgentDashboard``'s mount-time warmup worker), so a hang here must not
    be able to stall either for as long as 10s.
    """
    path = launcher if launcher is not None else DEFAULT_LAUNCHER_PATH
    resolved = Path(path)

    cached = _cache.get(resolved)
    now = time.monotonic()
    if cached is not None and now < cached[1]:
        return cached[0]

    caps = _probe_uncached(resolved)
    _cache[resolved] = (caps, now + ttl)
    return caps


def _probe_uncached(resolved: Path) -> LauncherCapabilities:
    try:
        result = subprocess.run(
            [str(resolved), "--capabilities"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, ValueError) as exc:
        # UnicodeDecodeError/ValueError: subprocess.run(text=True) decodes
        # stdout internally and can raise on non-UTF-8 launcher output --
        # that must degrade to CLAUDE_ONLY like every other failure mode,
        # not propagate out of a probe this module promises never raises.
        log.debug("launcher capabilities probe failed for %s: %s", resolved, exc)
        eventlog.emit(
            "warn",
            "capabilities probe degraded to claude-only",
            {"launcher": str(resolved), "reason": str(exc)},
            min_interval_s=3600,
        )
        return CLAUDE_ONLY

    if result.returncode != 0:
        log.debug("launcher capabilities probe for %s exited %d", resolved, result.returncode)
        eventlog.emit(
            "warn",
            "capabilities probe degraded to claude-only",
            {"launcher": str(resolved), "reason": f"exit {result.returncode}"},
            min_interval_s=3600,
        )
        return CLAUDE_ONLY

    caps = _parse_capabilities(result.stdout)
    if caps is None:
        log.debug("launcher capabilities probe for %s returned an invalid contract", resolved)
        eventlog.emit(
            "warn",
            "capabilities probe degraded to claude-only",
            {"launcher": str(resolved), "reason": "invalid contract"},
            min_interval_s=3600,
        )
        return CLAUDE_ONLY
    return caps


def supports_harness(caps: LauncherCapabilities, name: str) -> bool:
    """Whether ``caps`` advertises the ``name`` agent harness (e.g. "codex")."""
    return name in caps.agents


def supported_flags(caps: LauncherCapabilities, harness: str) -> tuple[str, ...]:
    """CLI flags ``caps`` advertises for ``harness``, or ``()`` if unknown."""
    return caps.flags.get(harness, ())

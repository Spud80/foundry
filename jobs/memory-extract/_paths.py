"""Centralized vault-path resolution for cortex memory pipeline.

Single source of truth for default vault-root path. Replaces 17+ duplicated
``sys.platform``-branching blocks across ``scripts/memory/`` and
``scripts/cortex-annotations/``.

Per CLAUDE.md "Multi-user design (v0 discipline)" - all path-handling must
be centralized. This helper is the v0 implementation of that contract,
ahead of a full config-layer expected at Hemit-transfer.

Precedence (lowest -> highest):
  1. Platform default (linux: /data/sync/obsidian/My Vault,
                       other: C:/sync/obsidian/My Vault)
  2. OBSIDIAN_VAULT environment variable (alias under retirement)
  3. OBSIDIAN_VAULT_ROOT environment variable
  4. ``--vault-root`` CLI flag (callers pass args.vault_root explicitly via
     ``resolve_vault_root(cli_value)``)

Two env-var names, one value - a transition, not an endpoint
------------------------------------------------------------
``OBSIDIAN_VAULT_ROOT`` is the name; ``OBSIDIAN_VAULT`` is an alias that four
consumers grew independently (this module, ``cortex_backend.py``,
``harness-index.py`` and ``session-start-40-memory.sh``, spread over three
repos and four hosts). Every one of them now reads both, with the precedence
above, so a host can be moved without a rename window.

Dual-read exists to de-risk one cutover, not because the ambiguity is wanted.
When the two names point at different roots the resolver says so on stderr
instead of picking one quietly, and the alias is retired once that warning has
stayed silent across every host for a measured period (the trigger is written
down in PLAN-memory-raw-relocation, env-harmonisation phase). Do not add a
fifth consumer that reads only the alias.

Usage:
    from _paths import resolve_vault_root, resolve_memory_dir

    vault = resolve_vault_root(args.vault_root)       # env-aware
    memory = resolve_memory_dir(args.memory_dir,
                                args.vault_root)      # cortex-memory dir


Raw-root contract (declared home)
---------------------------------
This module is the single declared home for WHERE the raw session corpus
lives and for WHAT the lookup is called. Three parties carry that knowledge
and they live in three independently deployed repos:

  * ``_paths.py``            (cortex)          - this file, the definition
  * ``cortex_backend.py``    (dev-environment) - drill-down from an
                                                 extracted hit
  * ``jobs/audit/audit.py``  (foundry)         - the raw index that
                                                 ``source_session:``
                                                 validation looks up in

None of them can import this module, so each reads ``CORTEX_RAW_ROOT``
directly and falls back to the same default sub-path. The duplicated
knowledge is therefore exactly two values - the env-var name and the default
segments - and both are pinned by a generated fixture
(``cortex/tests/memory/raw-root-parity-fixtures.json``) that each party
verifies from its own side. Change either value here, regenerate the
fixture with ``tests/memory/_compute_raw_root_fixture.py``, and the other
two repos fail until they follow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

VAULT_ROOT_ENV = "OBSIDIAN_VAULT_ROOT"
#: Alias under retirement (see the schedule-alias-retirement note in the
#: raw-root contract docstring above). Two names for one value grew up across
#: four consumers in three repos; every one of them reads both while the raw
#: relocation cutover moves hosts one at a time.
VAULT_ROOT_ENV_ALIAS = "OBSIDIAN_VAULT"
RAW_ROOT_ENV = "CORTEX_RAW_ROOT"
LINUX_DEFAULT = Path("/data/sync/obsidian/My Vault")
WINDOWS_DEFAULT = Path("C:/sync/obsidian/My Vault")
MEMORY_SUBDIR = ("8.Cortex", "Memory")
RAW_LEAF = "raw"
#: Default raw-root relative to vault-root. The leaf stays ``raw/<date>/``
#: after relocation, so every path literal measured FROM the raw root keeps
#: holding; only the segments above it move.
RAW_SUBDIR = MEMORY_SUBDIR + (RAW_LEAF,)


def platform_default_vault_root() -> Path:
    """Return platform-specific default vault-root (no env-var lookup)."""
    if sys.platform.startswith("linux"):
        return LINUX_DEFAULT
    return WINDOWS_DEFAULT


_env_notice_emitted = False


def _note_env_choice(message: str) -> None:
    """Emit one line on stderr, at most once per process.

    Once per process because ``default_vault_root()`` is called in loops; the
    signal is "this host is configured oddly", which needs saying once.
    """
    global _env_notice_emitted
    if _env_notice_emitted:
        return
    _env_notice_emitted = True
    print(message, file=sys.stderr)


def vault_root_from_env() -> Optional[str]:
    """Return the env-configured vault root, reading both names.

    Precedence: ``OBSIDIAN_VAULT_ROOT`` over the ``OBSIDIAN_VAULT`` alias.

    Deliberately silent when the primary name decides: that is the intended
    steady state and announcing it on every process would land in hook output
    the user reads. The two cases worth a line are the ones that mean a host
    is mid-migration - the alias deciding, and the two names disagreeing.
    Both are what the alias-retirement trigger is measured on.
    """
    primary = os.environ.get(VAULT_ROOT_ENV) or None
    alias = os.environ.get(VAULT_ROOT_ENV_ALIAS) or None
    if primary and alias and Path(primary) != Path(alias):
        _note_env_choice(
            f"WARNING: {VAULT_ROOT_ENV}={primary} and {VAULT_ROOT_ENV_ALIAS}={alias} "
            f"point at different roots; using {VAULT_ROOT_ENV}. Unset "
            f"{VAULT_ROOT_ENV_ALIAS} or make them agree."
        )
        return primary
    if primary:
        return primary
    if alias:
        _note_env_choice(
            f"vault-root from {VAULT_ROOT_ENV_ALIAS}={alias} "
            f"({VAULT_ROOT_ENV_ALIAS} is an alias under retirement; "
            f"set {VAULT_ROOT_ENV} instead)"
        )
        return alias
    return None


def default_vault_root() -> Path:
    """Return default vault-root: env (either name) or platform default."""
    env = vault_root_from_env()
    if env:
        return Path(env)
    return platform_default_vault_root()


def resolve_vault_root(cli_value: Optional[str] = None) -> Path:
    """Resolve vault-root with precedence: CLI > env > platform-default.

    Callers typically pass ``args.vault_root`` (or ``None``) as ``cli_value``.
    """
    if cli_value:
        return Path(cli_value)
    return default_vault_root()


def resolve_memory_dir(
    cli_memory_dir: Optional[str] = None,
    cli_vault_root: Optional[str] = None,
) -> Path:
    """Resolve cortex memory-dir: explicit CLI > vault-root-based default.

    ``8.Cortex/Memory`` is a fixed sub-path under vault-root per SPEC.
    """
    if cli_memory_dir:
        return Path(cli_memory_dir)
    return resolve_vault_root(cli_vault_root).joinpath(*MEMORY_SUBDIR)


def resolve_raw_dir(
    cli_raw_root: Optional[str] = None,
    cli_memory_dir: Optional[str] = None,
    cli_vault_root: Optional[str] = None,
) -> Path:
    """Resolve the raw session-corpus root: CLI > env > memory-dir default.

    The default is ``resolve_memory_dir(...) / "raw"``, i.e. bit-identical to
    what every caller composed by hand before this helper existed. Relocation
    is therefore an env change (``CORTEX_RAW_ROOT``), not a code change.

    Unlike the vault root, the raw root can live OUTSIDE the vault, so callers
    must not assume the result is a descendant of ``resolve_vault_root()``.
    """
    if cli_raw_root:
        return Path(cli_raw_root)
    env = os.environ.get(RAW_ROOT_ENV)
    if env:
        return Path(env)
    return resolve_memory_dir(cli_memory_dir, cli_vault_root) / RAW_LEAF


class RawRootUnavailable(RuntimeError):
    """The configured raw-session root does not exist.

    Kept distinct from an EMPTY root, which is an ordinary quiet night. The two
    were indistinguishable across the raw consumers - a missing root read as
    "no new sessions" - and that is how the pipeline once stood still for 50
    hours without saying anything (2026-06-06). Relocation makes the root
    genuinely able to be wrong, so the distinction has to be real.
    """


def require_raw_dir(
    cli_raw_root: Optional[str] = None,
    cli_memory_dir: Optional[str] = None,
    cli_vault_root: Optional[str] = None,
) -> Path:
    """``resolve_raw_dir()``, but raise when the resolved root is not a dir.

    For every caller that would otherwise treat "root missing" as "nothing to
    do". A caller that legitimately handles an absent root - a bootstrap that
    creates it - should use ``resolve_raw_dir()`` and say so.
    """
    root = resolve_raw_dir(cli_raw_root, cli_memory_dir, cli_vault_root)
    if not root.is_dir():
        raise RawRootUnavailable(
            f"raw root does not exist: {root} -- set {RAW_ROOT_ENV} (or pass "
            f"--raw-root) to where the raw session corpus actually lives. An "
            f"empty raw root is fine; a missing one is a misconfiguration."
        )
    return root

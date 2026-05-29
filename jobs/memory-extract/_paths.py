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
  2. OBSIDIAN_VAULT_ROOT environment variable
  3. ``--vault-root`` CLI flag (callers pass args.vault_root explicitly via
     ``resolve_vault_root(cli_value)``)

Usage:
    from _paths import resolve_vault_root, resolve_memory_dir

    vault = resolve_vault_root(args.vault_root)       # env-aware
    memory = resolve_memory_dir(args.memory_dir,
                                args.vault_root)      # cortex-memory dir
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

VAULT_ROOT_ENV = "OBSIDIAN_VAULT_ROOT"
LINUX_DEFAULT = Path("/data/sync/obsidian/My Vault")
WINDOWS_DEFAULT = Path("C:/sync/obsidian/My Vault")
MEMORY_SUBDIR = ("8.Cortex", "Memory")


def platform_default_vault_root() -> Path:
    """Return platform-specific default vault-root (no env-var lookup)."""
    if sys.platform.startswith("linux"):
        return LINUX_DEFAULT
    return WINDOWS_DEFAULT


def default_vault_root() -> Path:
    """Return default vault-root: ``OBSIDIAN_VAULT_ROOT`` env or platform default."""
    env = os.environ.get(VAULT_ROOT_ENV)
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

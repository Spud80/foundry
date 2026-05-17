#!/usr/bin/env python3
"""Smoke-test for Phase 1000 audit.

Covers PLAN-foundry Phase 1000 task 1000-6 scenarios:
  1. Empty vault -> tier=silent, no findings, no Telegram
  2. Dedup-collision detected (check 1)
  3. Schema violation: missing hard-required on pre_classified=full (check 2)
  4. Schema violation: legacy Status-emoji in tags (check 2)
  5. Broken source_session on ai-session entry -> tier=kritisk (check 3)
  6. Broken links/related -> tier=lav (check 3)
  7. Forbidden Status-emoji in tags -> tier=hoy (check 5)
  8. Forbidden Growth-emoji in tags -> tier=hoy (check 5)
  9. Missing required_field: creator on capture=book + pre_classified=full (check 6)
 10. Idempotency: re-run produces structurally same report (modulo timestamps)
 11. Sample skipped when fewer than 5 fresh entries (check 4)
 12. Tier aggregation: kritisk overrides hoy
 13. Report atomic-write via tempfile pattern
 14. Heartbeat-state updates with consecutive counters
 15. Vault-unavailable returns exit code 124
 16. Excluded paths (_test/, archive/, templates/, _underscore-prefix) skipped
 17. Sync-conflict files skipped

Run from repo root:
  python jobs/audit/_test/smoke_phase_1000.py

Does NOT call claude -p (check 4 is invoked with --skip-llm in smoke-test).
End-to-end against real claude -p verified on foundry CT post-deploy.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make jobs/audit importable.
HERE = Path(__file__).resolve().parent
JOB_DIR = HERE.parent
sys.path.insert(0, str(JOB_DIR))

import audit  # noqa: E402


# ---------- Helpers ----------

def make_entry(
    path: Path,
    *,
    title: str = "Test entry",
    created: str | None = None,
    updated: str | None = None,
    capture: str = "note",
    intent=None,
    status: str | None = None,
    source: str = "ai-session",
    source_session: str | None = None,
    user_id: str = "carl",
    scope: str = "personal",
    dedup_hash: str = "abc123",
    processing_state: str = "completed",
    pre_classified: str = "full",
    tags: list | None = None,
    topics: list | None = None,
    creator: str | None = None,
    due: str | None = None,
    extra: dict | None = None,
    body: str = "Body text.\n",
):
    """Write a synthetic vault entry."""
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    fm = {
        "title": title,
        "created": created or now_iso,
        "updated": updated or now_iso,
        "capture": capture,
        "intent": intent,
        "source": source,
        "source_session": source_session or "[[raw/2026-05-13/test-sid-001]]",
        "user_id": user_id,
        "scope": scope,
        "dedup_hash": dedup_hash,
        "processing_state": processing_state,
        "pre_classified": pre_classified,
        "tags": tags if tags is not None else ["📥", "💭"],
        "topics": topics if topics is not None else [],
    }
    if status is not None:
        fm["status"] = status
    if creator is not None:
        fm["creator"] = creator
    if due is not None:
        fm["due"] = due
    if extra:
        fm.update(extra)

    # Strip None values to keep clean YAML (None becomes 'null' which is fine,
    # but for entries that should have a field MISSING, callers pop manually).
    import yaml
    fm_yaml = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm_yaml}---\n{body}", encoding="utf-8")


def make_raw_session(vault_root: Path, date: str, sid: str) -> Path:
    """Create a stub raw-session file for source_session resolution."""
    p = vault_root / "8.Cortex" / "Memory" / "raw" / date / f"{sid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# raw session\n", encoding="utf-8")
    return p


def make_vault(tmp: Path) -> tuple[Path, Path, Path]:
    """Return (vault_root, report_dir, state_file)."""
    vault_root = tmp / "vault"
    (vault_root / "1.Inbox").mkdir(parents=True)
    (vault_root / "2.Resources" / "Notes" / "Books").mkdir(parents=True)
    (vault_root / "2.Resources" / "Notes" / "Misc").mkdir(parents=True)
    (vault_root / "2.Resources" / "Notes" / "Annotations").mkdir(parents=True)
    (vault_root / "8.Cortex" / "Memory" / "raw" / "2026-05-13").mkdir(parents=True)
    (vault_root / "5.Utility" / "Pipeline" / "Audit-Reports").mkdir(parents=True)
    report_dir = vault_root / "5.Utility" / "Pipeline" / "Audit-Reports"
    state_file = tmp / "audit-state.json"
    return vault_root, report_dir, state_file


def run_audit(vault_root: Path, report_dir: Path, state_file: Path, dry_run: bool = False) -> int:
    """Invoke audit.main() via sys.argv override."""
    argv_backup = sys.argv
    sys.argv = [
        "audit.py",
        "--vault-root", str(vault_root),
        "--report-dir", str(report_dir),
        "--state-file", str(state_file),
        "--skip-llm",
    ]
    if dry_run:
        sys.argv.append("--dry-run")
    try:
        return audit.main()
    finally:
        sys.argv = argv_backup


# ---------- Scenarios ----------

def scenario_empty_vault():
    """Empty vault: tier=silent, exit 0, no findings."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0, f"empty-vault: rc={rc}"
        reports = list(report_dir.glob("*.md"))
        assert len(reports) == 1, f"empty-vault: expected 1 report, got {len(reports)}"
        report = reports[0].read_text(encoding="utf-8")
        assert "tier: silent" in report, "empty-vault: tier should be silent"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["last_tier"] == "silent"
        assert state["consecutive_silent_runs"] == 1
    print("  [OK] scenario_empty_vault")


def scenario_dedup_collision():
    """Two entries with same dedup_hash -> check 1 detects collision, tier=hoy."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        make_entry(vault_root / "2.Resources" / "Notes" / "Misc" / "first.md", dedup_hash="collision-hash-aaaaaa")
        make_entry(vault_root / "2.Resources" / "Notes" / "Misc" / "second.md", dedup_hash="collision-hash-aaaaaa")
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "dedup_collisions: 1" in report
        assert "tier: hoy" in report or "tier: kritisk" in report
        assert "collision-hash" in report
    print("  [OK] scenario_dedup_collision")


def scenario_schema_missing_hard_required():
    """pre_classified=full but missing 'capture' field -> check 2 detects."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        # Write entry manually without 'capture' field.
        path = vault_root / "2.Resources" / "Notes" / "Misc" / "missing-capture.md"
        path.write_text(
            "---\n"
            "title: No-capture entry\n"
            "created: 2026-05-13T10:00:00+02:00\n"
            "updated: 2026-05-13T10:00:00+02:00\n"
            "intent: null\n"
            "source: ai-session\n"
            "source_session: '[[raw/2026-05-13/test-sid-001]]'\n"
            "user_id: carl\n"
            "scope: personal\n"
            "dedup_hash: hash-missing-cap\n"
            "processing_state: completed\n"
            "pre_classified: full\n"
            "tags: [📥, 💭]\n"
            "---\nBody.\n",
            encoding="utf-8",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "missing_hard_required" in report
        assert "capture" in report
    print("  [OK] scenario_schema_missing_hard_required")


def scenario_legacy_status_emoji():
    """tags contains 🟩 (Status-emoji) -> check 2 + check 5 both detect."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Misc" / "legacy-emoji.md",
            tags=["📥", "💭", "🟩"],
            dedup_hash="hash-legacy-emoji",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "legacy_status_emoji" in report or "forbidden_status_emoji" in report
        assert "tier: hoy" in report
    print("  [OK] scenario_legacy_status_emoji")


def scenario_broken_source_session_kritisk():
    """ai-session entry with source_session pointing to nonexistent raw -> tier=kritisk."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        # Do NOT create raw-session file; source_session will resolve to nothing.
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Annotations" / "ghost-link.md",
            source_session="[[raw/2026-05-13/missing-sid-999]]",
            dedup_hash="hash-ghost",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "tier: kritisk" in report, f"expected kritisk in report:\n{report[:1500]}"
        assert "broken_source_session: 1" in report
    print("  [OK] scenario_broken_source_session_kritisk")


def scenario_fallback_uuid_skipped():
    """ai-session entry with session_id_source=fallback-uuid -> source_session
    wikilink resolution skipped (synthetic UUID, no raw file expected to exist).
    See capture-vocabulary.md Pipeline-instrumentation fields + audit-pass-spec.md
    check 3 fallback-uuid exception."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        # No raw-session file created; the wikilink intentionally does not resolve.
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Annotations" / "headless-save.md",
            source_session="[[raw/2026-05-13/synthetic-uuid-from-headless]]",
            dedup_hash="hash-fallback-uuid",
            extra={"session_id_source": "fallback-uuid"},
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        # source_session wikilink-check skipped -> 0 broken, finding does not drive kritisk-tier.
        assert "broken_source_session: 0" in report, (
            f"expected fallback-uuid to skip wikilink check:\n{report[:1500]}"
        )
        # session_id_source should be recognised as a known field (no schema-violation).
        assert "unknown field 'session_id_source'" not in report, (
            f"session_id_source should be in KNOWN_FIELDS:\n{report[:1500]}"
        )
    print("  [OK] scenario_fallback_uuid_skipped")


def scenario_manual_note_bypass():
    """Manually-curated notes (no full pipeline-marker signature) must be
    skipped by checks 2/3/5/6. Simulates typical Books/Quotes/Ideas-folder
    notes that user creates by hand and don't go through /save-pipeline.

    See audit-pass-spec.md "Pipeline-emitted vs manual notes" - gate is
    presence of ALL of: dedup_hash, pre_classified, user_id, scope."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)

        # Write a manual book-note directly (bypassing make_entry which auto-
        # sets all pipeline-markers). Includes fields a user template might
        # legitimately add (source, processing_state) but missing the full
        # pipeline signature (no dedup_hash, no pre_classified, no user_id,
        # no scope). Also has fields outside KNOWN_FIELDS (author, published)
        # that would normally trigger unknown_field findings.
        book_path = vault_root / "2.Resources" / "Notes" / "Books" / "Manual Book.md"
        book_path.parent.mkdir(parents=True, exist_ok=True)
        book_path.write_text(
            "---\n"
            "title: Manual Book\n"
            "source: web\n"
            "processing_state: completed\n"
            "author: Some Author\n"
            "published: 2020\n"
            "tags:\n"
            "  - 📥\n"
            "  - 📖\n"
            "---\n"
            "Body text.\n",
            encoding="utf-8",
        )

        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")

        # Check 2: no schema-violations on manual note (no missing_baseline,
        # no unknown_field for author/published).
        assert "schema_violations: 0" in report, (
            f"manual note should not be schema-audited:\n{report[:2000]}"
        )
        # Check 3: no broken_links (manual notes have no source_session).
        assert "broken_source_session: 0" in report
        # Check 5: no tag violations (📖 is fine on manual Book, missing 💭 is fine).
        assert "tag_violations: 0" in report, (
            f"manual note should not get tag-violations:\n{report[:2000]}"
        )
        # Overall tier should be silent (no findings drive any severity).
        assert "tier: silent" in report, (
            f"manual note alone should produce silent tier:\n{report[:1500]}"
        )
    print("  [OK] scenario_manual_note_bypass")


def scenario_phase_100_skeleton_audited():
    """Phase 100 skeleton entries (pre_classified: none) from /save are still
    pipeline-emitted: they carry all 4 markers (user_id + scope + dedup_hash +
    pre_classified) per save.py:471-476. Audit gate MUST treat them as pipeline-
    entries and run checks 2/3/5/6 (which all pass for valid Phase 100 entries
    because MINIMUM_BASELINE is satisfied).

    Regression coverage: if a future refactor of save.py accidentally drops
    user_id/scope from the skeleton-path, this test would fail. Conversely if
    a future audit refactor tightens the gate beyond multi-field, Phase 100
    entries would silently disappear from audit coverage."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "phase100-sid")

        # Phase 100 skeleton: classifier did not run. Only minimum-baseline
        # fields are set + all 4 pipeline-markers. No capture, no intent.
        # make_entry default has capture="note", intent=None - that matches
        # what /save Phase 100 writes (skeleton-derived title, no classification).
        make_entry(
            vault_root / "1.Inbox" / "ai-capture-phase100-sid-20260513T120000.md",
            pre_classified="none",
            source_session="[[raw/2026-05-13/phase100-sid]]",
            dedup_hash="hash-phase100",
            processing_state="new",
        )

        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")

        # Phase 100 entry is a valid pipeline-citizen - no findings expected.
        assert "schema_violations: 0" in report, (
            f"Phase 100 skeleton should pass MINIMUM_BASELINE check:\n{report[:2000]}"
        )
        assert "broken_source_session: 0" in report
        assert "tag_violations: 0" in report
        assert "missing_required_field: 0" in report
        assert "tier: silent" in report, (
            f"Phase 100 alone should produce silent tier:\n{report[:1500]}"
        )

        # Sanity: also verify the pipeline-gate helper itself classifies
        # this exact frontmatter as pipeline-emitted (defence-in-depth).
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from audit import is_pipeline_entry, load_frontmatter  # noqa
        fm, _ = load_frontmatter(
            vault_root / "1.Inbox" / "ai-capture-phase100-sid-20260513T120000.md"
        )
        assert is_pipeline_entry(fm), (
            "Phase 100 frontmatter must pass is_pipeline_entry gate; "
            "if this fails, save.py skeleton has stopped writing one of "
            "{dedup_hash, pre_classified, user_id, scope}"
        )
    print("  [OK] scenario_phase_100_skeleton_audited")


def scenario_broken_links_lav():
    """links[] array with broken target -> tier=lav."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Misc" / "broken-link.md",
            extra={"links": ["[[nonexistent-note]]"]},
            dedup_hash="hash-broken-link",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "broken_links: 1" in report
        assert "tier: lav" in report
    print("  [OK] scenario_broken_links_lav")


def scenario_forbidden_status_emoji_check5():
    """Status-emoji 🟥 in tags -> check 5 fires hoy severity."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Misc" / "status-emoji.md",
            tags=["📥", "💭", "🟥"],
            dedup_hash="hash-status-emoji",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "forbidden_status_emoji" in report
    print("  [OK] scenario_forbidden_status_emoji_check5")


def scenario_forbidden_growth_emoji_check5():
    """Growth-emoji 🌱 in tags -> check 5 fires hoy severity."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Misc" / "growth-emoji.md",
            tags=["📥", "💭", "🌱"],
            dedup_hash="hash-growth-emoji",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "forbidden_growth_emoji" in report
    print("  [OK] scenario_forbidden_growth_emoji_check5")


def scenario_missing_creator():
    """capture=book + pre_classified=full but no creator -> check 6 fires."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Books" / "book-no-creator.md",
            capture="book",
            dedup_hash="hash-book-no-creator",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "missing_creator" in report
        assert "missing_required_field: 1" in report
    print("  [OK] scenario_missing_creator")


def scenario_idempotency_rerun():
    """Re-running on same vault produces structurally same report (modulo timestamps)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Misc" / "stable.md",
            dedup_hash="hash-stable",
        )
        run_audit(vault_root, report_dir, state_file)
        run1 = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        # Re-run.
        run_audit(vault_root, report_dir, state_file)
        run2 = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")

        # Strip volatile fields and compare structure.
        import re as _re
        def normalize(s):
            s = _re.sub(r"audit_date:.*", "audit_date: <ts>", s)
            s = _re.sub(r"duration_seconds:.*", "duration_seconds: <dur>", s)
            return s
        assert normalize(run1) == normalize(run2), "idempotency violated"
        # Verify single report file (no append).
        assert len(list(report_dir.glob("*.md"))) == 1
    print("  [OK] scenario_idempotency_rerun")


def scenario_sampling_topics_demoted():
    """Topics-only divergence (Jaccard < 0.5) must NOT drive any severity tier.
    Structural axes (capture+intent) match -> structural_divergence_rate=0,
    topics_divergence_rate>0 -> tier=silent (topics is informational only)."""
    # We don't actually invoke claude -p here; we unit-test the rate-aggregation
    # logic by constructing a synthetic sampling-result and feeding it through
    # the tier-aggregation directly.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from audit import aggregate_tier  # noqa
    findings = {
        "schema": {"by_severity": {"kritisk": 0, "hoy": 0, "lav": 0}},
        "wikilinks": {"by_severity": {"kritisk": 0, "hoy": 0, "lav": 0}},
        "tags": {"by_severity": {"hoy": 0, "lav": 0}},
        "dedup": {"count": 0},
        "missing_required": {"count": 0},
        # Synthetic: 3/5 topics disagreements, 0/5 capture/intent disagreements.
        "sampling": {
            "skipped": False,
            "structural_divergence_rate": 0.0,
            "topics_divergence_rate": 0.6,
            "structural_above_threshold": False,
            "above_threshold": False,  # backwards-compat alias
            "divergence_rate": 0.0,
        },
    }
    tier = aggregate_tier(findings)
    assert tier == "silent", (
        f"topics-only divergence should NOT drive tier; got {tier!r}"
    )
    print("  [OK] scenario_sampling_topics_demoted")


def scenario_sampling_structural_drives_tier():
    """capture+intent disagreement on 2/5 samples -> structural rate 4/10=0.40
    -> above 0.20 threshold -> hoy tier."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from audit import aggregate_tier  # noqa
    findings = {
        "schema": {"by_severity": {"kritisk": 0, "hoy": 0, "lav": 0}},
        "wikilinks": {"by_severity": {"kritisk": 0, "hoy": 0, "lav": 0}},
        "tags": {"by_severity": {"hoy": 0, "lav": 0}},
        "dedup": {"count": 0},
        "missing_required": {"count": 0},
        "sampling": {
            "skipped": False,
            "structural_divergence_rate": 0.40,
            "topics_divergence_rate": 0.0,
            "structural_above_threshold": True,
            "above_threshold": True,  # backwards-compat alias
            "divergence_rate": 0.40,
        },
    }
    tier = aggregate_tier(findings)
    assert tier == "hoy", (
        f"structural divergence above threshold should drive hoy; got {tier!r}"
    )
    print("  [OK] scenario_sampling_structural_drives_tier")


def scenario_sampling_skipped():
    """Fewer than 5 fresh ai-session entries -> check 4 skipped."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        # Just 2 fresh entries -> below SAMPLING_MIN_FRESH=5
        for i in range(2):
            make_entry(
                vault_root / "2.Resources" / "Notes" / "Misc" / f"fresh-{i}.md",
                dedup_hash=f"hash-fresh-{i}",
            )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "classification_check_skipped: true" in report
    print("  [OK] scenario_sampling_skipped")


def scenario_tier_aggregation_kritisk():
    """Mix of kritisk + hoy + lav findings -> overall tier=kritisk."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        # No raw-session created -> source_session will be broken (kritisk).
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Misc" / "mixed-1.md",
            source_session="[[raw/2026-05-13/ghost-sid]]",
            tags=["📥", "💭", "🟥"],  # tag-violation (hoy)
            extra={"links": ["[[nonexistent]]"]},  # broken link (lav)
            dedup_hash="hash-mixed-1",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "tier: kritisk" in report, f"expected kritisk:\n{report[:1500]}"
    print("  [OK] scenario_tier_aggregation_kritisk")


def scenario_atomic_write_tempfile():
    """Verify atomic_write does not leave .tmp-audit-* tempfiles on success."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        run_audit(vault_root, report_dir, state_file)
        tempfiles = list(report_dir.glob(".tmp-audit-*"))
        assert not tempfiles, f"tempfiles left behind: {tempfiles}"
    print("  [OK] scenario_atomic_write_tempfile")


def scenario_heartbeat_consecutive_counters():
    """consecutive_silent_runs increments on consecutive silent runs."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        run_audit(vault_root, report_dir, state_file)
        s1 = json.loads(state_file.read_text(encoding="utf-8"))
        run_audit(vault_root, report_dir, state_file)
        s2 = json.loads(state_file.read_text(encoding="utf-8"))
        assert s1["consecutive_silent_runs"] == 1
        assert s2["consecutive_silent_runs"] == 2
        assert s1["consecutive_failed_runs"] == 0
        assert s2["consecutive_failed_runs"] == 0
    print("  [OK] scenario_heartbeat_consecutive_counters")


def scenario_vault_unavailable():
    """Nonexistent vault_root -> exit 124."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ghost_vault = tmp / "no-such-vault"
        report_dir = tmp / "reports"
        state_file = tmp / "state.json"
        rc = run_audit(ghost_vault, report_dir, state_file)
        assert rc == 124, f"vault-unavailable: rc={rc}"
    print("  [OK] scenario_vault_unavailable")


def scenario_excluded_paths():
    """_test/, archive/, templates/, _underscore-prefix files are skipped."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        # Excluded by path.
        (vault_root / "2.Resources" / "Notes" / "_test").mkdir()
        make_entry(
            vault_root / "2.Resources" / "Notes" / "_test" / "test-entry.md",
            dedup_hash="hash-excluded-test",
            tags=["📥", "💭", "🟥"],  # would otherwise fire
        )
        # Excluded by underscore-prefix.
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Misc" / "_underscore.md",
            dedup_hash="hash-excluded-underscore",
            tags=["📥", "💭", "🟥"],
        )
        # Non-excluded entry to keep the vault non-empty and verify scan ran.
        make_entry(
            vault_root / "2.Resources" / "Notes" / "Misc" / "regular.md",
            dedup_hash="hash-regular",
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        # Should NOT find the tag-violations in excluded files.
        assert "tag_violations: 0" in report, f"excluded paths leaked:\n{report[:2000]}"
    print("  [OK] scenario_excluded_paths")


def scenario_sync_conflict_files_skipped():
    """*.sync-conflict-* files are excluded."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root, report_dir, state_file = make_vault(tmp)
        make_raw_session(vault_root, "2026-05-13", "test-sid-001")
        # Conflict-file content would otherwise fire tag-violation.
        conflict = vault_root / "2.Resources" / "Notes" / "Misc" / "regular.sync-conflict-20260513-AAA.md"
        make_entry(
            conflict,
            dedup_hash="hash-conflict",
            tags=["📥", "💭", "🟥"],
        )
        rc = run_audit(vault_root, report_dir, state_file)
        assert rc == 0
        report = (report_dir / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "tag_violations: 0" in report, f"sync-conflict leaked:\n{report[:2000]}"
    print("  [OK] scenario_sync_conflict_files_skipped")


# ---------- Runner ----------

SCENARIOS = [
    scenario_empty_vault,
    scenario_dedup_collision,
    scenario_schema_missing_hard_required,
    scenario_legacy_status_emoji,
    scenario_broken_source_session_kritisk,
    scenario_fallback_uuid_skipped,
    scenario_manual_note_bypass,
    scenario_phase_100_skeleton_audited,
    scenario_broken_links_lav,
    scenario_forbidden_status_emoji_check5,
    scenario_forbidden_growth_emoji_check5,
    scenario_missing_creator,
    scenario_idempotency_rerun,
    scenario_sampling_skipped,
    scenario_sampling_topics_demoted,
    scenario_sampling_structural_drives_tier,
    scenario_tier_aggregation_kritisk,
    scenario_atomic_write_tempfile,
    scenario_heartbeat_consecutive_counters,
    scenario_vault_unavailable,
    scenario_excluded_paths,
    scenario_sync_conflict_files_skipped,
]


def main():
    print(f"Running {len(SCENARIOS)} smoke-test scenarios for Phase 1000 audit...")
    failures = []
    for fn in SCENARIOS:
        try:
            fn()
        except AssertionError as e:
            failures.append((fn.__name__, str(e)))
            print(f"  [FAIL] {fn.__name__}: {e}")
        except Exception as e:
            import traceback as tb
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))
            print(f"  [ERROR] {fn.__name__}: {e}")
            tb.print_exc()
    print()
    if failures:
        print(f"FAILED: {len(failures)} / {len(SCENARIOS)}")
        for name, err in failures:
            print(f"  {name}: {err}")
        sys.exit(1)
    print(f"PASS: {len(SCENARIOS)} / {len(SCENARIOS)}")
    sys.exit(0)


if __name__ == "__main__":
    main()

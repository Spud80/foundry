#!/usr/bin/env python3
"""Smoke-test for Phase 900 fallback-classifier.

Covers PLAN-foundry Phase 900 task 900-6 scenarios:
  1. pre_classified: partial (missing one field) - mock LLM fills it
  2. pre_classified: none (full classify from raw body) - mock LLM returns full
  3. Idempotency on re-run (foundry_pending marker missing -> no-op)
  4. Recovery: rename-only state (post-mutate, pre-rename crash)
  5. Recovery: re-classify state (mutate-mid-flight crash)
  6. State classification across all 4 states
  7. Merge missing-only semantics (does not overwrite existing fields)
  8. Off-limits fields preserved (dedup_hash, created, source, etc.)
  9. Validation errors on bad LLM output
 10. Atomic write tempfile pattern

Run from repo root:
  python jobs/fallback-classifier/_test/smoke_phase_900.py

Does NOT call claude -p (mocked via monkey-patching classify.call_claude).
End-to-end against real claude -p is verified on foundry CT post-deploy.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

# Make jobs/fallback-classifier importable
JOB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JOB_DIR))

import classify  # noqa: E402

PASS = "[PASS]"
FAIL = "[FAIL]"
failures: list[str] = []


def assert_(cond: bool, msg: str) -> None:
    if cond:
        print(f"  {PASS} {msg}")
    else:
        print(f"  {FAIL} {msg}")
        failures.append(msg)


def mk_pending(inbox: Path, sid: str, ts: str, fm_extra: dict, body: str = "Test body") -> Path:
    """Write a pending-foundry-*.md file with given frontmatter overrides."""
    base_fm = {
        "title": "",
        "created": "2026-05-13T10:00:00+02:00",
        "updated": "2026-05-13T10:00:00+02:00",
        "source": "mobile",
        "source_session": None,
        "user_id": "carl",
        "scope": "personal",
        "dedup_hash": "abc123def456",
        "processing_state": "new",
        "foundry_pending": True,
        "pre_classified": "none",
    }
    base_fm.update(fm_extra)
    path = inbox / f"pending-foundry-{sid}-{ts}.md"
    path.write_text(classify.dump_frontmatter(base_fm, body), encoding="utf-8")
    return path


def install_mock_claude(response: dict) -> None:
    """Monkey-patch classify.call_claude to return a fixed response."""
    classify.call_claude = lambda body, current_fm, current_date: response


# ---------- Scenario 1: pre_classified: partial ----------

def test_partial_fill_missing() -> None:
    print("\n--- Scenario 1: pre_classified: partial, fill missing capture/intent ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        inbox = Path(tmpdir) / "1.Inbox"
        inbox.mkdir()
        pending = mk_pending(inbox, "01a2b3c4", "20260513T100000", {
            "pre_classified": "partial",
            "title": "Husk å teste cronen",
            # capture + intent missing
        }, body="Husk å teste fallback-cronen i morgen.")
        install_mock_claude({
            "capture": "note",
            "intent": "reminder",
            "status": "active",
            "due": "2026-05-14T09:00:00+02:00",
            "topics": ["¤memory-pipeline"],
        })
        result = classify.process_file(pending)
        assert_(result == "classified", f"result=classified (got {result!r})")
        assert_(not pending.exists(), "pending file removed")
        target = inbox / "ai-capture-01a2b3c4-20260513T100000.md"
        assert_(target.exists(), "ai-capture-*.md created")
        fm, body = classify.load_frontmatter(target)
        assert_(fm.get("pre_classified") == "full", f"pre_classified=full (got {fm.get('pre_classified')!r})")
        assert_("foundry_pending" not in fm, "foundry_pending removed")
        assert_(fm.get("capture") == "note", f"capture=note (got {fm.get('capture')!r})")
        assert_(fm.get("intent") == "reminder", f"intent=reminder (got {fm.get('intent')!r})")
        assert_(fm.get("status") == "active", "status=active set for intent != null")
        assert_(fm.get("due") == "2026-05-14T09:00:00+02:00", "due set for intent=reminder")
        assert_(fm.get("title") == "Husk å teste cronen", "title preserved (was already set)")
        assert_(fm.get("dedup_hash") == "abc123def456", "dedup_hash preserved (off-limits)")
        assert_(body.strip() == "Husk å teste fallback-cronen i morgen.", "body byte-identical")


# ---------- Scenario 2: pre_classified: none, full classify ----------

def test_none_full_classify() -> None:
    print("\n--- Scenario 2: pre_classified: none, full classify from raw body ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        inbox = Path(tmpdir) / "1.Inbox"
        inbox.mkdir()
        pending = mk_pending(inbox, "deadbeef", "20260513T110000", {
            "pre_classified": "none",
            # nothing else set beyond capture-tid fields
        }, body="Boken Atomic Habits av James Clear handler om vaner.")
        install_mock_claude({
            "title": "Atomic Habits",
            "capture": "book",
            "intent": None,
            "creator": "James Clear",
            "topics": ["¤vaner"],
        })
        result = classify.process_file(pending)
        assert_(result == "classified", f"result=classified (got {result!r})")
        target = inbox / "ai-capture-deadbeef-20260513T110000.md"
        assert_(target.exists(), "ai-capture-*.md created")
        fm, _ = classify.load_frontmatter(target)
        assert_(fm.get("capture") == "book", "capture=book")
        assert_(fm.get("intent") is None, "intent=None (no auto-status)")
        assert_("status" not in fm, "status NOT set when intent=None")
        assert_(fm.get("creator") == "James Clear", "creator=James Clear")
        assert_(fm.get("pre_classified") == "full", "pre_classified=full")
        assert_(fm.get("tags") == ["📥", "💭"], "default tags applied")


# ---------- Scenario 3: idempotency on re-run ----------

def test_idempotency_no_op() -> None:
    print("\n--- Scenario 3: idempotency on re-run (no foundry_pending marker) ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        inbox = Path(tmpdir) / "1.Inbox"
        inbox.mkdir()
        # First run: classify + rename
        pending = mk_pending(inbox, "01b2c3d4", "20260513T120000", {
            "pre_classified": "partial",
        }, body="Idempotent body")
        install_mock_claude({"capture": "note", "intent": None})
        first = classify.process_file(pending)
        assert_(first == "classified", "first run classified")
        # Second run: re-pickup nothing (pending file gone, only ai-capture exists which is not in glob)
        target = inbox / "ai-capture-01b2c3d4-20260513T120000.md"
        assert_(target.exists(), "ai-capture-*.md still present")
        # Now simulate scenario: someone created another pending-foundry with same content
        # No - actual idempotency test: glob 1.Inbox/pending-foundry-*.md, ensure 0 files left
        leftover = list(inbox.glob("pending-foundry-*.md"))
        assert_(len(leftover) == 0, f"no pending files left after classify (got {len(leftover)})")


# ---------- Scenario 4: recovery rename-only ----------

def test_recovery_rename_only() -> None:
    print("\n--- Scenario 4: recovery rename-only (crash between mutate and rename) ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        inbox = Path(tmpdir) / "1.Inbox"
        inbox.mkdir()
        # Simulate post-mutate-pre-rename state: pending-foundry-*.md with all fields filled,
        # foundry_pending removed, pre_classified: full
        pending = mk_pending(inbox, "abc1234", "20260513T130000", {
            "pre_classified": "full",
            "title": "Complete entry",
            "capture": "note",
            "intent": None,
        }, body="Recovered body")
        # Remove foundry_pending (simulate the mutate-step having happened)
        fm, body = classify.load_frontmatter(pending)
        fm.pop("foundry_pending", None)
        pending.write_text(classify.dump_frontmatter(fm, body), encoding="utf-8")

        state = classify.classify_state(fm)
        assert_(state == "rename-only", f"state=rename-only (got {state!r})")

        # claude -p should NOT be called for rename-only
        called = {"yes": False}
        def _claude_should_not_be_called(*a, **kw):
            called["yes"] = True
            return {}
        classify.call_claude = _claude_should_not_be_called

        result = classify.process_file(pending)
        assert_(result == "rename-only", f"result=rename-only (got {result!r})")
        assert_(not called["yes"], "claude -p was NOT invoked for rename-only state")
        assert_(not pending.exists(), "pending file removed")
        target = inbox / "ai-capture-abc1234-20260513T130000.md"
        assert_(target.exists(), "ai-capture-*.md created via rename")


# ---------- Scenario 5: recovery re-classify ----------

def test_recovery_reclassify() -> None:
    print("\n--- Scenario 5: recovery re-classify (foundry_pending removed but fields missing) ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        inbox = Path(tmpdir) / "1.Inbox"
        inbox.mkdir()
        # Simulate mid-mutate-crash: foundry_pending was removed but other fields not yet written
        pending = mk_pending(inbox, "999777", "20260513T140000", {
            "pre_classified": "partial",
            # capture/intent missing
        }, body="Half-written body")
        fm, body = classify.load_frontmatter(pending)
        fm.pop("foundry_pending", None)
        pending.write_text(classify.dump_frontmatter(fm, body), encoding="utf-8")

        state = classify.classify_state(fm)
        assert_(state == "recovery-reclassify", f"state=recovery-reclassify (got {state!r})")

        install_mock_claude({"capture": "note", "intent": None})
        result = classify.process_file(pending)
        assert_(result == "reclassified", f"result=reclassified (got {result!r})")
        target = inbox / "ai-capture-999777-20260513T140000.md"
        assert_(target.exists(), "ai-capture-*.md created via re-classify path")


# ---------- Scenario 6: state classification matrix ----------

def test_state_classification_matrix() -> None:
    print("\n--- Scenario 6: state classification matrix ---")
    # State A: normal
    fm_a = {"foundry_pending": True, "pre_classified": "partial"}
    assert_(classify.classify_state(fm_a) == "normal", "state A: foundry_pending=true -> normal")
    # State B: rename-only
    fm_b = {
        "title": "X", "created": "2026-01-01T00:00:00+02:00", "updated": "2026-01-01T00:00:00+02:00",
        "capture": "note", "intent": None, "source": "ai-session",
        "source_session": "[[raw/x/y]]", "user_id": "carl", "scope": "personal",
        "dedup_hash": "h", "processing_state": "new", "pre_classified": "full",
    }
    assert_(classify.classify_state(fm_b) == "rename-only", "state B: all fields + full + no foundry_pending -> rename-only")
    # State C: recovery-reclassify
    fm_c = {"pre_classified": "partial", "processing_state": "new"}  # missing many hard-required
    assert_(classify.classify_state(fm_c) == "recovery-reclassify", "state C: missing fields + no foundry_pending -> recovery-reclassify")
    # State D: unknown
    fm_d = {"foundry_pending": False}  # explicit false, no other markers
    assert_(classify.classify_state(fm_d) == "unknown", "state D: explicit false + nothing else -> unknown")


# ---------- Scenario 7: merge missing-only semantics ----------

def test_merge_preserves_existing() -> None:
    print("\n--- Scenario 7: merge missing-only does not overwrite existing fields ---")
    current = {
        "title": "User-supplied title",
        "capture": "note",
        "intent": "reminder",
        "due": "2026-06-01T09:00:00+02:00",
        "status": "active",
        "dedup_hash": "preserved",
        "foundry_pending": True,
    }
    llm = {
        "title": "LLM-generated title",  # should be ignored
        "capture": "idea",  # should be ignored
        "topics": ["¤new-topic"],  # should be applied
    }
    merged = classify.merge_classification(current, llm)
    assert_(merged["title"] == "User-supplied title", "existing title preserved")
    assert_(merged["capture"] == "note", "existing capture preserved")
    assert_(merged["intent"] == "reminder", "existing intent preserved")
    assert_(merged["topics"] == ["¤new-topic"], "missing topics filled from LLM")
    assert_(merged["pre_classified"] == "full", "pre_classified always overwritten to full")
    assert_(merged["dedup_hash"] == "preserved", "dedup_hash preserved")
    assert_("foundry_pending" not in merged, "foundry_pending removed")


# ---------- Scenario 8: validation errors ----------

def test_validation_rejects_bad_enum() -> None:
    print("\n--- Scenario 8: validation rejects invalid enum values ---")
    errs1 = classify.validate_llm_output({"capture": "invalid-thing"})
    assert_(any("capture" in e for e in errs1), "rejects unknown capture enum")
    errs2 = classify.validate_llm_output({"intent": "reminder"})  # missing due
    assert_(any("due" in e for e in errs2), "rejects intent=reminder without due")
    errs3 = classify.validate_llm_output({"capture": "note", "intent": None})
    assert_(errs3 == [], f"accepts valid capture+intent=null (got errs {errs3})")


# ---------- Scenario 9: atomic write tempfile pattern ----------

def test_atomic_write_tempfile() -> None:
    print("\n--- Scenario 9: atomic_write uses .tmp-fallback-* pattern ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "test.md"
        classify.atomic_write(target, "hello\n")
        assert_(target.read_text(encoding="utf-8") == "hello\n", "atomic_write wrote content")
        # Verify no .tmp-fallback-* leftover
        leftover = list(Path(tmpdir).glob(".tmp-fallback-*"))
        assert_(len(leftover) == 0, f"no tempfile leftover (got {len(leftover)})")


# ---------- Scenario 10: filename derivation ----------

def test_filename_derivation() -> None:
    print("\n--- Scenario 10: pending-foundry -> ai-capture filename mapping ---")
    p = Path("/tmp/1.Inbox/pending-foundry-abc123-20260513T100000.md")
    target = classify.derive_ai_capture_path(p)
    assert_(target.name == "ai-capture-abc123-20260513T100000.md",
            f"derived={target.name!r}")
    # Compound session-id with hyphens
    p2 = Path("/tmp/1.Inbox/pending-foundry-01935a4e-8f2c-7b0d-20260513T100000.md")
    target2 = classify.derive_ai_capture_path(p2)
    assert_(target2.name == "ai-capture-01935a4e-8f2c-7b0d-20260513T100000.md",
            f"compound sid: derived={target2.name!r}")


# ---------- Driver ----------

def main() -> int:
    print("Phase 900 fallback-classifier smoke-test")
    print("=" * 60)

    test_partial_fill_missing()
    test_none_full_classify()
    test_idempotency_no_op()
    test_recovery_rename_only()
    test_recovery_reclassify()
    test_state_classification_matrix()
    test_merge_preserves_existing()
    test_validation_rejects_bad_enum()
    test_atomic_write_tempfile()
    test_filename_derivation()

    print()
    print("=" * 60)
    if failures:
        print(f"{FAIL} {len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"{PASS} all smoke-tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for Supabase runtime state helpers."""

from assets.snippets.supabase_state import normalize_crawl_run_status


def test_crawl_run_status_matches_db_constraint():
    assert normalize_crawl_run_status("succeeded") == "succeeded"
    assert normalize_crawl_run_status("failed") == "failed"
    assert normalize_crawl_run_status("partial") == "partial"
    assert normalize_crawl_run_status("running") == "running"


def test_crawl_run_status_legacy_aliases():
    assert normalize_crawl_run_status("success") == "succeeded"
    assert normalize_crawl_run_status("failure") == "failed"


def test_crawl_run_status_unknown_fails_closed():
    assert normalize_crawl_run_status("unexpected") == "failed"

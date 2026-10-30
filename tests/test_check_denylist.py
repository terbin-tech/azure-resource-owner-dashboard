from pathlib import Path

from scripts.check_denylist import find_hits, load_terms


def test_finds_term_case_insensitive_as_whole_word(tmp_path: Path):
    f = tmp_path / "a.md"
    f.write_text("owner: Student-A\nstudent-ab is someone else\n")
    assert find_hits([str(f)], ["student-a"]) == [(str(f), 1, "student-a")]


def test_load_terms_skips_comments_and_blanks(tmp_path: Path):
    p = tmp_path / ".denylist"
    p.write_text("# usernames\nStudent-A\n\n111122223333  # account\n")
    assert load_terms(p) == ["student-a", "111122223333"]


def test_missing_denylist_means_no_terms(tmp_path: Path):
    assert load_terms(tmp_path / "nope") == []


def test_binary_files_are_skipped(tmp_path: Path):
    f = tmp_path / "img.png"
    f.write_bytes(b"\x89PNG\xff\xfe")
    assert find_hits([str(f)], ["student-a"]) == []

import pytest

from dashboard.people import (
    Person,
    RosterError,
    load_roster_file,
    map_people,
    parse_roster,
)


def test_parse_roster_handles_bom_and_spaces():
    text = "\ufeffusername , primary_username,cohort\n student-a ,student-a, cohort-x\n\n"
    assert parse_roster(text) == {"student-a": ("student-a", "cohort-x")}


@pytest.mark.parametrize(
    "text,line",
    [
        # A lone carriage return, the old Mac line ending, is not a row break to the csv module.
        ("username,primary_username,cohort\rstudent-a,student-a,cohort-x\r", 1),
        ("username,primary_username,cohort\nstudent-a,student-a," + "x" * 140000 + "\n", 2),
    ],
    ids=["lone-cr", "field-over-the-csv-limit"],
)
def test_parse_roster_reports_unreadable_csv_by_line_only(text, line):
    with pytest.raises(RosterError) as err:
        parse_roster(text)
    assert str(err.value) == f"line {line}: not valid CSV"


def test_parse_roster_rejects_duplicates_without_naming_them():
    with pytest.raises(RosterError) as err:
        parse_roster(
            "username,primary_username,cohort\n"
            "student-a,student-a,cohort-x\n"
            "student-a,student-a,cohort-x\n"
        )
    assert "line 3" in str(err.value)
    assert "student-a" not in str(err.value)


def test_parse_roster_rejects_a_duplicate_that_differs_only_in_case():
    with pytest.raises(RosterError, match="line 3: duplicate username"):
        parse_roster(
            "username,primary_username,cohort\n"
            "student-a,student-a,cohort-x\n"
            "Student-A,Student-A,cohort-x\n"
        )


def test_parse_roster_requires_columns():
    with pytest.raises(RosterError, match="cohort"):
        parse_roster("username,primary_username\nstudent-a,student-a\n")


def test_parse_roster_skips_comment_lines():
    text = "username,primary_username,cohort\n#student-x,student-x,cohort-x\nstudent-a,,cohort-y\n"
    assert parse_roster(text) == {"student-a": ("student-a", "cohort-y")}


def test_missing_roster_file_is_none(tmp_path):
    assert load_roster_file(None) is None
    assert load_roster_file(str(tmp_path / "nope.csv")) is None


def test_roster_file_is_parsed(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text("username,primary_username,cohort\nstudent-a,student-a,cohort-x\n")
    assert load_roster_file(str(path)) == {"student-a": ("student-a", "cohort-x")}


@pytest.mark.parametrize(
    "text",
    [
        "username,primary_username,cohort,Max Mustermann\nstudent-a,student-a,cohort-x,x\n",
        "username,primary_username,cohort\nstudent-a,student-a,cohort-x,Max Mustermann\n",
    ],
    ids=["extra-header", "extra-value"],
)
def test_parse_roster_rejects_extra_columns_without_naming_them(text):
    with pytest.raises(RosterError) as err:
        parse_roster(text)
    assert "Max" not in str(err.value)


@pytest.mark.parametrize("primary", ["Max Mustermann", "a" * 65, "stüdent", "student-a."])
def test_parse_roster_rejects_a_primary_username_that_is_no_username(primary):
    with pytest.raises(RosterError) as err:
        parse_roster(f"username,primary_username,cohort\nstudent-a,{primary},cohort-x\n")
    assert "line 2" in str(err.value)
    assert primary not in str(err.value)


@pytest.mark.parametrize("primary", ["a", "a" * 64, "a" * 63 + "_", "st.udent-a", "o'neil~#!^"])
def test_parse_roster_accepts_a_sign_in_prefix_as_primary_username(primary):
    text = f"username,primary_username,cohort\nstudent-a,{primary},cohort-x\n"
    assert parse_roster(text)["student-a"] == (primary, "cohort-x")


def test_primary_username_may_name_another_roster_row():
    text = "username,primary_username,cohort\nlab user,lab user,cohort-x\nstudent-a,Lab User,cohort-x\n"
    assert parse_roster(text)["student-a"] == ("Lab User", "cohort-x")


def test_roster_groups_test_users_under_the_primary_card():
    roster = {"student-a-test": ("student-a", "cohort-x")}
    assert map_people(roster, ["student-a-test"])["student-a-test"].card == "student-a"


def test_people_cover_roster_names_and_owners():
    roster = {"gone-user": ("student-a", "cohort-x")}
    people = map_people(roster, ["gone-user", "ex-student"])
    assert people["gone-user"] == Person("gone-user", "student-a", "cohort-x")
    assert people["ex-student"] == Person("ex-student", "ex-student", None)


def test_roster_matches_owners_case_insensitively():
    roster = {"student-a": ("Student-A", "cohort-x")}
    people = map_people(roster, ["student-a"])
    assert people == {"student-a": Person("student-a", "Student-A", "cohort-x")}


def test_owner_without_a_roster_has_no_cohort():
    assert map_people(None, ["student-a"]) == {"student-a": Person("student-a", "student-a", None)}


def test_empty_owner_names_are_skipped():
    assert map_people(None, ["", "student-a"]).keys() == {"student-a"}


def test_two_spellings_share_one_person():
    people = map_people(None, ["student-a", "Student-A"])
    assert people == {"student-a": Person("student-a", "Student-A", None)}


def test_card_spellings_that_differ_only_in_case_are_one_card():
    roster = parse_roster(
        "username,primary_username,cohort\nlab user,lab user,cohort-x\nstudent-a,Lab User,cohort-x\n"
    )
    people = map_people(roster, [])
    assert {p.card for p in people.values()} == {"Lab User"}
    assert set(people) == {"lab user", "student-a"}

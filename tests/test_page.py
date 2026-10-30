import base64
import hashlib
import re
from datetime import UTC, datetime

from dashboard.attribution import Owner
from dashboard.config import Config
from dashboard.model import AKS, DISK, NETWORK_WATCHER, PUBLIC_IP, VM, Group
from dashboard.pipeline import Card, Row
from dashboard.render import (
    CSS,
    DELETE_GROUP_TEXT,
    FOOTER,
    JS,
    NEW_TAB,
    NODE_GROUP_TEXT,
    SHARED_GROUP_TEXT,
    help_line,
    portal_link,
    render_page,
)
from tests.helpers import SUB, grp, make_report, make_row, res, rid

CFG = Config()
DEALLOCATED = {"extended": {"instanceView": {"powerState": {"code": "PowerState/deallocated"}}}}
RUNNING = {"extended": {"instanceView": {"powerState": {"code": "PowerState/running"}}}}


def _sha(text):
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def _vm(name="vm-1", properties=DEALLOCATED, **kw):
    return res(VM, name, properties=properties, **kw)


def _a(url, text):
    return f'<a href="{url}" rel="noopener noreferrer" target="_blank">{text}{NEW_TAB}</a>'


def _name(r):
    """A resource name cell's link markup."""
    return _a(f"https://portal.azure.com/#resource{r.id}", r.name)


def _heading(name, group_id=None):
    """A group heading's link markup."""
    group_id = group_id or grp(name).id
    return f"<h4>{_a(f'https://portal.azure.com/#@/resource{group_id}', name)}</h4>"


def test_portal_links_only_for_valid_ids():
    vm = rid("rg-a", "Microsoft.Compute/virtualMachines", "vm-1")
    assert portal_link(vm) == f"https://portal.azure.com/#resource{vm}"
    group = grp("rg-a").id
    assert portal_link(group, group=True) == f"https://portal.azure.com/#@/resource{group}"
    assert portal_link(rid("rg-a", "Microsoft.Compute/disks", 'd"><x')) is None
    assert portal_link(vm + "\n") is None  # fullmatch, not match
    assert portal_link(group + "\n", group=True) is None
    # Only ASCII: no Arabic-Indic digit, and no KELVIN SIGN, which IGNORECASE alone folds to "k".
    assert portal_link(vm.replace(SUB, SUB[:-1] + "\u0661")) is None
    assert portal_link(grp("rg-\u212a").id, group=True) is None


def test_portal_links_say_they_open_a_new_tab():
    # The group's own ID wins over the spelling in the resource ID, as its name does.
    known = Group(f"/subscriptions/{SUB}/resourceGroups/RG-A", "RG-A", SUB)
    row = make_row(_vm(), age_days=2)
    page = render_page(make_report([row], groups={known.key: known}), CFG)
    assert (
        '<h2>By student</h2><p class="muted">Resource names open the Azure portal in a new tab.</p>'
    ) in page
    assert page.count(NEW_TAB) == page.count('target="_blank"') == 2  # the name and its group
    assert _heading("RG-A", known.id) in page
    assert _name(row.resource) in page
    assert ".vh{" in CSS  # visually hidden, still read out
    assert "<h2>By student</h2><p>Nothing running.</p>" in render_page(make_report(), CFG)


def test_hostile_group_and_resource_names_stay_inert():
    evil = "<script>alert(1)</script>\"'><img src=x onerror=alert(1)>"
    group = "<script>x</script>"
    g = grp(group)
    row = make_row(res(DISK, '"><img src=x onerror=alert(1)>', group=group), cohort=evil)
    lost = res(PUBLIC_IP, "pip-1", group=group, tags={"Name": evil})
    unknown = Row(lost, Owner(None, "unknown", "no creation event", guess=evil), None, 1)
    report = make_report([row], unknown=[unknown], groups={g.key: g})
    page = render_page(report, CFG)
    assert page.count("<script>") == 1  # only our own
    assert "<img" not in page
    assert "&lt;script&gt;x&lt;/script&gt;" in page
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_csp_hashes_match_inline_blocks():
    page = render_page(make_report(), CFG)
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page).group(1)
    assert f"style-src '{_sha(CSS)}'" in csp
    assert f"script-src '{_sha(JS)}'" in csp
    assert csp.startswith("default-src 'none'")
    assert f"<style>{CSS}</style>" in page
    assert f"<script>{JS}</script>" in page
    # Links never pass this page's URL on.
    assert '<meta name="referrer" content="no-referrer">' in page


def test_no_inline_style_or_event_handler_attributes():
    row = make_row(res(PUBLIC_IP, "pip-1"), age_days=30)
    page = render_page(make_report([row]), CFG)
    assert not re.search(r"\sstyle=", page)
    assert not re.search(r"\son[a-z]+=", page)


def test_empty_report_renders():
    page = render_page(make_report(), CFG)
    assert "<h2>By student</h2><p>Nothing running.</p>" in page
    # The script looks these up by id, so they render even when empty.
    assert '<section id="this-dashboard"><h2>This dashboard</h2><p>None.</p></section>' in page
    assert (
        '<details id="made-by-azure"><summary>Made by Azure (0)</summary><p>None.</p></details>'
    ) in page


def test_incomplete_is_one_small_banner_on_top():
    messages = [
        "Resource Graph (RateLimiting)",
        "Activity Log in Course subscription, 1 of 1 groups (AuthorizationFailed)",
    ]
    page = render_page(make_report(incomplete=messages), CFG)
    banner = (
        '<p class="banner" id="incomplete">Incomplete: Resource Graph (RateLimiting); '
        "Activity Log in Course subscription, 1 of 1 groups (AuthorizationFailed)</p>"
    )
    assert page.count('id="incomplete"') == 1
    assert page.index(banner) < page.index("<h2>By student</h2>")
    assert 'id="incomplete"' not in render_page(make_report(), CFG)


def test_page_leaves_out_the_teacher_diagnostics():
    old = make_row(_vm(properties=RUNNING), age_days=30, public_ip=True)
    page = render_page(make_report([old], unmapped_users=3, roster_loaded=False), CFG)
    # They belong in the local verify.md, never on the page every student reads.
    assert "unmapped" not in page
    assert "roster: not loaded" not in page


def test_student_wording():
    row = make_row(res(PUBLIC_IP, "pip-1"), age_days=30)
    page = render_page(make_report([row]), CFG)
    assert "<title>Forgotten Azure resources</title>" in page
    assert (
        "<h1>What did I forget to delete?</h1>"
        "<p>Type your username, delete what you no longer need.</p>"
    ) in page
    assert "<h2>By student</h2>" in page
    assert "<h2>No owner found (ask before deleting)</h2>" in page
    assert "<h2>Cluster gone (ask a teacher)</h2>" in page
    assert FOOTER in page
    assert (
        "Visible to everyone with access to the class subscription. "
        "Please don't share screenshots outside the course." in FOOTER
    )
    assert 'placeholder="your school sign-in name (before the @)"' in page
    assert '<p class="muted">Resource names open the Azure portal in a new tab.</p>' in page
    for word in ("AWS", "IAM", "console"):
        assert word not in page


def test_cards_alphabetical_with_the_check_rank_for_filters():
    calm = make_row(_vm(), user="student-a", age_days=1)
    busy = make_row(res(DISK, "disk-1"), user="student-b", age_days=10)
    page = render_page(make_report([busy, calm]), CFG)
    assert page.index('id="card-student-a"') < page.index('id="card-student-b"')
    assert (
        'id="card-student-a" data-cohort="cohort-x" data-rank="1" data-alpha="0" data-checks="0"'
        in page
    )
    assert (
        'id="card-student-b" data-cohort="cohort-x" data-rank="0" data-alpha="1" data-checks="1"'
        in page
    )
    assert 'var key = c === "" && u === "" ? "data-alpha" : "data-rank";' in JS
    assert "order(key);" in JS


def test_cohort_filter_options_and_data_attributes():
    row = make_row(_vm(), cohort="cohort-x")
    page = render_page(make_report([row]), CFG)
    assert '<option value="cohort-x">cohort-x</option>' in page
    assert 'data-cohort="cohort-x"' in page


def test_stale_banner_is_hidden_with_the_generated_time():
    page = render_page(make_report(), CFG)
    assert (
        '<p class="banner stale" id="stale" data-generated="2026-10-02T12:00:00Z" hidden>'
        "This page is more than a day old: the scheduled runs may have failed. Tell a teacher.</p>"
    ) in page
    assert page.index('id="stale"') < page.index("<h2>By student</h2>")
    assert "26 * 3600 * 1000" in JS


def test_card_columns_and_phone_layout():
    row = make_row(_vm(), age_days=2)
    page = render_page(make_report([row]), CFG)
    assert "<th>Resource</th><th>Type</th><th>Age</th><th>Check</th>" in page
    assert (
        f"<td>{_name(row.resource)}</td><td>microsoft.compute/virtualmachines</td>"
        '<td class="num">2 d</td><td></td></tr>'
    ) in page
    assert "@media (max-width:600px)" in CSS


def test_cards_group_rows_under_resource_group_headings():
    in_b = make_row(res(DISK, "disk-b", group="rg-b"))
    in_a = make_row(res(DISK, "disk-a", group="rg-a"))
    # A group's own name wins over the spelling in a resource ID.
    known = Group(grp("rg-a").id, "RG-A", SUB)
    other = make_row(res(DISK, "disk-c", group="rg-b"), user=None, method="unknown")
    card = Card("student-a", "cohort-x", [in_b, in_a])
    report = make_report([in_b, in_a, other], cards=[card], groups={known.key: known})
    page = render_page(report, CFG)
    assert page.count("<h4>") == 2
    first = (
        f'{_heading("RG-A", known.id)}<p class="muted">{DELETE_GROUP_TEXT}</p>'
        '<div class="wrap"><table>'
    )
    # rg-b also holds a row without an owner, so it gets the safe line (escaped apostrophe).
    shared = SHARED_GROUP_TEXT.replace("'", "&#x27;")
    second = f'{_heading("rg-b")}<p class="muted">{shared}</p><div class="wrap"><table>'
    assert first in page
    assert second in page
    disk_a = f"<td>{_name(in_a.resource)}</td>"
    assert page.index(first) < page.index(disk_a) < page.index(second)
    assert page.index(second) < page.index(f"<td>{_name(in_b.resource)}</td>")


def test_help_line_follows_group_owners():
    key = grp("rg-a").key
    card = Card("student-a", "cohort-x", [])

    def line(owners, **overrides):
        return help_line(card, key, make_report(group_owners={key: frozenset(owners)}, **overrides))

    assert line({"student-a"}) == DELETE_GROUP_TEXT
    assert line({"student-a", "student-b"}) == SHARED_GROUP_TEXT
    assert line({"student-a", None}) == SHARED_GROUP_TEXT
    assert line({"student-a"}, node_groups=frozenset({key})) == NODE_GROUP_TEXT
    assert line({"student-a", None}, node_groups=frozenset({key})) == NODE_GROUP_TEXT
    # Never advise deleting a group the scan may not have seen whole (owner, 2026-10-30).
    assert line({"student-a"}, incomplete_subscriptions=frozenset({SUB})) == SHARED_GROUP_TEXT
    assert help_line(card, key, make_report()) == SHARED_GROUP_TEXT
    assert DELETE_GROUP_TEXT == (
        "Delete the resource group to remove everything in it, or delete items one by one."
    )
    assert SHARED_GROUP_TEXT == (
        "This group also holds other people's resources: delete your items one by one."
    )
    assert NODE_GROUP_TEXT == "Made by a Kubernetes cluster: delete the cluster, not this group."


def test_parent_and_group_owners_show_their_via_note():
    parent = make_row(res(DISK, "disk-1"), method="parent", detail="via vm-1")
    group = make_row(res(PUBLIC_IP, "pip-1"), method="resource-group", detail="via resource group")
    event = make_row(res(DISK, "disk-2"), detail="not a note")
    card = Card("student-a", "cohort-x", [parent, group, event])
    page = render_page(make_report([parent, group, event], cards=[card]), CFG)
    assert f'<td>{_name(parent.resource)} <span class="muted">via vm-1</span></td>' in page
    assert f'<td>{_name(group.resource)} <span class="muted">via resource group</span></td>' in page
    assert f"<td>{_name(event.resource)}</td>" in page
    assert "not a note" not in page


def test_header_names_scanned_and_not_scanned_subscriptions():
    page = render_page(make_report(not_scanned=["Second course"]), CFG)
    assert " · subscriptions Course subscription · not scanned: Second course</p>" in page
    page = render_page(make_report(), CFG)
    assert " · subscriptions Course subscription</p>" in page
    assert "not scanned" not in page


def test_bottom_tables_have_a_resource_group_column():
    failed = make_row(
        res(DISK, "disk-1"), user=None, method="lookup-failed", detail="unknown (lookup failed)"
    )
    guessed = Row(
        res(PUBLIC_IP, "pip-1", group="rg-b"),
        Owner(None, "unknown", "no creation event in the last 90 days", guess="web"),
        None,
        30,
    )
    page = render_page(make_report([failed, guessed], unknown=[failed, guessed]), CFG)
    assert "<th>Resource</th><th>Type</th><th>Resource group</th><th>Age</th><th>Why</th>" in page
    assert (
        f"<tr><td>{_name(failed.resource)}</td><td>microsoft.compute/disks</td><td>rg-a</td>"
        '<td class="num">1 d</td><td>unknown (lookup failed)</td></tr>'
    ) in page
    assert (
        '<td>rg-b</td><td class="num">30 d</td><td>no creation event in the last 90 days'
        " · Name tag: web (guess)</td></tr>"
    ) in page


def test_unknown_age_reads_unknown():
    lost = make_row(res(DISK, "disk-1"), user=None, method="unknown", detail="x", age_days=None)
    page = render_page(make_report([lost], unknown=[lost]), CFG)
    assert '<td>rg-a</td><td class="num">unknown</td><td>x</td></tr>' in page


def test_quiet_sections():
    watcher = make_row(res(NETWORK_WATCHER, "nw-1"), user=None, method="azure", age_days=30)
    own = make_row(
        res(DISK, "disk-1", group="resource-owner-dashboard"),
        user=None,
        method="dashboard",
        age_days=30,
    )
    page = render_page(make_report([watcher, own], made_by_azure=[watcher], dashboard=[own]), CFG)
    this = page.index('<section id="this-dashboard"><h2>This dashboard</h2>')
    made = page.index('<details id="made-by-azure"><summary>Made by Azure (1)</summary>')
    assert page.index('id="cluster-gone"') < this < made
    assert made < page.index('<a href="#top">Back to top</a>')
    assert f"<td>{_name(watcher.resource)}</td>" in page[made:]
    assert f"<td>{_name(own.resource)}</td>" in page[this:made]
    quiet = page[this : page.index("<footer>")]
    assert 'class="check"' not in quiet
    assert "marked" not in quiet
    assert 'getElementById("this-dashboard")' in JS
    assert 'getElementById("made-by-azure")' in JS
    assert 'thisDashboard.hidden = u !== ""' in JS
    assert 'madeByAzure.hidden = u !== ""' in JS


def test_check_marks_in_card_rows():
    old_public = make_row(_vm(properties=RUNNING), age_days=30, public_ip=True)
    fresh = make_row(res(DISK, "disk-1"), age_days=1)
    card = Card("student-a", "cohort-x", [old_public, fresh])
    page = render_page(make_report([old_public, fresh], cards=[card]), CFG)
    assert f'<tr class="marked"><td>{_name(old_public.resource)}</td>' in page
    assert (
        '<td><strong class="check">Check this:</strong> '
        '<span class="muted">older than 4 days, running with a public IP</span></td>'
    ) in page
    assert page.count('<tr class="marked">') == 1
    assert "tr.marked{background:var(--bad-bg)}" in CSS
    assert page.count('class="check"') == 1
    assert '<p class="muted">1 to check · 2 resources</p>' in page


def test_all_clear_only_when_nothing_runs():
    assert 'getAttribute("data-checks")' in JS
    assert "data-action" not in JS
    assert 'allClear.hidden = u === "" || matched === 0 || todo > 0 || rows > 0' in JS
    page = render_page(make_report(), CFG)
    assert (
        '<p class="banner ok" id="all-clear" hidden>'
        "All clear: nothing running for this username.</p>"
    ) in page


def test_unflagged_resources_get_their_own_banner():
    assert 'notFlagged.hidden = u === "" || matched === 0 || todo > 0 || rows === 0' in JS
    page = render_page(make_report(), CFG)
    assert (
        '<p class="banner" id="not-flagged" hidden>Nothing flagged yet, but resources are '
        "still running for this username. Delete what you no longer need.</p>"
    ) in page


def test_card_carries_its_resource_count():
    a = make_row(res(DISK, "disk-1"))
    b = make_row(res(DISK, "disk-2"))
    page = render_page(make_report([a, b], cards=[Card("student-a", "cohort-x", [a, b])]), CFG)
    assert 'data-checks="0" data-rows="2"' in page
    empty = render_page(make_report(cards=[Card("student-b", "cohort-x", [])]), CFG)
    assert 'data-checks="0" data-rows="0"' in empty


def test_find_me_suggests_every_username_once():
    rows = [make_row(res(DISK, n), user=u) for n, u in [("d1", "Student-B"), ("d2", "student-a")]]
    rows.append(make_row(res(DISK, "d4"), user="student-a"))
    evil = make_row(res(DISK, "d3"), user='x" onfocus="alert(1)')
    page = render_page(make_report([*rows, evil]), CFG)
    assert '<input id="find-me" type="text" list="usernames"' in page
    assert (
        '<datalist id="usernames"><option value="student-a"></option>'
        '<option value="Student-B"></option>'
        '<option value="x&quot; onfocus=&quot;alert(1)"></option></datalist>'
    ) in page


def test_find_me_controls_and_user_attributes():
    row = make_row(res(PUBLIC_IP, "pip-1"), age_days=30)
    page = render_page(make_report([row]), CFG)
    assert '<input id="find-me" type="text"' in page
    assert '<button id="forget-me" type="button">Forget me</button>' in page
    assert '<p class="banner" id="no-match" hidden>' in page
    assert page.count('data-user="student-a"') == 1  # the card only


def test_find_me_script_keeps_the_name_out_of_html_and_the_url():
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "user="):
        assert sink not in JS
    assert 'getAttribute("data-user")' in JS
    assert all("try {" in line for line in JS.splitlines() if "localStorage" in line)
    for arg in re.findall(r"querySelector(?:All)?\(([^)]*)\)", JS):
        assert arg.startswith('"')  # only literal selectors, never the typed name


def test_malicious_username_is_escaped_in_data_user():
    evil = 'x" onmouseover="alert(1)'
    row = make_row(res(PUBLIC_IP, "pip-1"), user=evil)
    page = render_page(make_report([row]), CFG)
    assert 'data-user="x&quot; onmouseover=&quot;alert(1)"' in page
    assert 'data-user="x" onmouseover' not in page


def test_showing_line_follows_find_me():
    row = make_row(res(PUBLIC_IP, "pip-1"), age_days=30)
    page = render_page(make_report([row]), CFG)
    assert page.index('id="cohort"') < page.index('id="find-me"')
    assert page.index('id="find-me"') < page.index('id="showing"')
    assert '<label for="find-me">Find me</label>' in page


def test_show_all_does_not_forget_the_name():
    assert 'document.getElementById("show-all")' in JS
    handler = JS[JS.index("showAll.addEventListener") :]
    handler = handler[: handler.index("});")]
    assert 'who.value = ""' in handler
    assert "localStorage" not in handler
    assert 'getElementById("showing-name")' in JS
    assert re.search(r"showingName\.textContent\s*=", JS)


def test_no_owner_section_has_an_id_for_the_filter():
    row = make_row(res(PUBLIC_IP, "pip-1"), user=None)
    page = render_page(make_report([row], unknown=[row]), CFG)
    assert '<section id="no-owner">' in page
    assert 'getElementById("no-owner")' in JS


def test_cluster_gone_section_hides_while_filtered():
    row = make_row(res(AKS, "aks-1"), user=None)
    page = render_page(make_report([row], orphaned=[row]), CFG)
    assert '<section id="cluster-gone">' in page
    assert 'getElementById("cluster-gone")' in JS
    assert "clusterGone.hidden = u !== " in JS


def test_find_me_input_is_debounced_and_sorts_only_on_key_change():
    assert "setTimeout(" in JS
    assert "clearTimeout(" in JS
    assert "sortedBy" in JS
    lines = JS.splitlines()
    calls = [
        i for i, line in enumerate(lines) if "order(" in line and "function order(" not in line
    ]
    assert calls
    for i in calls:
        assert re.search(r"if \(.*sortedBy", lines[i - 1])


def test_generated_time_names_central_european_time_with_its_offset():
    summer = render_page(make_report(), CFG)  # 2026-10-02 12:00 UTC: summer time, UTC+2
    assert (
        'Generated <time id="generated" datetime="2026-10-02T12:00:00Z">2026-10-02 12:00 UTC</time>'
        ' (<span id="local-time">14:00 Central European time, UTC+2</span>)'
    ) in summer
    winter = render_page(make_report(generated_at=datetime(2026, 1, 15, 6, 0, tzinfo=UTC)), CFG)
    assert '(<span id="local-time">07:00 Central European time, UTC+1</span>)' in winter
    assert "Berlin" not in summer


def test_script_shows_the_generated_time_in_the_viewers_zone():
    assert 'getElementById("generated")' in JS
    assert 'getAttribute("datetime")' in JS
    for option in ('hourCycle: "h23"', 'timeZoneName: "shortOffset"'):
        assert option in JS
    assert "your time, " in JS
    # Older browsers reject shortOffset: the server text stays.
    line = next(line for line in JS.splitlines() if "Intl.DateTimeFormat" in line)
    assert "try {" in line


def _two_cards():
    calm = make_row(_vm(), user="student-a", age_days=1)
    busy = make_row(res(DISK, "disk-1"), user="student-b", age_days=10)
    return render_page(make_report([busy, calm]), CFG)


def test_only_marked_checkbox_sits_between_cohort_and_find_me():
    page = _two_cards()
    assert (
        '<p><input id="only-checks" type="checkbox"> '
        '<label for="only-checks">Only show students with something to check</label></p>'
    ) in page
    assert page.index('id="cohort"') < page.index('id="only-checks"') < page.index('id="find-me"')
    assert 'getElementById("only-checks")' in JS
    assert 'only.addEventListener("change", apply)' in JS
    assert 'only.checked && el.getAttribute("data-checks") === "0"' in JS
    assert "localStorage" not in "".join(line for line in JS.splitlines() if "only" in line)


def test_overview_line_counts_students_and_announces_changes():
    page = _two_cards()
    assert (
        '<nav id="overview" aria-label="On this page"><p>'
        '<span id="result-count" role="status" aria-live="polite">'
        "2 students · 1 with something to check</span>"
    ) in page
    assert page.index('id="showing"') < page.index('id="overview"')
    assert page.index('id="overview"') < page.index("<h2>By student</h2>")
    assert '"Showing " + shown + " of "' in JS
    assert 'overview.hidden = u !== ""' in JS
    assert "resultCount.textContent !== text" in JS  # announce only real changes


def test_overview_links_to_the_bottom_sections_by_count():
    lost = make_row(res(PUBLIC_IP, "pip-1"), user=None)
    page = render_page(make_report([lost], unknown=[lost]), CFG)
    assert (
        '<span id="result-count" role="status" aria-live="polite">0 students · 0 with something'
        ' to check</span> · <a href="#no-owner">1 with no owner</a> · 0 cluster gone</p></nav>'
    ) in page
    gone = make_row(res(AKS, "aks-1"), user=None)
    page = render_page(make_report([gone], orphaned=[gone, gone]), CFG)
    assert ' · 0 with no owner · <a href="#cluster-gone">2 cluster gone</a></p></nav>' in page


def test_back_to_top_link_before_the_footer():
    page = render_page(make_report(), CFG)
    assert '<main id="top">' in page
    assert f'</details><p><a href="#top">Back to top</a></p>{FOOTER}' in page
    assert FOOTER.startswith("<footer>")


def test_a_url_hash_that_is_not_a_cohort_keeps_the_current_cohort():
    # #no-owner, #cluster-gone and #top are links, not cohorts: filtering by them would hide every card.
    assert "select.options[i].value === h" in JS
    assert "return current;" in JS

"""Render the single self-contained HTML page.

Each resource row that needs a look gets a "check this" mark. Cards group their
rows by resource group, each with a help line.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import Config
from .model import VM, Resource, parse_id
from .pipeline import Card, Report, Row

STOPPED_TEXT = "stopped but still billed: use Stop in the portal so it shows Deallocated"
UNKNOWN_POWER_TEXT = "power state unknown: check it is Deallocated"
# Known power states that get no power mark, casefolded; any other code is unknown.
_NO_POWER_MARK = frozenset(
    f"powerstate/{s}" for s in ("deallocated", "deallocating", "starting", "stopping")
)


def checks(row: Row, cfg: Config) -> tuple[str, ...]:
    """Return why a resource gets a "check this" mark: age, a public IP, a billed VM."""
    # A part of an existing VM goes with it, and the VM's own marks cover it.
    if row.attached:
        return ()
    r = row.resource
    out = []
    if row.age_days is not None and row.age_days > cfg.max_age_days:
        out.append(f"older than {cfg.max_age_days} days")
    if r.type == VM:
        # Stopped still bills compute; only Deallocated stops it
        # (https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing).
        code = r.power_state
        state = code.casefold() if isinstance(code, str) else None
        if state == "powerstate/running":
            if row.public_ip:
                out.append("running with a public IP")
        elif state == "powerstate/stopped":
            out.append(STOPPED_TEXT)
        elif state not in _NO_POWER_MARK:  # fail closed: never let a bad read hide a billed VM
            out.append(UNKNOWN_POWER_TEXT)
    return tuple(out)


def check_count(card: Card, cfg: Config) -> int:
    return sum(1 for r in card.rows if checks(r, cfg))


CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--muted:#5f6368;--line:#e2e2e2;
--warn-bg:#fff4e5;--warn-fg:#7a4100;--ok-bg:#e8f5e9;--ok-fg:#1b5e20;--badge:#eef1f5;
--bad-bg:#fdecea;--bad-fg:#8a1c1c}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e8eaed;--muted:#9aa0a6;
--line:#2c3036;--warn-bg:#3a2a12;--warn-fg:#ffcc80;--ok-bg:#16301a;--ok-fg:#a5d6a7;--badge:#2a2f36;
--bad-bg:#3b1414;--bad-fg:#ffb4ab}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 4px}h2{font-size:1.15rem;margin:28px 0 8px}h3{font-size:1rem;margin:0 0 8px}
.muted{color:var(--muted)}
.banner{background:var(--warn-bg);color:var(--warn-fg);padding:8px 12px;border-radius:6px;margin:8px 0}
.banner.stale{background:var(--bad-bg);color:var(--bad-fg);font-weight:600}
.banner.ok{background:var(--ok-bg);color:var(--ok-fg)}
.check{font-weight:600;color:var(--bad-fg)}
tr.marked{background:var(--bad-bg)}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
footer{margin:28px 0 8px}
input,select,button{font:inherit}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.92rem}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted)}
.num{text-align:right;white-space:nowrap}
.badge{display:inline-block;background:var(--badge);border-radius:999px;padding:0 8px;
font-size:.78rem;margin-left:4px;white-space:nowrap}
.card{border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0}
.card.clean{background:var(--ok-bg);color:var(--ok-fg)}
a{color:inherit}
@media (max-width:600px){.wrap thead{display:none}
.wrap tr{display:block;border-bottom:1px solid var(--line);padding:4px 0}
.wrap td{display:block;border:0;padding:2px 8px}.wrap .num{text-align:left}}
[hidden]{display:none!important}
"""

JS = """
(function () {
  var select = document.getElementById("cohort");
  var label = document.getElementById("cohort-label");
  var cards = document.getElementById("cards");
  var stale = document.getElementById("stale");
  var who = document.getElementById("find-me");
  var forget = document.getElementById("forget-me");
  var allClear = document.getElementById("all-clear");
  var notFlagged = document.getElementById("not-flagged");
  var noMatch = document.getElementById("no-match");
  var showing = document.getElementById("showing");
  var showingName = document.getElementById("showing-name");
  var showAll = document.getElementById("show-all");
  var noOwner = document.getElementById("no-owner");
  var clusterGone = document.getElementById("cluster-gone");
  var thisDashboard = document.getElementById("this-dashboard");
  var madeByAzure = document.getElementById("made-by-azure");
  var only = document.getElementById("only-checks");
  var overview = document.getElementById("overview");
  var resultCount = document.getElementById("result-count");
  var KEY = "dashboard-find-me";
  var STALE_MS = 26 * 3600 * 1000;
  var generated = Date.parse(stale.getAttribute("data-generated"));
  if (!isNaN(generated) && Date.now() - generated > STALE_MS) { stale.hidden = false; }
  var TIME = { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "shortOffset" };
  function part(parts, type) {
    for (var i = 0; i < parts.length; i++) { if (parts[i].type === type) { return parts[i].value; } }
    return "";
  }
  try { var parts = new Intl.DateTimeFormat(undefined, TIME).formatToParts(new Date(document.getElementById("generated").getAttribute("datetime"))); document.getElementById("local-time").textContent = part(parts, "hour") + ":" + part(parts, "minute") + " your time, " + part(parts, "timeZoneName"); } catch (err) { /* older browser: the server text stays */ }
  try { who.value = localStorage.getItem(KEY) || ""; } catch (err) { who.value = ""; }
  var current = "";
  // Only a cohort in the select counts: #no-owner, #cluster-gone and #top are jump links.
  function cohort() {
    var h;
    try { h = decodeURIComponent(location.hash.slice(1)); } catch (err) { return current; }
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === h) { current = h; }
    }
    return current;
  }
  function typed() { return who.value.trim().toLowerCase(); }
  function order(key) {
    var list = Array.prototype.slice.call(cards.querySelectorAll("article.card"));
    list.sort(function (a, b) { return Number(a.getAttribute(key)) - Number(b.getAttribute(key)); });
    for (var i = 0; i < list.length; i++) { cards.appendChild(list[i]); }
  }
  var sortedBy = "data-alpha";
  var timer = null;
  function apply() {
    var c = cohort();
    var u = typed();
    var items = document.querySelectorAll("[data-cohort]");
    var matched = 0;
    var todo = 0;
    var rows = 0;
    var shown = 0;
    var marked = 0;
    for (var i = 0; i < items.length; i++) {
      var el = items[i];
      var owner = (el.getAttribute("data-user") || "").toLowerCase();
      var hide = (c !== "" && el.getAttribute("data-cohort") !== c) || (u !== "" && owner !== u) ||
        (only.checked && el.getAttribute("data-checks") === "0");
      el.hidden = hide;
      if (!hide) {
        shown += 1;
        if (el.getAttribute("data-checks") !== "0") { marked += 1; }
      }
      if (!hide && u !== "") {
        matched += 1;
        todo += Number(el.getAttribute("data-checks") || "0");
        rows += Number(el.getAttribute("data-rows") || "0");
      }
    }
    allClear.hidden = u === "" || matched === 0 || todo > 0 || rows > 0;
    notFlagged.hidden = u === "" || matched === 0 || todo > 0 || rows === 0;
    noMatch.hidden = u === "" || matched > 0;
    showing.hidden = u === "";
    showingName.textContent = who.value.trim();
    var students = items.length === 1 ? "1 student" : items.length + " students";
    var text = (shown === items.length ? students : "Showing " + shown + " of " + students) +
      " · " + marked + " with something to check";
    if (resultCount.textContent !== text) { resultCount.textContent = text; }
    overview.hidden = u !== "";
    noOwner.hidden = u !== "";
    clusterGone.hidden = u !== "";
    thisDashboard.hidden = u !== "";
    madeByAzure.hidden = u !== "";
    label.textContent = c === "" ? "all cohorts" : c === "-" ? "no cohort" : c;
    select.value = c;
    var key = c === "" && u === "" ? "data-alpha" : "data-rank";
    if (key !== sortedBy) {
      order(key);
      sortedBy = key;
    }
  }
  select.addEventListener("change", function () {
    location.hash = encodeURIComponent(select.value);
  });
  who.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      try { if (typed() === "") { localStorage.removeItem(KEY); } else { localStorage.setItem(KEY, who.value.trim()); } } catch (err) { /* storage blocked: the filter still works */ }
      apply();
    }, 150);
  });
  forget.addEventListener("click", function () {
    who.value = "";
    try { localStorage.removeItem(KEY); } catch (err) { /* nothing stored */ }
    apply();
  });
  showAll.addEventListener("click", function () {
    who.value = "";
    apply();
  });
  only.addEventListener("change", apply);
  window.addEventListener("hashchange", apply);
  apply();
})();
"""

BERLIN = ZoneInfo("Europe/Berlin")
DELETE_GROUP_TEXT = (
    "Delete the resource group to remove everything in it, or delete items one by one."
)
SHARED_GROUP_TEXT = "This group also holds other people's resources: delete your items one by one."
NODE_GROUP_TEXT = "Made by a Kubernetes cluster: delete the cluster, not this group."


def e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _sha256(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


# The page's policy: only its own inline style and script, by hash.
CSP = (
    f"default-src 'none'; style-src '{_sha256(CSS)}'; script-src '{_sha256(JS)}'; "
    "img-src data:; base-uri 'none'; form-action 'none'"
)


# Fullmatch only: "$" would accept a trailing newline. re.ASCII, because under IGNORECASE alone
# [a-z] also matches U+212A KELVIN SIGN. The set admits only characters that are legal in a URL
# fragment as they are (RFC 3986 section 3.5), so the ID needs no encoding.
PORTAL_ID = re.compile(
    r"/subscriptions/[0-9a-f-]{36}/resourcegroups/[a-z0-9_().-]{1,90}(/providers/[a-z0-9_.()/-]+)?",
    re.IGNORECASE | re.ASCII,
)
# The ID goes in raw, unencoded, as both sources write it. Resource: "to go directly to the
# resource in the Azure portal", strcat("https://portal.azure.com/#resource", ResourceId)
# (https://learn.microsoft.com/en-us/azure/azure-monitor/vm/vminsights-maps-retirement).
# Resource group: the Azure Developer CLI output that Learn shows, "https://portal.azure.com/
# #@/resource/subscriptions/<your-subscription-id>/resourceGroups/rg-<your-environment-name>"
# (https://learn.microsoft.com/en-us/azure/spring-apps/basic-standard/quickstart-deploy-web-app).
RESOURCE_URL = "https://portal.azure.com/#resource"
GROUP_URL = "https://portal.azure.com/#@/resource"


def portal_link(arm_id: str, *, group: bool = False) -> str | None:
    """Built only from a fixed template and an ID that passes PORTAL_ID."""
    if not PORTAL_ID.fullmatch(arm_id):
        return None
    return (GROUP_URL if group else RESOURCE_URL) + arm_id


NEW_TAB = '<span aria-hidden="true"> ↗</span><span class="vh"> (opens in new tab)</span>'


def _linked(text: str, link: str | None) -> str:
    """The text, linked to the portal in a new tab when there is a link."""
    # The arrow is for the eye, the hidden words for screen readers (WCAG G201).
    return (
        f'<a href="{e(link)}" rel="noopener noreferrer" target="_blank">{e(text)}{NEW_TAB}</a>'
        if link
        else e(text)
    )


def _resource_cell(r: Resource) -> str:
    """The name, not the ID: Azure IDs are long, and the group is the heading above."""
    return _linked(r.name, portal_link(r.id))


def _type(r: Resource) -> str:
    return e(r.type)


def _group_name(report: Report, r: Resource) -> str:
    """The group's own name when the scan listed it, else the spelling in the resource ID."""
    group = report.groups.get(r.group_key)
    return group.name if group else r.group


def _group_link(report: Report, r: Resource) -> str | None:
    """The group's own ID when the scan listed it, else one built from the resource's."""
    group = report.groups.get(r.group_key)
    group_id = group.id if group else f"/subscriptions/{r.subscription}/resourceGroups/{r.group}"
    return portal_link(group_id, group=True)


def help_line(card: Card, group_key: str, report: Report) -> str:
    """Say whether the student may delete the whole group (owner, 2026-10-30).

    Only when every counted row in it is this card's, and never in a subscription whose
    inventory may be incomplete: the page must not advise deleting a group it may not have
    seen whole.
    """
    if group_key in report.node_groups:
        return NODE_GROUP_TEXT
    subscription, _ = parse_id(group_key)
    if (
        report.group_owners.get(group_key) == frozenset({card.name})
        and subscription not in report.incomplete_subscriptions
    ):
        return DELETE_GROUP_TEXT
    return SHARED_GROUP_TEXT


def _n(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _age(days: int | None) -> str:
    return "unknown" if days is None else f"{days} d"


def _check_cell(reasons: tuple[str, ...]) -> str:
    if not reasons:
        return "<td></td>"
    # Plain text, never a link: a status mark shouldn't look clickable (GOV.UK Tag).
    return (
        '<td><strong class="check">Check this:</strong> '
        f'<span class="muted">{e(", ".join(reasons))}</span></td>'
    )


def _cohort_attr(cohort: str | None) -> str:
    return f' data-cohort="{e(cohort or "-")}"'


def _user_attr(name: str | None) -> str:
    """The find-me filter compares this attribute with the typed name."""
    return f' data-user="{e(name or "")}"'


def _table(head: list[str], body: list[str]) -> str:
    cells = "".join(f"<th>{e(h)}</th>" for h in head)
    return f'<div class="wrap"><table><thead><tr>{cells}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


STALE_TEXT = "This page is more than a day old: the scheduled runs may have failed. Tell a teacher."


def _offset(when: datetime) -> str:
    """UTC+1 or UTC+2: a number, because zone abbreviations are ambiguous (IANA theory.html)."""
    offset = when.utcoffset()
    minutes = int(offset.total_seconds()) // 60 if offset else 0
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{'-' if minutes < 0 else '+'}{hours}" + (f":{rest:02d}" if rest else "")


def _header(report: Report) -> str:
    utc = report.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    cet = report.generated_at.astimezone(BERLIN)
    # Students live all over Europe: say "Central European time", never "Berlin". The
    # script swaps this for the viewer's own time when the browser can.
    local = f"{cet:%H:%M} Central European time, {_offset(cet)}"
    stamp = report.generated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The script shows this when the page is over 26 h old: yesterday's run failed.
    stale = f'<p class="banner stale" id="stale" data-generated="{stamp}" hidden>{STALE_TEXT}</p>'
    incomplete = (
        f'<p class="banner" id="incomplete">Incomplete: {e("; ".join(report.incomplete))}</p>'
        if report.incomplete
        else ""
    )
    not_scanned = (
        f" · not scanned: {e(', '.join(report.not_scanned))}" if report.not_scanned else ""
    )
    options = ['<option value="">All cohorts</option>']
    options += [f'<option value="{e(c)}">{e(c)}</option>' for c in report.cohorts]
    options.append('<option value="-">No cohort</option>')
    # The browser suggests a full username while it is typed; the filter still matches
    # the whole name (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/datalist).
    names = sorted({c.name for c in report.cards}, key=lambda n: (n.casefold(), n))
    usernames = "".join(f'<option value="{e(n)}"></option>' for n in names)
    return (
        "<header><h1>What did I forget to delete?</h1>"
        "<p>Type your username, delete what you no longer need.</p>"
        f'<p class="muted">Generated <time id="generated" datetime="{stamp}">{e(utc)}</time>'
        f' (<span id="local-time">{e(local)}</span>) · subscriptions {e(", ".join(report.scanned))}'
        f"{not_scanned}</p>"
        f"{stale}{incomplete}"
        f'<p><label for="cohort">Cohort</label> <select id="cohort">{"".join(options)}</select>'
        ' <span class="muted">showing <span id="cohort-label">all cohorts</span></span></p>'
        '<p><input id="only-checks" type="checkbox"> '
        '<label for="only-checks">Only show students with something to check</label></p>'
        '<p><label for="find-me">Find me</label> <input id="find-me" type="text" list="usernames" '
        'autocomplete="off" spellcheck="false" placeholder="your school sign-in name (before the @)"> '
        '<button id="forget-me" type="button">Forget me</button> <span class="muted">'
        "remembered on this device only</span></p>"
        f'<datalist id="usernames">{usernames}</datalist>'
        '<p id="showing" hidden>Showing only <strong id="showing-name"></strong> · '
        '<button id="show-all" type="button">Show everyone</button></p>'
        '<p class="banner ok" id="all-clear" hidden>All clear: nothing running for this '
        "username.</p>"
        '<p class="banner" id="not-flagged" hidden>Nothing flagged yet, but resources are '
        "still running for this username. Delete what you no longer need.</p>"
        '<p class="banner" id="no-match" hidden>Nothing found for this username. Check the '
        "spelling, or you have nothing running.</p></header>"
    )


def _card(card: Card, cfg: Config, rank: int, alpha: int, report: Report) -> str:
    marks = [checks(r, cfg) for r in card.rows]
    to_check = sum(1 for m in marks if m)
    cohort = (
        f'<span class="badge">{e(card.cohort)}</span>'
        if card.cohort
        else '<span class="badge">no cohort</span>'
    )
    head = f"<h3>{e(card.name)} {cohort}</h3>"
    attrs = (
        f' id="card-{e(card.name)}"{_cohort_attr(card.cohort)}'
        f' data-rank="{rank}" data-alpha="{alpha}" data-checks="{to_check}" data-rows="{len(card.rows)}"{_user_attr(card.name)}'
    )
    if not card.rows:
        return f'<article class="card clean"{attrs}>{head}<p>No resources running.</p></article>'
    by_group: dict[str, list[tuple[Row, tuple[str, ...]]]] = {}
    for r, m in zip(card.rows, marks, strict=True):
        by_group.setdefault(r.resource.group_key, []).append((r, m))
    first = {key: rows[0][0].resource for key, rows in by_group.items()}
    headings = {key: _group_name(report, r) for key, r in first.items()}
    groups = []
    for key in sorted(by_group, key=lambda k: (headings[k].casefold(), headings[k], k)):
        body = [
            f"<tr{' class="marked"' if m else ''}><td>{_resource_cell(r.resource)}"
            f"{f' <span class="muted">{e(r.owner.detail)}</span>' if r.owner.method in ('parent', 'resource-group') else ''}</td>"
            f'<td>{_type(r.resource)}</td><td class="num">{_age(r.age_days)}</td>{_check_cell(m)}</tr>'
            for r, m in by_group[key]
        ]
        groups.append(
            f"<h4>{_linked(headings[key], _group_link(report, first[key]))}</h4>"
            f'<p class="muted">{e(help_line(card, key, report))}</p>'
            + _table(["Resource", "Type", "Age", "Check"], body)
        )
    summary = f'<p class="muted">{to_check} to check · {_n(len(card.rows), "resource")}</p>'
    return f'<article class="card"{attrs}>{head}{summary}{"".join(groups)}</article>'


def _rows(report: Report, items: list[Row]) -> list[str]:
    return [
        f"<tr><td>{_resource_cell(r.resource)}</td><td>{_type(r.resource)}</td>"
        f'<td>{e(_group_name(report, r.resource))}</td><td class="num">{_age(r.age_days)}</td>'
        f"<td>{e(r.owner.detail)}{e(f' · Name tag: {r.owner.guess} (guess)') if r.owner.guess else ''}</td></tr>"
        for r in items
    ]


def _bottom_table(report: Report, items: list[Row]) -> str:
    """The rows without a card; always rendered, because the script finds them by id."""
    head = ["Resource", "Type", "Resource group", "Age", "Why"]
    return _table(head, _rows(report, items)) if items else "<p>None.</p>"


def _owner_problems(report: Report) -> str:
    unknown = _bottom_table(report, report.unknown)
    orphaned = _bottom_table(report, report.orphaned)
    return (
        f'<section id="no-owner"><h2>No owner found (ask before deleting)</h2>{unknown}</section>'
        f'<section id="cluster-gone"><h2>Cluster gone (ask a teacher)</h2>{orphaned}</section>'
    )


def _quiet(report: Report) -> str:
    """The dashboard's own group and what Azure made by itself: never on a card."""
    dashboard = _bottom_table(report, report.dashboard)
    azure = _bottom_table(report, report.made_by_azure)
    return (
        f'<section id="this-dashboard"><h2>This dashboard</h2>{dashboard}</section>'
        f'<details id="made-by-azure"><summary>Made by Azure ({len(report.made_by_azure)})</summary>'
        f"{azure}</details>"
    )


def _jump(count: int, text: str, target: str) -> str:
    """The whole phrase is the link text (WCAG 2.4.4); nothing to jump to at 0."""
    words = e(f"{count} {text}")
    return f'<a href="#{target}">{words}</a>' if count else words


def _overview(students: int, marked: int, report: Report) -> str:
    """Render the one line above the cards.

    The script keeps the student counts in step with the filters. It is a status region,
    so screen readers hear the new count (WCAG 4.1.3). The two links jump to the sections
    at the bottom (WCAG G124); those rows have no cohort.
    """
    return (
        '<nav id="overview" aria-label="On this page"><p>'
        '<span id="result-count" role="status" aria-live="polite">'
        f"{_n(students, 'student')} · {marked} with something to check</span>"
        f" · {_jump(len(report.unknown), 'with no owner', 'no-owner')}"
        f" · {_jump(len(report.orphaned), 'cluster gone', 'cluster-gone')}</p></nav>"
    )


FOOTER = (
    '<footer><p class="muted">Visible to everyone with access to the class subscription. '
    "Please don't share screenshots outside the course.</p></footer>"
)


def render_page(report: Report, cfg: Config) -> str:
    count = {c.name: check_count(c, cfg) for c in report.cards}
    # Alphabetical by default; while a cohort or name filter is set the script
    # switches to data-rank: most marked rows first, then by name.
    by_need = sorted(report.cards, key=lambda c: (-count[c.name], c.name.casefold(), c.name))
    rank = {c.name: i for i, c in enumerate(by_need)}
    cards = sorted(report.cards, key=lambda c: (c.name.casefold(), c.name))
    listed = "".join(_card(c, cfg, rank[c.name], i, report) for i, c in enumerate(cards))
    body = (
        _header(report)
        + _overview(len(report.cards), sum(1 for n in count.values() if n), report)
        + '<section id="cards"><h2>By student</h2>'
        + (
            '<p class="muted">Resource names open the Azure portal in a new tab.</p>' + listed
            if listed
            else "<p>Nothing running.</p>"
        )
        + "</section>"
        + _owner_problems(report)
        + _quiet(report)
        + '<p><a href="#top">Back to top</a></p>'
        + FOOTER
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        '<meta name="referrer" content="no-referrer">'
        '<meta name="robots" content="noindex"><title>Forgotten Azure resources</title>'
        f'<style>{CSS}</style></head><body><main id="top">{body}</main>'
        f"<script>{JS}</script></body></html>"
    )

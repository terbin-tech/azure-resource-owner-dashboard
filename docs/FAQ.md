# Dashboard: how it works and FAQ

## How it works

1. **Schedule.** A timer starts an Azure Function at 06:00 and 16:00 UTC.
2. **Scan (read-only).** The function lists the resources in the class subscription, in every region and of every resource type. It changes and deletes nothing.
3. **Owner.** For each resource, it finds the creation event in the Activity Log. The Activity Log keeps the last 90 days, and the event shows which school sign-in created the resource. Some resources take their owner from a parent: a disk from its VM, a network interface from its VM, a public IP from its network interface. Others take it from their resource group, which names who created the group.
4. **Cohort.** A teacher uploads a roster to the dashboard's storage, under `config/roster.csv`. It maps each sign-in to a person and a cohort, and lets a second sign-in of the same person share one card. Cohorts come only from the roster: a sign-in that is not in it still gets a card, shown with "no cohort", and without a roster every card shows "no cohort".
5. **Page.** The function writes one HTML file, `site/index.html`, to its own private storage account. A second function, the page, hands it out at `/api/page`, behind the school sign-in, to people who hold a role on the class subscription.
6. **Nothing is stored.** Who created what exists only in memory during a run. Each run replaces the page, and old page versions expire after about 1 to 2 days.

All infrastructure is Bicep, in `infra/`. A maintainer deploys it from their own machine. GitHub never gets Azure credentials.

## FAQ

**Who can see the page?**
Everyone who signs in with a school account and holds a role on the class subscription. The page asks Azure which roles the signed-in person has, on every request, so a person who loses their role loses the page on their next reload. Someone without a role sees a short "you don't have access" page. Students see each other's resources; the teachers agreed to that. The page shows school sign-in prefixes, not real names.

**What does "Check this" mean?**
The resource is older than 4 days, it is a running VM with a public IP, it is a VM that is stopped but still billed, or its power state could not be read. Its row is tinted. A disk attached to a VM, a network interface on an existing VM and a public IP on such a network interface are never marked, because the VM's own marks cover them. A mark is a hint, not an order. The teachers decide what has to go.

Only a VM in the Deallocated state stops the compute charge. A VM stopped from inside the operating system, or stopped without deallocating, is still billed, so it is marked: use Stop in the portal and check it shows Deallocated.

**What does the help line under a resource group say?**
Either that you can delete the whole group, or that you should delete your own items one by one. The first appears only when every counted row in the group is yours and the scan of that subscription was complete. A group that Kubernetes made for a cluster says to delete the cluster instead.

**Why isn't my resource on my card?**
- It was created more than 90 days ago. The Activity Log no longer has the event, so the resource shows under "No owner found".
- It was created with someone else's credentials, for example a script running under another sign-in. It then shows on that person's card.
- Azure created it by itself. It is listed under "Made by Azure" at the bottom, never on a card.
- It sits in the dashboard's own resource group. It is listed under "This dashboard".
- It was created after the last run. Wait for the next one, at 06:00 or 16:00 UTC.
- Your resources are on a second card, because the roster does not list that sign-in under your main one. Ask a teacher to add it.

**Can I see only the students with something to check?**
Tick "Only show students with something to check" under Cohort. The line above the cards counts the students shown and how many of them have a mark. It follows the cohort too. The box starts unticked on every visit.

**Where is "No owner found"?**
At the bottom of the full page. The line above the cards links to it ("... with no owner") and to "Cluster gone". "Back to top" at the bottom takes you back. It's hidden while a name is typed into Find me, and shows again under Show everyone. Ask a teacher before deleting what is listed there.

**What is "Cluster gone"?**
Resources that belonged to a Kubernetes cluster that no longer exists, such as disks or load balancers left behind. Nobody can be named as the owner. Ask a teacher.

**Does Find me send my username anywhere?**
No. It filters in your browser, and only remembers the name on your device (`localStorage`). Forget me clears it.

**The page has a red banner. What now?**
It means the page is more than 26 hours old: at least two scheduled runs in a row did not update it. Tell a teacher.

**What does the "incomplete" banner mean?**
Part of the scan failed, for example one query or one subscription. The page may be missing resources. It names what failed.

**The page says "The page has not been built yet" or "The access check failed". What now?**
The first appears before the first run has written the page; wait for the next run. The second means Azure could not answer the role check for a moment; reload in a minute. If either stays, tell a teacher.

**What does it cost to run?**
Practically nothing, though the figure is not known yet. The Flex Consumption plan has a monthly free grant of executions and memory time, and that grant is per subscription, so whether it applies to the class subscription depends on the subscription's offer. The page is a few kB, and the logs are kept for 30 days.

**Can the dashboard delete or change anything?**
No. Every Azure client the scan uses sends its requests through a transport that refuses everything except GET requests and the Resource Graph query, so a write fails before it leaves the function, and an offline test proves it. The scan's identity holds the Reader role on each scanned subscription, and no more there. Its one write is the page, a single `PUT` of `site/index.html` in its own storage account, through a second guard that allows no other request. Nothing in the class's resources is created, changed or deleted.

**Who gets told when a run fails?**
Three Azure Monitor alert rules watch for a failed run (`scan-failed`), no successful run for about 26 hours (`scan-missing`), and an incomplete run (`scan-incomplete`). They send email through an action group, but only if an alert email was set when the dashboard was deployed. The email address has to be confirmed with a one-time passcode within 30 minutes of the deploy ([action groups](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/action-groups)). Right after the schedule is switched on, `scan-missing` fires until the first run has happened; that is expected.

**How do I run it locally?**
Without Azure, `python -m dashboard demo` writes a page with fake data to `out-demo/index.html`. The real scan, `python -m dashboard scan`, uses your own `az login` and writes `out/index.html` and `out/verify.md`. Delete `out/` afterwards, because it holds live data.

**How do I deploy a change?**
From a clean checkout of `main`, follow the deploy steps in the README: what-if, summary, review, create, smoke test. A person reviews every what-if before anything is created, and nothing deploys from GitHub.

**How do I turn it off?**
Either set `scheduleEnabled = false` in `infra/main.bicepparam` and deploy, which stops the runs and the missed-run alert, or remove everything. Bicep has no destroy, so removal goes in this order:

1. Delete the app registration with `az ad app delete --id <app registration client ID>`, so that its redirect URI never points at a hostname that has been freed ([dangling subdomain prevention](https://learn.microsoft.com/en-us/azure/app-service/reference-dangling-subdomain-prevention)).
2. Delete the Reader assignment of the `scan` identity on each scanned subscription, by its ID only. List it first: `az role assignment list --assignee-object-id <scan principal id> --role Reader --scope /subscriptions/<subscription id> --fill-principal-name false --query '[].id' -o tsv` must print exactly one non-empty ID. Then `az role assignment delete --ids <that id>`. Never delete by `--scope` alone: the command "deletes all role assignments that satisfy the provided query condition" ([az role assignment](https://learn.microsoft.com/en-us/cli/azure/role/assignment)). The scan identity's principal ID is the `scanPrincipalId` output of the deployment, and it is gone once the resource group is.
3. Delete the resource group, with `az group delete --name <resource group name>`. This removes the function app, both storage accounts, the logs and the alert rules.

All of it can be rebuilt from the repo.

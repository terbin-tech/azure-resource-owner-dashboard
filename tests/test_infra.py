import json
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROLE = re.compile(r"\[roleDefinitions\('([^']+)'\)\.id\]")
APP_TABLES = [
    "AppAvailabilityResults",
    "AppBrowserTimings",
    "AppDependencies",
    "AppEvents",
    "AppExceptions",
    "AppMetrics",
    "AppPageViews",
    "AppPerformanceCounters",
    "AppRequests",
    "AppSystemEvents",
    "AppTraces",
]
# https://learn.microsoft.com/en-us/azure/azure-functions/functions-app-settings#flex-consumption-plan-deprecations
DEPRECATED_SETTINGS = {
    "ENABLE_ORYX_BUILD",
    "FUNCTIONS_EXTENSION_VERSION",
    "FUNCTIONS_WORKER_RUNTIME",
    "FUNCTIONS_WORKER_RUNTIME_VERSION",
    "FUNCTIONS_MAX_HTTP_CONCURRENCY",
    "FUNCTIONS_WORKER_PROCESS_COUNT",
    "FUNCTIONS_WORKER_DYNAMIC_CONCURRENCY_ENABLED",
    "SCM_DO_BUILD_DURING_DEPLOYMENT",
    "WEBSITE_CONTENTAZUREFILECONNECTIONSTRING",
    "WEBSITE_CONTENTOVERVNET",
    "WEBSITE_CONTENTSHARE",
    "WEBSITE_DNS_SERVER",
    "WEBSITE_MAX_DYNAMIC_APPLICATION_SCALE_OUT",
    "WEBSITE_NODE_DEFAULT_VERSION",
    "WEBSITE_RUN_FROM_PACKAGE",
    "WEBSITE_SKIP_CONTENTSHARE_VALIDATION",
    "WEBSITE_VNET_ROUTE_ALL",
}
DEPRECATED_PROPERTIES = {
    "alwaysOn",
    "containerSize",
    "dnsServers",
    "ftpsState",
    "isReserved",
    "IsXenon",
    "javaVersion",
    "LinuxFxVersion",
    "netFrameworkVersion",
    "powerShellVersion",
    "functionAppScaleLimit",
    "preWarmedInstanceCount",
    "use32BitWorkerProcess",
    "vnetBackupRestoreEnabled",
    "vnetContentShareEnabled",
    "vnetImagePullEnabled",
    "vnetRouteAllEnabled",
    "windowsFxVersion",
}


@pytest.fixture(scope="module")
def compiled() -> str:
    bicep = shutil.which("bicep")
    if bicep is None:
        if os.environ.get("REQUIRE_BICEP") == "1":
            pytest.fail("REQUIRE_BICEP=1 but no bicep on PATH")
        pytest.skip("no bicep on PATH")
    result = subprocess.run(
        [bicep, "build", "infra/main.bicep", "--stdout"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr  # no warnings either
    return result.stdout


@pytest.fixture(scope="module")
def template(compiled: str) -> dict:
    return json.loads(compiled)


def _resources(template: dict):
    """Every resource, nested module templates included (a list, or an object in language 2.0)."""
    resources = template.get("resources", [])
    for resource in resources.values() if isinstance(resources, dict) else resources:
        yield resource
        if resource["type"] == "Microsoft.Resources/deployments":
            yield from _resources(resource["properties"]["template"])


def _of_type(template: dict, type_: str) -> list[dict]:
    return [r for r in _resources(template) if r["type"] == type_]


def _app(template: dict) -> dict:
    """The compiled app.bicep: the module template that defines the function app."""
    (app,) = [
        d["properties"]["template"]
        for d in _of_type(template, "Microsoft.Resources/deployments")
        if any(r["type"] == "Microsoft.Web/sites" for r in _resources(d["properties"]["template"]))
    ]
    return app


def _last_name(resource: dict) -> str:
    """The last name segment: a literal, or the last quoted argument of a format() call."""
    name = resource["name"]
    if not name.startswith("["):
        return name.rsplit("/", 1)[-1]
    match = re.search(r"'([^']*)'\)\]$", name)
    assert match, name
    return match.group(1)


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


def test_storage_accounts_refuse_shared_keys_and_public_blobs(template):
    accounts = _of_type(template, "Microsoft.Storage/storageAccounts")
    assert len(accounts) == 2
    for account in accounts:
        assert account["kind"] == "StorageV2"
        assert account["sku"] == {"name": "Standard_LRS"}
        properties = account["properties"]
        assert properties["allowSharedKeyAccess"] is False
        assert properties["allowBlobPublicAccess"] is False
        assert properties["minimumTlsVersion"] == "TLS1_2"
        assert properties["supportsHttpsTrafficOnly"] is True
        assert properties["defaultToOAuthAuthentication"] is True
        assert properties["allowCrossTenantReplication"] is False


def test_containers_are_private_and_page_versions_expire(template):
    containers = _of_type(template, "Microsoft.Storage/storageAccounts/blobServices/containers")
    assert sorted(_last_name(c) for c in containers) == ["config", "deployments", "site"]
    assert all(c["properties"] == {"publicAccess": "None"} for c in containers)

    services = [
        s
        for s in _of_type(template, "Microsoft.Storage/storageAccounts/blobServices")
        if s.get("properties", {}).get("isVersioningEnabled") is True
    ]
    assert len(services) == 1
    assert services[0]["properties"]["deleteRetentionPolicy"] == {"enabled": False}
    assert services[0]["properties"]["containerDeleteRetentionPolicy"] == {"enabled": False}

    (policy,) = _of_type(template, "Microsoft.Storage/storageAccounts/managementPolicies")
    assert policy["properties"]["policy"]["rules"] == [
        {
            "name": "expire-previous-versions",
            "enabled": True,
            "type": "Lifecycle",
            "definition": {
                "actions": {"version": {"delete": {"daysAfterCreationGreaterThan": 1}}},
                "filters": {"blobTypes": ["blockBlob"], "prefixMatch": ["site/", "config/"]},
            },
        }
    ]


def _label(expression: str, labels: dict[str, str]) -> str:
    found = [label for marker, label in labels.items() if marker in expression]
    assert len(found) == 1, expression
    return found[0]


def test_role_assignments_are_exactly_the_designed_set(template, compiled):
    scopes = {
        "storageAccounts', format('dashhost": "host account",
        "'default', 'site')": "site container",
        "'default', 'config')": "config container",
        "'Microsoft.Insights/components'": "application insights",
        "'Microsoft.OperationalInsights/workspaces'": "workspace",
    }
    principals = {
        "userAssignedIdentities', 'scan')": "scan",
        "parameters('principalId')": "scan",  # reader.bicep, given the scan identity below
        "'scan-failed')": "scan-failed rule",
        "'scan-missing')": "scan-missing rule",
        "'scan-incomplete')": "scan-incomplete rule",
    }
    assignments = Counter()
    for assignment in _of_type(template, "Microsoft.Authorization/roleAssignments"):
        properties = assignment["properties"]
        role = ROLE.fullmatch(properties["roleDefinitionId"])
        assert role, properties["roleDefinitionId"]
        scope = _label(assignment["scope"], scopes) if "scope" in assignment else "subscription"
        assignments[role.group(1), scope, _label(properties["principalId"], principals)] += 1
        assert properties["principalType"] == "ServicePrincipal"
    # Roles: Reader 4 (one per scanned subscription, here the module's one, and three alert rules),
    # and one each of the five data and telemetry roles. The auth identity gets none.
    assert assignments == {
        ("Reader", "subscription", "scan"): 1,
        ("Storage Blob Data Owner", "host account", "scan"): 1,
        ("Storage Table Data Contributor", "host account", "scan"): 1,
        ("Storage Blob Data Contributor", "site container", "scan"): 1,
        ("Storage Blob Data Reader", "config container", "scan"): 1,
        ("Monitoring Metrics Publisher", "application insights", "scan"): 1,
        ("Reader", "workspace", "scan-failed rule"): 1,
        ("Reader", "workspace", "scan-missing rule"): 1,
        ("Reader", "workspace", "scan-incomplete rule"): 1,
    }
    (readers,) = [
        d
        for d in _of_type(template, "Microsoft.Resources/deployments")
        if "principalId" in d["properties"].get("parameters", {})
    ]
    assert readers["subscriptionId"] == "[parameters('scanSubscriptionIds')[copyIndex()]]"
    assert readers["properties"]["parameters"]["principalId"] == {
        "value": "[reference('app').outputs.scanPrincipalId.value]"
    }
    scan_output = _app(template)["outputs"]["scanPrincipalId"]["value"]
    assert "userAssignedIdentities', 'scan')" in scan_output
    assert not re.search(
        r"roleDefinitions\('(Owner|Contributor|User Access Administrator)'\)", compiled
    )


def _identity_id(name: str) -> str:
    return f"resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', '{name}')"


def test_identities_are_exactly_scan_and_auth(template):
    identities = _of_type(template, "Microsoft.ManagedIdentity/userAssignedIdentities")
    assert sorted(i["name"] for i in identities) == ["auth", "scan"]
    # The sign-in federated credential lives on the app registration, outside this template.
    assert not [r for r in _resources(template) if "federatedIdentityCredentials" in r["type"]]
    (site,) = _of_type(template, "Microsoft.Web/sites")
    assert site["identity"] == {
        "type": "UserAssigned",
        "userAssignedIdentities": {
            f"[format('{{0}}', {_identity_id('scan')})]": {},
            f"[format('{{0}}', {_identity_id('auth')})]": {},
        },
    }
    storage = site["properties"]["functionAppConfig"]["deployment"]["storage"]
    assert (
        storage["authentication"]["userAssignedIdentityResourceId"] == f"[{_identity_id('scan')}]"
    )


def test_app_settings_are_exactly_the_designed_ones(template):
    (site,) = _of_type(template, "Microsoft.Web/sites")
    settings = site["properties"]["siteConfig"]["appSettings"]
    names = [s["name"] for s in settings]
    assert sorted(names) == sorted(
        [
            "AzureWebJobsStorage__accountName",
            "AzureWebJobsStorage__credential",
            "AzureWebJobsStorage__clientId",
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            "APPLICATIONINSIGHTS_AUTHENTICATION_STRING",
            "OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID",
            "AzureWebJobs.scan.Disabled",
            "DASHBOARD_DATA_ACCOUNT",
            "DASHBOARD_SCAN_CLIENT_ID",
        ]
    )
    assert not DEPRECATED_SETTINGS & set(names)
    # The kill switch: scheduleEnabled false disables the scan.
    (switch,) = [s for s in settings if s["name"] == "AzureWebJobs.scan.Disabled"]
    assert switch["value"] == "[if(parameters('scheduleEnabled'), 'false', 'true')]"
    assert "AzureWebJobsStorage" not in names  # the key-based connection
    assert "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET" not in names
    assert not {k.lower() for k in DEPRECATED_PROPERTIES} & {
        k.lower() for k in _keys(site["properties"])
    }


def test_no_keys_anywhere(compiled):
    for word in ("listKeys", "AccountKey", "primaryKey"):
        assert word.lower() not in compiled.lower(), word


def test_sign_in_is_conditional_and_strict(template):
    (auth,) = [
        c
        for c in _of_type(template, "Microsoft.Web/sites/config")
        if _last_name(c) == "authsettingsV2"
    ]
    assert auth["condition"] == "[not(empty(parameters('authClientId')))]"
    properties = auth["properties"]
    assert properties["globalValidation"] == {
        "requireAuthentication": True,
        "unauthenticatedClientAction": "RedirectToLoginPage",
    }
    assert properties["login"]["tokenStore"] == {"enabled": False}
    assert properties["httpSettings"]["requireHttps"] is True
    aad = properties["identityProviders"]["azureActiveDirectory"]
    assert aad["enabled"] is True
    assert aad["registration"]["clientId"] == "[parameters('authClientId')]"
    assert (
        aad["registration"]["clientSecretSettingName"] == "OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID"
    )
    assert aad["validation"]["defaultAuthorizationPolicy"]["allowedApplications"] == [
        "[parameters('authClientId')]"
    ]


def test_monitoring(template):
    (insights,) = _of_type(template, "Microsoft.Insights/components")
    assert insights["properties"]["DisableLocalAuth"] is True

    (workspace,) = _of_type(template, "Microsoft.OperationalInsights/workspaces")
    assert workspace["properties"]["retentionInDays"] == 30
    assert workspace["properties"]["features"] == {"disableLocalAuth": True}
    (tables,) = _of_type(template, "Microsoft.OperationalInsights/workspaces/tables")
    assert tables["copy"]["count"] == "[length(variables('appTables'))]"
    assert _app(template)["variables"]["appTables"] == APP_TABLES
    assert tables["properties"] == {"retentionInDays": 30, "totalRetentionInDays": 30}

    rules = {r["name"]: r for r in _of_type(template, "Microsoft.Insights/scheduledQueryRules")}
    assert sorted(rules) == ["scan-failed", "scan-incomplete", "scan-missing"]
    expected = {
        "scan-failed": (1, "PT1H", "GreaterThan", 0),
        "scan-missing": (2, "P2D", "LessThan", 1),
        "scan-incomplete": (3, "PT1H", "GreaterThan", 0),
    }
    for name, (severity, window, operator, threshold) in expected.items():
        rule = rules[name]
        assert rule["identity"] == {"type": "SystemAssigned"}
        properties = rule["properties"]
        assert properties["severity"] == severity
        assert properties["windowSize"] == window
        assert properties["evaluationFrequency"] == "PT1H"
        (criterion,) = properties["criteria"]["allOf"]
        assert criterion["operator"] == operator
        assert criterion["threshold"] == threshold
        assert criterion["timeAggregation"] == "Total"
    assert "ago(26h)" in rules["scan-missing"]["properties"]["criteria"]["allOf"][0]["query"]
    assert rules["scan-missing"]["properties"]["enabled"] == "[parameters('scheduleEnabled')]"
    assert rules["scan-failed"]["properties"]["enabled"] is True
    assert rules["scan-incomplete"]["properties"]["enabled"] is True

    (group,) = _of_type(template, "Microsoft.Insights/actionGroups")
    assert group["properties"]["emailReceivers"].startswith("[if(empty(parameters('alertEmail'))")


def test_flex_app_shape(template):
    (plan,) = _of_type(template, "Microsoft.Web/serverfarms")
    assert plan["sku"] == {"name": "FC1", "tier": "FlexConsumption"}
    assert plan["properties"]["reserved"] is True
    (site,) = _of_type(template, "Microsoft.Web/sites")
    assert site["kind"] == "functionapp,linux"
    assert site["properties"]["httpsOnly"] is True
    assert site["properties"]["siteConfig"]["minTlsVersion"] == "1.2"
    config = site["properties"]["functionAppConfig"]
    assert config["runtime"] == {"name": "python", "version": "3.14"}
    assert config["scaleAndConcurrency"] == {"instanceMemoryMB": 2048, "maximumInstanceCount": 2}
    storage = config["deployment"]["storage"]
    assert storage["type"] == "blobContainer"
    assert storage["authentication"]["type"] == "UserAssignedIdentity"
    (scm,) = _of_type(template, "Microsoft.Web/sites/basicPublishingCredentialsPolicies")
    assert _last_name(scm) == "scm"
    assert scm["properties"] == {"allow": False}


def test_dashboard_group_name_matches_settings(template):
    settings = json.loads((REPO / "settings.json").read_text(encoding="utf-8"))
    default = template["parameters"]["resourceGroupName"]["defaultValue"]
    assert default == settings["dashboard_resource_group"]

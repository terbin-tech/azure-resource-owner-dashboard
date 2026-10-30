"""The Functions entry point: the scan timer and the page HTTP function.

Timer trigger (NCRONTAB in UTC; "Don't set runOnStartup to true in production"):
https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-timer?pivots=programming-language-python&tabs=python-v2
HTTP trigger ("When a level isn't explicitly set, authorization defaults to the function level";
sign-in is App Service's, so the level is anonymous):
https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-http-webhook-trigger?pivots=programming-language-python&tabs=python-v2
"""

import functools
import os
from datetime import UTC, datetime
from pathlib import Path

import azure.functions as func

from dashboard import deployed, web
from dashboard.clients import Clients, make_clients

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

SCHEDULE = "0 0 6,16 * * *"  # 06:00 and 16:00 UTC
SETTINGS = Path(__file__).with_name("settings.json")


# Built once per process; nothing here reaches the network until a client sends a request.
@functools.cache
def _deployed() -> deployed.Deployed:
    return deployed.deployed_from_env(os.environ)


@functools.cache
def _clients() -> Clients:
    return make_clients(deployed.make_credential(_deployed()))


@app.function_name(name="scan")
@app.timer_trigger(schedule=SCHEDULE, arg_name="timer", run_on_startup=False)
def scan(timer: func.TimerRequest) -> None:
    deployed.scheduled_run(os.environ, SETTINGS, datetime.now(UTC))


@app.function_name(name="page")
@app.route(route="page", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def page(req: func.HttpRequest) -> func.HttpResponse:
    r = web.serve(
        req.headers,
        _clients(),
        web.page_reader(_clients(), _deployed().data_account),
        auth_enabled=os.environ.get("WEBSITE_AUTH_ENABLED", "").casefold() == "true",
    )
    # mimetype "defaults to 'text/plain'":
    # https://learn.microsoft.com/en-us/python/api/azure-functions/azure.functions.httpresponse?view=azure-python
    return func.HttpResponse(
        r.body, status_code=r.status, headers=r.headers, mimetype="text/html", charset="utf-8"
    )

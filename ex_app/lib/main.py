"""Windmill as an ExApp"""

import asyncio
import contextlib
import json
import os
import random
import re
import string
import typing
from base64 import b64decode
from contextlib import asynccontextmanager
from pathlib import Path
from time import sleep

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Request, responses
from nc_py_api import NextcloudApp, NextcloudException
from nc_py_api.ex_app import LogLvl, nc_app, persistent_storage, run_app
from nc_py_api.ex_app.integration_fastapi import AppAPIAuthMiddleware, fetch_models_task
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, Response

# os.environ["NEXTCLOUD_URL"] = "http://nextcloud.local/index.php"
# os.environ["APP_HOST"] = "0.0.0.0"
# os.environ["APP_ID"] = "windmill_app"
# os.environ["APP_SECRET"] = "12345"  # noqa
# os.environ["APP_PORT"] = "23001"


DEFAULT_USER_EMAIL = "admin@windmill.dev"
USERS_STORAGE_PATH = Path(persistent_storage()).joinpath("windmill_users_config.json")
USERS_STORAGE = {}
print("[DEBUG]: USERS_STORAGE_PATH=", str(USERS_STORAGE_PATH), flush=True)
if USERS_STORAGE_PATH.exists():
    with open(USERS_STORAGE_PATH, encoding="utf-8") as __f:
        USERS_STORAGE.update(json.load(__f))


def get_user_email(user_name: str) -> str:
    return f"{user_name}@windmill.dev"


def add_user_to_storage(user_email: str, password: str, token: str = "") -> None:
    USERS_STORAGE[user_email] = {"password": password, "token": token}
    with open(USERS_STORAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(USERS_STORAGE, f, indent=4)


async def create_user(user_name: str) -> str:
    password = generate_random_string()
    user_email = get_user_email(user_name)
    async with httpx.AsyncClient() as client:
        r = await client.request(
            method="POST",
            url="http://127.0.0.1:8000/api/users/create",
            json={
                "email": user_email,
                "password": password,
                "super_admin": True,
                "name": user_name,
            },
            cookies={"token": USERS_STORAGE["admin@windmill.dev"]["token"]},
        )
        r = await client.post(
            url="http://127.0.0.1:8000/api/auth/login",
            json={"email": user_email, "password": password},
        )
        add_user_to_storage(user_email, password, r.text)
    return r.text


async def login_user(user_email: str, password: str) -> str:
    print("login_user:DEBUG:", user_email, flush=True)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            url="http://127.0.0.1:8000/api/auth/login",
            json={"email": user_email, "password": password},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"login_user: {r.text}")
        return r.text


def login_user_sync(user_email: str, password: str) -> str:
    with httpx.Client() as client:
        r = client.post(
            url="http://127.0.0.1:8000/api/auth/login",
            json={"email": user_email, "password": password},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"login_user: {r.text}")
        return r.text


async def check_token(token: str) -> bool:
    async with httpx.AsyncClient() as client:
        r = await client.get("http://127.0.0.1:8000/api/users/whoami", cookies={"token": token})
        return bool(r.status_code < 400)


def check_token_sync(token: str) -> bool:
    with httpx.Client() as client:
        r = client.get("http://127.0.0.1:8000/api/users/whoami", cookies={"token": token})
        return bool(r.status_code < 400)


def get_valid_user_token_sync(user_email: str) -> str:
    token = USERS_STORAGE[user_email]["token"]
    if check_token_sync(token):
        return token
    user_password = USERS_STORAGE[user_email]["password"]
    token = login_user_sync(user_email, user_password)
    add_user_to_storage(user_email, user_password, token)
    return token


async def provision_user(request: Request, create_missing_user: bool) -> None:
    if "token" in request.cookies:
        print(f"DEBUG: TOKEN IS PRESENT: {request.cookies['token']}", flush=True)
        if (await check_token(request.cookies["token"])) is True:
            return
        print(f"DEBUG: TOKEN IS INVALID: {request.cookies['token']}", flush=True)

    user_name = get_windmill_username_from_request(request)
    if not user_name:
        print("WARNING: provision_user: `username` is missing in the request to ExApp.", flush=True)
        print("[DEBUG]: ", request.headers, flush=True)
        return
    user_email = get_user_email(user_name)
    if user_email in USERS_STORAGE:
        windmill_token_valid = await check_token(USERS_STORAGE[user_email]["token"])
        if not USERS_STORAGE[user_email]["token"] or windmill_token_valid is False:
            if not create_missing_user:
                print("WARNING: Do not creating user due to specified flag.", flush=True)
                return
            user_password = USERS_STORAGE[user_email]["password"]
            add_user_to_storage(user_email, user_password, await login_user(user_email, user_password))
    else:
        await create_user(user_name)
    request.cookies["token"] = USERS_STORAGE[user_email]["token"]
    print(f"DEBUG: ADDING TOKEN({request.cookies['token']}) to request", flush=True)


RATE_LIMIT_DICT = {}
PROTECTED_URLS = [
    r"^/api/w/nextcloud/jobs/.*",
]


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        print(request.url.path, flush=True)
        x_origin_ip = request.headers.get("X-Origin-IP")
        request_path = request.url.path
        key = (x_origin_ip, request_path)
        if key in RATE_LIMIT_DICT:
            delay = min(5, RATE_LIMIT_DICT[key])  # Maximum delay of 5 seconds
            await asyncio.sleep(delay)

        response = await call_next(request)

        for pattern in PROTECTED_URLS:
            if re.match(pattern, request_path):
                if response.status_code == 401:
                    if key in RATE_LIMIT_DICT:
                        RATE_LIMIT_DICT[key] += 1
                    else:
                        RATE_LIMIT_DICT[key] = 1
                elif key in RATE_LIMIT_DICT and response.status_code < 400:
                    del RATE_LIMIT_DICT[key]  # remove RateLimit if action was successful
                break

        return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _t = asyncio.create_task(start_background_webhooks_syncing())  # noqa
    yield


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)  # set global AppAPI authentication middleware
APP.add_middleware(RateLimitMiddleware)


def get_windmill_username_from_request(request: Request) -> str:
    auth_aa = b64decode(request.headers.get("AUTHORIZATION-APP-API", "")).decode("UTF-8")
    try:
        username, _ = auth_aa.split(":", maxsplit=1)
    except ValueError:
        username = ""
    if not username:
        return ""
    return "wapp_" + username


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    print(f"enabled={enabled}")
    if enabled:
        nc.log(LogLvl.WARNING, f"Hello from {nc.app_cfg.app_name} :)")
        nc.ui.resources.set_script("top_menu", "windmill_app", "ex_app/js/windmill_app-main")
        nc.ui.top_menu.register("windmill_app", "Workflow Engine", "ex_app/img/app.svg", True)
    else:
        nc.log(LogLvl.WARNING, f"Bye bye from {nc.app_cfg.app_name} :(")
        nc.ui.resources.delete_script("top_menu", "windmill_app", "ex_app/js/windmill_app-main")
        nc.ui.top_menu.unregister("windmill_app")
    return ""


@APP.get("/heartbeat")
async def heartbeat_callback():
    return responses.JSONResponse(content={"status": "ok"})


@APP.post("/init")
async def init_callback(b_tasks: BackgroundTasks, nc: typing.Annotated[NextcloudApp, Depends(nc_app)]):
    b_tasks.add_task(fetch_models_task, nc, {}, 0)
    return responses.JSONResponse(content={})


@APP.put("/enabled")
def enabled_callback(enabled: bool, nc: typing.Annotated[NextcloudApp, Depends(nc_app)]):
    return responses.JSONResponse(content={"error": enabled_handler(enabled, nc)})


async def proxy_request_to_windmill(request: Request, path: str, path_prefix: str = ""):
    async with httpx.AsyncClient() as client:
        url = f"http://127.0.0.1:8000{path_prefix}/{path}"
        headers = {key: value for key, value in request.headers.items() if key.lower() not in ("host", "cookie")}
        if request.method == "GET":
            response = await client.get(
                url,
                params=request.query_params,
                cookies=request.cookies,
                headers=headers,
            )
        else:
            response = await client.request(
                method=request.method,
                url=url,
                params=request.query_params,
                headers=headers,
                cookies=request.cookies,
                content=await request.body(),
            )
        print(
            "proxy_request_to_windmill: ",
            f"method={request.method}, path={path}, path_prefix={path_prefix}, status={response.status_code}",
            flush=True,
        )
        response_header = dict(response.headers)
        response_header.pop("transfer-encoding", None)
        return Response(content=response.content, status_code=response.status_code, headers=response_header)


@APP.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def proxy_backend_requests(request: Request, path: str):
    print(f"proxy_BACKEND_requests: {path} - {request.method}\nCookies: {request.cookies}", flush=True)
    await provision_user(request, False)
    return await proxy_request_to_windmill(request, path, "/api")


@APP.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def proxy_frontend_requests(request: Request, path: str):
    print(f"proxy_FRONTEND_requests: {path} - {request.method}\nCookies: {request.cookies}", flush=True)
    await provision_user(request, True)
    if path == "index.php/apps/app_api/proxy/windmill_app/":
        raise ValueError("DEBUG: remove before release if no triggering of it")
        # path = path.replace("index.php/apps/app_api/proxy/windmill_app/", "")
    file_server_path = ""
    if path.startswith("ex_app"):
        file_server_path = Path("../../" + path)
    elif not path:
        file_server_path = Path("/static_frontend/200.html").joinpath(path)
    elif Path("/static_frontend/").joinpath(path).is_file():
        file_server_path = Path("/static_frontend/").joinpath(path)

    if file_server_path:
        print("proxy_FRONTEND_requests: <OK> Returning: ", str(file_server_path), flush=True)
        response = FileResponse(str(file_server_path))
    else:
        print(f"proxy_FRONTEND_requests: <LOCAL FILE MISSING> Routing({path}) to the backend", flush=True)
        response = await proxy_request_to_windmill(request, path)
    response.headers["content-security-policy"] = "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:;"
    return response


def initialize_windmill() -> None:
    if not USERS_STORAGE_PATH.exists():
        while True:  # Let's wait until Windmill opens the port.
            with contextlib.suppress(httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError):
                r = httpx.get("http://127.0.0.1:8000/api/users/whoami")
                if r.status_code in (401, 403):
                    break
        r = httpx.post(
            url="http://127.0.0.1:8000/api/auth/login", json={"email": "admin@windmill.dev", "password": "changeme"}
        )
        if r.status_code >= 400:
            raise RuntimeError(f"initialize_windmill: can not login with default credentials, {r.text}")
        default_token = r.text
        new_default_password = generate_random_string()
        r = httpx.post(
            url="http://127.0.0.1:8000/api/users/setpassword",
            json={"password": new_default_password},
            cookies={"token": default_token},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"initialize_windmill: can not change default credentials password, {r.text}")
        add_user_to_storage(DEFAULT_USER_EMAIL, new_default_password, default_token)
        r = httpx.post(
            url="http://127.0.0.1:8000/api/users/tokens/create",
            json={"label": "NC_PERSISTENT"},
            cookies={"token": default_token},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"initialize_windmill: can not create persistent token, {r.text}")
        default_token = r.text
        add_user_to_storage(DEFAULT_USER_EMAIL, new_default_password, default_token)
        r = httpx.post(
            url="http://127.0.0.1:8000/api/workspaces/create",
            json={"id": "nextcloud", "name": "nextcloud"},
            cookies={"token": default_token},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"initialize_windmill: can not create default workspace, {r.text}")
        r = httpx.post(
            url="http://127.0.0.1:8000/api/w/nextcloud/workspaces/edit_auto_invite",
            json={"operator": False, "invite_all": True, "auto_add": True},
            cookies={"token": default_token},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"initialize_windmill: can not create default workspace, {r.text}")


def generate_random_string(length=10):
    letters = string.ascii_letters + string.digits  # You can include other characters if needed
    return "".join(random.choice(letters) for i in range(length))  # noqa


async def start_background_webhooks_syncing():
    await asyncio.to_thread(webhooks_syncing)


def webhooks_syncing():
    while True:
        try:
            _webhooks_syncing()
        except Exception as e:
            print(f"webhooks_syncing: Exception occurred! Info: {e}")
            sleep(60)


def _webhooks_syncing():
    workspace = "nextcloud"

    while True:
        nc = NextcloudApp()
        if not nc.enabled_state:
            print("ExApp is disabled, sleeping for 5 minutes")
            sleep(5 * 60)
            continue
        print("Running workflow sync")
        token = get_valid_user_token_sync(DEFAULT_USER_EMAIL)
        flow_paths = get_flow_paths(workspace, token)
        print("webhooks_syncing(flow_paths):\n", flow_paths, flush=True)
        expected_listeners = get_expected_listeners(workspace, token, flow_paths)
        print("webhooks_syncing(expected_listeners):\n", json.dumps(expected_listeners, indent=4), flush=True)
        registered_listeners = get_registered_listeners()
        print("get_registered_listeners: ", json.dumps(registered_listeners, indent=4), flush=True)
        for expected_listener in expected_listeners:
            expected_listener["filters"] = _preprocess_webhook_event_filter(expected_listener["filters"])
            registered_listeners_for_uri = get_registered_listeners_for_uri(
                expected_listener["webhook"], registered_listeners
            )
            for event in expected_listener["events"]:
                listener = next(filter(lambda listener: listener["event"] == event, registered_listeners_for_uri), None)
                listener["eventFilter"] = _preprocess_webhook_event_filter(listener["eventFilter"])
                if listener is not None:
                    if listener["eventFilter"] != expected_listener["filters"]:
                        print("webhooks_syncing: before update_listener:", json.dumps(listener))
                        update_listener(listener, expected_listener["filters"], token)
                else:
                    register_listener(event, expected_listener["filters"], expected_listener["webhook"], token)
        for registered_listener in registered_listeners:
            if registered_listener["appId"] == nc.app_cfg.app_name:  # noqa
                if (
                    next(
                        filter(
                            lambda expected_listener: registered_listener["uri"] == expected_listener["webhook"]
                            and registered_listener["event"] in expected_listener["events"],
                            expected_listeners,
                        ),
                        None,
                    )
                    is None
                ):
                    delete_listener(registered_listener)
        sleep(30)


def _preprocess_webhook_event_filter(event_filter):
    if event_filter in (None, {}):
        return []
    return event_filter


def get_flow_paths(workspace: str, token: str) -> list[str]:
    method = "GET"
    path = f"w/{workspace}/flows/list"
    print(f"sync_API_request: {path} - {method}", flush=True)
    flow_paths = []
    with httpx.Client() as client:
        url = f"http://127.0.0.1:8000/api/{path}"
        headers = {"Authorization": f"Bearer {token}"}
        response = client.request(
            method=method,
            url=url,
            params={"per_page": 100},
            headers=headers,
        )
        print(
            f"sync_API_request: method={method}, path={path}, status={response.status_code}",
            flush=True,
        )
        try:
            response_data = json.loads(response.content)
            for flow in response_data:
                flow_paths.append(flow["path"])
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
    return flow_paths


def get_expected_listeners(workspace: str, token: str, flow_paths: list[str]) -> list[dict]:
    flows = []
    for flow_path in flow_paths:
        with httpx.Client() as client:
            method = "GET"
            path = f"w/{workspace}/flows/get/{flow_path}"
            url = f"http://127.0.0.1:8000/api/{path}"
            headers = {"Authorization": f"Bearer {token}"}
            print(f"get_expected_listeners: {path} - {method}", flush=True)
            response = client.request(
                method=method,
                url=url,
                params={"per_page": 100},
                headers=headers,
            )
            print(
                f"get_expected_listeners: method={method}, path={path}, status={response.status_code}",
                flush=True,
            )
            try:
                response_data = json.loads(response.content)
            except json.JSONDecodeError as e:
                print(f"get_expected_listeners: Error parsing JSON: {e}")
                return []
            first_module = response_data["value"]["modules"][0]
            if (
                first_module.get("summary", "") == "CORE:LISTEN_TO_EVENT"
                and first_module["value"]["input_transforms"]["events"]["type"] == "static"
                and first_module["value"]["input_transforms"]["filters"]["type"] == "static"
            ):
                webhook = f"/api/w/{workspace}/jobs/run/f/{flow_path}"
                # webhook = f"https://app.windmill.dev/api/w/{workspace}/jobs/run/f/{flow_path}"
                input_transforms = first_module["value"]["input_transforms"]
                flows.append(
                    {
                        "webhook": webhook,
                        "filters": input_transforms["filters"]["value"],
                        # Remove backslashes from the beginning to yield canonical reference
                        "events": [
                            event[1:] if event.startswith("\\") else event
                            for event in input_transforms["events"]["value"]
                        ],
                    }
                )
    return flows


def get_registered_listeners_for_uri(webhook: str, registered_listeners: list) -> list:
    return [listener for listener in registered_listeners if listener["uri"] == webhook]


def register_listener(event, event_filter, webhook, token: str) -> dict:
    auth_data = {
        "Authorization": f"Bearer {token}",
    }
    nc = NextcloudApp()
    print(f"register_listener: {webhook} - {event}", flush=True)
    print(json.dumps(event_filter, indent=4), flush=True)
    try:
        r = nc.webhooks.register(
            "POST", webhook, event, event_filter=event_filter, auth_method="header", auth_data=auth_data
        )
    except NextcloudException as e:
        print(f"Exception during registering webhook: {e}", flush=True)
        return {}
    print("register_listener:\n", json.dumps(r._raw_data, indent=4), flush=True)  # noqa
    return r._raw_data  # noqa


def update_listener(registered_listener: dict, event_filter, token: str) -> dict:
    auth_data = {
        "Authorization": f"Bearer {token}",
    }
    nc = NextcloudApp()
    print(f"update_listener: {registered_listener['uri']} - {registered_listener['event']}", flush=True)
    print(json.dumps(event_filter, indent=4), flush=True)
    try:
        r = nc.webhooks.update(
            registered_listener["id"],
            "POST",
            registered_listener["uri"],
            registered_listener["event"],
            event_filter=event_filter,
            auth_method="header",
            auth_data=auth_data,
        )
    except NextcloudException as e:
        print(f"Exception during updating webhook: {e}", flush=True)
        return {}
    print("update_listener:\n", json.dumps(r._raw_data, indent=4), flush=True)  # noqa
    return r._raw_data  # noqa


def get_registered_listeners():
    nc = NextcloudApp()
    r = nc.ocs("GET", "/ocs/v1.php/apps/webhook_listeners/api/v1/webhooks")
    for i in r:  # we need the same format as in `get_expected_listeners(workspace, token, flow_paths)`
        if not i["eventFilter"]:
            i["eventFilter"] = None  # replace [] with None
    return r


def delete_listener(registered_listener: dict) -> bool:
    r = NextcloudApp().webhooks.unregister(registered_listener["id"])
    if r:
        print("delete_listener: removed registered listener with id=%d", registered_listener["id"], flush=True)
    return r


if __name__ == "__main__":
    initialize_windmill()
    # Current working dir is set for the Service we are wrapping, so change we first for ExApp default one
    os.chdir(Path(__file__).parent)
    run_app(APP, log_level="info")  # Calling wrapper around `uvicorn.run`.

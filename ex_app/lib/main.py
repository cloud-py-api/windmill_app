"""Windmill as ExApp"""

import os
import typing
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import json
import base64

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Request, responses
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl, nc_app, run_app
from nc_py_api.ex_app.integration_fastapi import fetch_models_task
from starlette.responses import FileResponse, Response

# os.environ["NEXTCLOUD_URL"] = "http://nextcloud.local/index.php"
# os.environ["APP_HOST"] = "0.0.0.0"
# os.environ["APP_ID"] = "windmill_app"
# os.environ["APP_SECRET"] = "12345"
# os.environ["APP_PORT"] = "23000"


@asynccontextmanager
async def lifespan(app: FastAPI):  # pylint: disable=unused-argument
    sync_timer()
    yield


APP = FastAPI(lifespan=lifespan)
# APP.add_middleware(AppAPIAuthMiddleware)  # set global AppAPI authentication middleware


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    print(f"enabled={enabled}")
    if enabled:
        nc.log(LogLvl.WARNING, f"Hello from {nc.app_cfg.app_name} :)")
        nc.ui.resources.set_script("top_menu", "windmill_app", "js/windmill_app-main")
        nc.ui.top_menu.register("windmill_app", "Workflow Engine", "img/app.svg")
    else:
        nc.log(LogLvl.WARNING, f"Bye bye from {nc.app_cfg.app_name} :(")
        nc.ui.resources.delete_script("top_menu", "windmill_app", "js/windmill_app-main")
        nc.ui.top_menu.unregister("windmill_app")
    return ""


@APP.get("/heartbeat")
async def heartbeat_callback():
    return responses.JSONResponse(content={"status": "ok"})


@APP.post("/init")
async def init_callback(b_tasks: BackgroundTasks, nc: typing.Annotated[NextcloudApp, Depends(nc_app)]):
    b_tasks.add_task(fetch_models_task, nc, {}, 0)
    return responses.JSONResponse(content={})


app_enabled = false
@APP.put("/enabled")
def enabled_callback(enabled: bool, nc: typing.Annotated[NextcloudApp, Depends(nc_app)]):
    app_enabled = enabled
    return responses.JSONResponse(content={"error": enabled_handler(enabled, nc)})


@APP.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def proxy_backend_requests(request: Request, path: str):
    print(f"proxy_BACKEND_requests: {path} - {request.method}\nCookies: {request.cookies}", flush=True)
    async with httpx.AsyncClient() as client:
        url = f"http://127.0.0.1:8000/api/{path}"
        headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
        # print(f"proxy_BACKEND_requests: method={request.method}, path={path}, status={response.status_code}")
        response = await client.request(
            method=request.method,
            url=url,
            params=request.query_params,
            headers=headers,
            cookies=request.cookies,
            content=await request.body(),
        )
        print(
            f"proxy_BACKEND_requests: method={request.method}, path={path}, status={response.status_code}", flush=True
        )

        response_header = dict(response.headers)
        response_header.pop("transfer-encoding", None)
        response_to_nc = Response(content=response.content, status_code=response.status_code, headers=response_header)
        # TO-DO: here maybe it is not needed?
        abc = response.cookies
        for cookie in abc:
            response_to_nc.set_cookie(key=cookie[0], value=cookie[1])

        # TO-DO: here maybe it is not needed?
        response_to_nc.headers["content-security-policy"] = "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:;"
        response_to_nc.headers["Access-Control-Allow-Origin"] = "*"
        response_to_nc.headers["X-Permitted-Cross-Domain-Policies"] = "all"
        return response_to_nc


@APP.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def proxy_frontend_requests(request: Request, path: str):
    print(f"proxy_FRONTEND_requests: {path} - {request.method}\nCookies: {request.cookies}", flush=True)

    # 2024-06-13 10:27:55 proxy_FRONTEND_requests: index.php/apps/app_api/proxy/windmill_app/ - GET

    if path == "index.php/apps/app_api/proxy/windmill_app/":
        path = path.replace("index.php/apps/app_api/proxy/windmill_app/", "")
    if path.startswith(("img/", "js/")):
        # file_server_path = Path("../" + path)
        file_server_path = Path("/ex_app/" + path)
    elif not path or path == "user/login":
        # file_server_path = Path("../../windmill_tmp/frontend/build/200.html")
        file_server_path = Path("/iframe/200.html")
    else:
        # file_server_path = Path("../../windmill_tmp/frontend/build/").joinpath(path)
        file_server_path = Path("/iframe/").joinpath(path)
    if file_server_path.exists():
        media_type = None
        if str(file_server_path).endswith(".js"):
            media_type = "application/javascript"
        response = FileResponse(str(file_server_path), media_type=media_type)
        response.headers["content-security-policy"] = "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:;"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "all"
        print("proxy_FRONTEND_requests: <OK> Returning: ", str(file_server_path), flush=True)
        return response

    print("proxy_FRONTEND_requests: <BAD> FILE DOES NOT EXIST: ", file_server_path, flush=True)
    print("proxy_FRONTEND_TO_BACKEND_requests: Asking for reply from BACKEND..", flush=True)
    async with httpx.AsyncClient() as client:
        url = f"http://127.0.0.1:8000/{path}"
        headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
        response = await client.request(
            method=request.method,
            url=url,
            params=request.query_params,
            headers=headers,
            cookies=request.cookies,
            content=await request.body(),
        )
        print(
            f"proxy_FRONTEND_TO_BACKEND_requests: method={request.method}, path={path}, status={response.status_code}",
            flush=True,
        )
        response_to_nc = Response(
            content=response.content, status_code=response.status_code, headers=dict(response.headers)
        )
        abc = response.cookies
        for cookie in abc:
            response_to_nc.set_cookie(key=cookie[0], value=cookie[1])
        response_to_nc.headers["content-security-policy"] = "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:;"
        response_to_nc.headers["Access-Control-Allow-Origin"] = "*"
        response_to_nc.headers["X-Permitted-Cross-Domain-Policies"] = "all"
        return response_to_nc


async def sync_timer():
    while True:
        if app_enabled:
            print("Running workflow sync")
            workspace = 'YOUR_WORKSPACE'
            token = 'YOUR_TOKEN_HERE'
            flow_paths = await get_flow_paths(workspace, token)
            expected_listeners = await get_expected_listeners(workspace, token, flow_paths)
            print(json.dumps(expected_listeners, indent=4), flush=True)
            for expected_listener in expected_listeners:
                registered_listeners = await get_registered_listeners_for_uri(expected_listener['webhook'])
                for event in expected_listener['events']:
                    listener = next(filter(lambda listener: listener['event'] == event, registered_listeners), None)
                    if listener is not None:
                        json.dumps(listener)
                        if json.dumps(listener['eventFilter']) != json.dumps(expected_listener['filters']):
                            await update_listener(listener, expected_listener['filters'], token)
                    else:
                        await register_listener(event, expected_listener['filters'], expected_listener['webhook'], token)
            registered_listeners = await get_registered_listeners()
            for registered_listener in registered_listeners:
                if registered_listener['appId'] == os.environ["APP_ID"]:
                    if next(filter(lambda expected_listener: registered_listener['uri'] == expected_listener['webhook'] and registered_listener['event'] in expected_listener['events'], expected_listeners), None) is None:
                        await delete_listener(registered_listener)
        await asyncio.sleep(5 * 60)


async def get_flow_paths(workspace, token):
    method = 'GET'
    path = f"/w/{workspace}/flows/list"
    print(f"sync_API_request: {path} - {method}", flush=True)
    flow_paths = []
    async with httpx.AsyncClient() as client:
        url = f"http://127.0.0.1:8000/api/{path}"
        headers = {'Authorization': f"Bearer {token}"}
        # print(f"proxy_BACKEND_requests: method={request.method}, path={path}, status={response.status_code}")
        response = await client.request(
            method=method,
            url=url,
            params={'per_page': 100},
            headers=headers,
        )
        print(
            f"sync_API_request: method={method}, path={path}, status={response.status_code}",
            flush=True
        )
        try:
            response_data = json.loads(response.content)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            return
        for flow in response_data:
            flow_paths.append(flow['path'])
    return flow_paths


async def get_expected_listeners(workspace, token, flow_paths):
    flows = []
    for flow_path in flow_paths:
        async with httpx.AsyncClient() as client:
            method = 'GET'
            path = f"/w/{workspace}/flows/get/{flow_path}"
            url = f"http://127.0.0.1:8000/api/{path}"
            headers = {'Authorization': f"Bearer {token}"}
            print(f"sync_API_request: {path} - {method}", flush=True)
            response = await client.request(
                method=method,
                url=url,
                params={'per_page': 100},
                headers=headers,
            )
            print(
                f"sync_API_request: method={method}, path={path}, status={response.status_code}",
                flush=True
            )
            try:
                response_data = json.loads(response.content)
            except json.JSONDecodeError as e:
                print(f"Error parsing JSON: {e}")
                return
            first_module = response_data['value']['modules'][0]
            if first_module['summary'] == 'CORE:LISTEN_TO_EVENT' \
                    and first_module['value']['input_transforms']['events']['type'] == 'static' \
                    and first_module['value']['input_transforms']['filters']['type'] == 'static':
                # webhook = f"http://127.0.0.1:8000/api/w/{workspace}/jobs/run/f/{flow_path}"
                webhook = f"https://app.windmill.dev/api/w/{workspace}/jobs/run/f/{flow_path}"
                flows.append({
                    'webhook': webhook,
                    'filters': first_module['value']['input_transforms']['filters']['value'],
                    # Remove backslashes from the beginning to yield canonical reference
                    'events': [event[1:] if event.startswith('\\') else event for event in first_module['value']['input_transforms']['events']['value']],
                })
    return flows


async def get_registered_listeners_for_uri(webhook):
    async with httpx.AsyncClient() as client:
        method = 'GET'
        path = '/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks'
        url = os.environ["NEXTCLOUD_URL"]+path
        userid = os.environ["NEXTCLOUD_USERID"]
        secret = os.environ["NEXTCLOUD_SECRET"]
        headers = {
            'AUTHORIZATION-APP-API': base64.b64encode(bytes(f"{userid}:{secret}", 'utf-8')).decode('utf-8'),
            'OCS-APIREQUEST': 'true',
            'EX-APP-VERSION': 'v1.0.0',
            'EX-APP-ID': os.environ['APP_ID'],
            'AA-VERSION': 'v2.3.0'
        }
        print(f"sync_API_nextcloud_request: {url} - {method}", flush=True)
        response = await client.request(
            method=method,
            url=url,
            params={'uri': webhook, 'format': 'json'},
            headers=headers,
        )
        print(
            f"sync_API_nextcloud_request: method={method}, path={path}, status={response.status_code}",
            flush=True
        )
        try:
            response_data = json.loads(response.content)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            return
        print(json.dumps(response_data, indent=4), flush=True)
        return response_data['ocs']['data']


async def register_listener(event, filter, webhook, token):
    async with httpx.AsyncClient() as client:
        method = 'POST'
        path = '/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks'
        url = os.environ["NEXTCLOUD_URL"]+path
        userid = os.environ["NEXTCLOUD_USERID"]
        secret = os.environ["NEXTCLOUD_SECRET"]

        headers = {

            'AUTHORIZATION-APP-API': base64.b64encode(bytes(f"{userid}:{secret}", 'utf-8')).decode('utf-8'),
            'OCS-APIREQUEST': 'true',
            'EX-APP-VERSION': 'v1.0.0',
            'EX-APP-ID': os.environ['APP_ID'],
            'AA-VERSION': 'v2.3.0'
        }
        auth_data = {
            'Authorization': f"Bearer {token}"
        }
        print(f"sync_API_nextcloud_request: {url} - {method}", flush=True)
        print(json.dumps(filter, indent=4), flush=True)
        response = await client.request(
            method=method,
            url=url,
            json={'httpMethod': 'POST', 'uri': webhook, 'event': event, 'eventFilter': filter, 'authMethod': 'header', 'authData': auth_data},
            headers=headers,
            params={'format': 'json'}
        )
        print(
            f"sync_API_nextcloud_request: method={method}, path={path}, status={response.status_code}",
            flush=True
        )
        try:
            response_data = json.loads(response.content)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            return
        print(json.dumps(response_data, indent=4), flush=True)
        return response_data['ocs']['data']


async def update_listener(registered_listener, filter, token):
    async with httpx.AsyncClient() as client:
        method = 'POST'
        path = f"/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks/{registered_listener['id']}"
        url = os.environ["NEXTCLOUD_URL"] + path
        userid = os.environ["NEXTCLOUD_USERID"]
        secret = os.environ["NEXTCLOUD_SECRET"]

        headers = {
            'AUTHORIZATION-APP-API': base64.b64encode(bytes(f"{userid}:{secret}", 'utf-8')).decode('utf-8'),
            'OCS-APIREQUEST': 'true',
            'EX-APP-VERSION': 'v1.0.0',
            'EX-APP-ID': os.environ['APP_ID'],
            'AA-VERSION': 'v2.3.0'
        }
        auth_data = {
            'Authorization': f"Bearer {token}"
        }
        print(f"sync_API_nextcloud_request: {url} - {method}", flush=True)
        print(json.dumps(filter, indent=4), flush=True)
        response = await client.request(
            method=method,
            url=url,
            json={'httpMethod': 'POST', 'uri': registered_listener['uri'], 'event': registered_listener['event'], 'eventFilter': filter, 'authMethod': 'header',
                  'authData': auth_data},
            headers=headers,
            params={'format': 'json'}
        )
        print(
            f"sync_API_nextcloud_request: method={method}, path={path}, status={response.status_code}",
            flush=True
        )
        try:
            response_data = json.loads(response.content)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            return
        print(json.dumps(response_data, indent=4), flush=True)
        return response_data['ocs']['data']


async def get_registered_listeners():
    async with httpx.AsyncClient() as client:
        method = 'GET'
        path = '/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks'
        url = os.environ["NEXTCLOUD_URL"] + path
        userid = os.environ["NEXTCLOUD_USERID"]
        secret = os.environ["NEXTCLOUD_SECRET"]
        headers = {
            'AUTHORIZATION-APP-API': base64.b64encode(bytes(f"{userid}:{secret}", 'utf-8')).decode('utf-8'),
            'OCS-APIREQUEST': 'true',
            'EX-APP-VERSION': 'v1.0.0',
            'EX-APP-ID': os.environ['APP_ID'],
            'AA-VERSION': 'v2.3.0'
        }
        print(f"sync_API_nextcloud_request: {url} - {method}", flush=True)
        response = await client.request(
            method=method,
            url=url,
            params={'format': 'json'},
            headers=headers,
        )
        print(
            f"sync_API_nextcloud_request: method={method}, path={path}, status={response.status_code}",
            flush=True
        )
        try:
            response_data = json.loads(response.content)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            return
        print(json.dumps(response_data, indent=4), flush=True)
        return response_data['ocs']['data']


async def delete_listener(registered_listener):
    async with httpx.AsyncClient() as client:
        method = 'DELETE'
        path = f"/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks/{registered_listener['id']}"
        url = os.environ["NEXTCLOUD_URL"] + path
        userid = os.environ["NEXTCLOUD_USERID"]
        secret = os.environ["NEXTCLOUD_SECRET"]

        headers = {
            'AUTHORIZATION-APP-API': base64.b64encode(bytes(f"{userid}:{secret}", 'utf-8')).decode('utf-8'),
            'OCS-APIREQUEST': 'true',
            'EX-APP-VERSION': 'v1.0.0',
            'EX-APP-ID': os.environ['APP_ID'],
            'AA-VERSION': 'v2.3.0'
        }
        print(f"sync_API_nextcloud_request: {url} - {method}", flush=True)
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            params={'format': 'json'}
        )
        print(
            f"sync_API_nextcloud_request: method={method}, path={path}, status={response.status_code}",
            flush=True
        )
        try:
            response_data = json.loads(response.content)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            return
        print(json.dumps(response_data, indent=4), flush=True)
        return response_data['ocs']['data']

if __name__ == "__main__":
    # Current working dir is set for the Service we are wrapping, so change we first for ExApp default one
    os.chdir(Path(__file__).parent)
    run_app(APP, log_level="info")  # Calling wrapper around `uvicorn.run`.

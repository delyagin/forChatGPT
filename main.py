import sys
sys.dont_write_bytecode = True

import aiohttp
import aiohttp.web as web
import asyncio
import gc
import glob
import json
import mimetypes
import os
import platform
import random
import re
import socket
import sqlite3
import stat
import time
from contextlib import redirect_stdout
from io import StringIO

import db
import db2
import tclog
import utlog
# import telebot1 as bot
# import winshell
from win32com.client import Dispatch
import datetime
import shutil
import requests
import smtplib
from log_to_txt import save_log

MINIMUM_AGENT_VERSION = "0.5"
URL = "http://bugtrack/rest/"
client1_path = r"\\ryzen02-server\MUA\CLIENT_1"

## WEB SERVER UTILS

async def serve_static_file(request, document_root, path):
    document_root = os.path.abspath(document_root) + os.sep
    fullpath = os.path.abspath(os.path.join(document_root, path))
    try:
        if not fullpath.startswith(document_root):
            raise web.HTTPForbidden
        if not os.path.isfile(fullpath):
            raise web.HTTPNotFound
        return web.FileResponse(fullpath)
    except PermissionError:
        raise web.HTTPForbidden
    except FileNotFoundError:
        raise web.HTTPNotFound

## WEB SERVER HANDLERS

async def init(loop, address, service):
    app = web.Application(loop=loop)
    app.router.add_route("GET", "/", indexhandler)
    app.router.add_route("GET",
            "/{p:(build|machine|mgroup|product|result|rset|testrun|tsuite)/.*}",
            indexhandler)
    app.router.add_route("GET", "/api/client", clienthandler)
    app.router.add_route("POST", "/api/take-job", takejobhandler)
    app.router.add_route("POST", "/api/finish-job/{jobid}", finishjobhandler)
    app.router.add_route("POST", "/api/giveup-job/{jobid}", giveupjobhandler)
    app.router.add_route("POST", "/api/start-test-run", starttestrunhandler)
    app.router.add_route("POST", "/api/end-test-run/{id}", endtestrunhandler)
    app.router.add_route("GET", "/status/", statushandler)
    app.router.add_route("GET", "/{path:.*}", statichandler)
    server = await loop.create_server(app.make_handler(), address, service)
    for sock in server.sockets:
        print("listening on", sock.getsockname())
    return server

async def indexhandler(request):
    return (await serve_static_file(request, "data", "index.html"))

async def statichandler(request):
    path = request.match_info["path"]
    return (await serve_static_file(request, "data", path))

async def takejobhandler(request):
    peername = request.transport.get_extra_info("peername")
    try:
        params = await request.json()
        agent_hostname = params["hostname"].lower()
        agent_version = params["version"]
        agent_result_id = params.get("result_id", None)
        if agent_result_id is None:
            match_vars = {}
        else:
            match_vars = {"result_id": agent_result_id}
    except ValueError:
        raise web.HTTPBadRequest(reason="request body is not JSON")
    except KeyError:
        raise web.HTTPBadRequest(reason="missing requests keys")
    machine = db.get_machine_by_hostname(agent_hostname)
    if machine is None:
        raise web.HTTPBadRequest(reason="no machine with your hostname, {!r}, exists".format(agent_hostname))
    if [int(v) for v in agent_version.split(".")] < [int(v) for v in MINIMUM_AGENT_VERSION.split(".")]:
        raise web.HTTPBadRequest(reason="minimum agent version is {}, while you are using {!r}".format(MINIMUM_AGENT_VERSION, agent_version))
    match_vars["machine_group_id"] = machine["machine_group_id"]
    print("TAKEJOB FROM:", agent_hostname, peername)
    # If the client connection is lost, aiohttp will cancel this
    # handler automatically, and JOBS.take with it too; JobCenter is
    # designed to cope with this gracefully.
    # XXX: what if the machine is edited while we're waiting over here?
    # XXX: specifically, that "machine_group_id" assignment may go stale.
    JOBS.giveup_all_by_worker(machine["id"])
    job = await JOBS.take(match_vars, machine["id"])
    if job is None:
        raise web.HTTPNotFound(reason="No suitable jobs found")
    print("JOB TAKEN:", job)
    return web.Response(text=json.dumps({
        "job": job.jobargs,
        "timeout": job.jobtimeout,
        "machine_id": machine["id"],
        "success_url": "/api/finish-job/" + str(job.jobid),
        "failure_url": "/api/giveup-job/" + str(job.jobid)
    }))

async def finishjobhandler(request):
    jobid = request.match_info["jobid"]
    try:
        args = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(reason="request body is not JSON")
    print("JOB DONE: " + repr(jobid) + "; RESULT=" + str(args))
    JOBS.finish(int(jobid), args)
    return web.Response()

async def giveupjobhandler(request):
    jobid = request.match_info["jobid"]
    print("JOB BORKED: " + repr(jobid) + "; MAKING IT AVAILABLE AGAIN")
    JOBS.giveup(int(jobid))
    return web.Response()

async def starttestrunhandler(request):
    try:
        params = await request.json()
        log_path = get(params, "log_path", str)
        machine_id = get(params, "machine_id", int)
        result_id = get(params, "result_id", int)
    except ValueError:
        raise web.HTTPBadRequest(reason="request body is not JSON")
    except KeyError:
        raise web.HTTPBadRequest(reason="missing requests keys")
    start_date = int(time.time())
    with db.DB:
        rowid = db.create_test_run(result_id, machine_id,
                                   log_path, start_date, None)
    messageall({
        "m": "insert", "table": "test_runs",
        "value": {
            "id": rowid,
            "result_id": result_id,
            "machine_id": machine_id,
            "log_path": log_path,
            "start_date": start_date,
            "end_date": None
        }
    })
    TEST_RUN_FUTURE_MAP[rowid] = future = asyncio.Future()
    future.add_done_callback(lambda f: TEST_RUN_FUTURE_MAP.pop(rowid))
    return web.Response(text=json.dumps({
        "test_run_id": rowid,
        "end_url": "/api/end-test-run/" + str(rowid)
    }))

async def endtestrunhandler(request):
    # TODO: maybe add some sanity checking here, like making
    # sure that the test run was not already finished?
    test_run_id = int(request.match_info["id"])
    end_date = int(time.time())
    with db.DB:
        db.update_test_run_end_date(test_run_id, end_date)
    messageall({
        "m": "update", "table": "test_runs",
        "value": { "id": test_run_id, "end_date": end_date }
    })
    future = TEST_RUN_FUTURE_MAP.get(test_run_id)
    if future is not None:
        future.set_result(True)
    return web.Response()

async def clienthandler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    peername = request.transport.get_extra_info("peername")
    print("client connected from", peername)
    CLIENTS.append(ws)
    try:
        async for msg in ws:
            if msg.tp == web.MsgType.close: break
            if msg.tp != web.MsgType.text: continue
            try:
                obj = json.loads(msg.data)
            except ValueError:
                print("invalid json:", msg.data)
                continue
            if type(obj) is not dict: break
            m = obj.get("m")
            if type(m) is not str: break
            func = APIFUNCMAP.get(m)
            if not func: break
            func(ws, obj)
    finally:
        CLIENTS.remove(ws)
        print("client disconnected from", peername)
    return ws

async def statushandler(request):
    with StringIO() as output:
        with redirect_stdout(output):
            print("== GENERAL")
            print("python:",
                    platform.python_implementation(),
                    platform.python_version())
            print("compiler:", platform.python_compiler())
            print("platform:", platform.platform(), platform.machine())
            print("aiohttp:", aiohttp.__version__)
            print("sqlite:", sqlite3.sqlite_version)
            print("== RUNTIME")
            print("number of tasks:", len(asyncio.Task.all_tasks()))
            print("number of running results:", len(RESULT_TASK_MAP))
            print("number of running test runs:", len(TEST_RUN_FUTURE_MAP))
            # XXX: json.dumps? really?
            for i, gen in enumerate(gc.get_stats()):
                print("gc gen", i, json.dumps(gen, sort_keys=True))
            print("worker queue size:",
                asyncio.get_event_loop()._default_executor._work_queue.qsize())
            print("== CLIENTS")
            for i, c in enumerate(CLIENTS):
                peername = c._writer.writer.get_extra_info("peername")
                print("client", i, peername)
            print("== ACTIVE RESULTS")
            for k, v in sorted(RESULT_STATUS_MAP.items()):
                print(k, "->", v)
            print("== JOBS")
            print("available:", len(JOBS.available))
            print("pending:", len(JOBS.pending))
            print("waiting:", len(JOBS.waiters))
            for j in JOBS.available.values():
                print("Available:", j)
            for j, _ in JOBS.pending.values():
                print("Pending:", j)
        text = output.getvalue()
    return web.Response(
            body=text.encode("utf-8"),
            content_type="text/plain")

## API UTILS

APIFUNCMAP = {}

def apifunc(name):
    def apifunc_inner(func):
        APIFUNCMAP[name] = func
        return None
    return apifunc_inner

def get(dictionary, key, valuetype):
    value = dictionary[key]
    if not isinstance(value, valuetype): raise TypeError
    return value

def getn(dictionary, key, valuetype):
    value = dictionary[key]
    if value is None: return None
    if not isinstance(value, valuetype): raise TypeError
    return value

def messageone(connection, obj):
    data = json.dumps(obj)
    connection.send_str(data)

def messageall(obj):
    data = json.dumps(obj)
    for client in CLIENTS:
        client.send_str(data)

def respond(connection, msg, data):
    cookie = msg.get("cookie", None)
    if type(cookie) is str:
        messageone(connection, {
            "m": "response",
            "cookie": cookie,
            "data": data
        })

## API FUNCTIONS

# All of these functions are very repetitive, and it should be
# possible to automate this whole section (and much of db.py)
# with some clever table definition DLS. TODO.

### PART 1: CREATE/*

@apifunc("create/build")
def af_create_build(connection, msg):
    path = get(msg, "path", str)
    path = os.path.normpath(path)
    product_id = get(msg, "product_id", int)
    product = db.get_product(product_id)
    respond(connection, msg, None)
    if product:
        job_create_build(path, product)

def normalize_test_suite_spec(spec):
    if spec.get("kind", None) == "TC":
        if "path" not in spec: raise ValueError
        if "routine" not in spec: raise ValueError
        return {
            "kind": "TC",
            "path": os.path.normpath(spec["path"]),
            "routine": spec["routine"]
        }
    if spec.get("kind", None) == "URSTEST":
        if "url" not in spec: raise ValueError
        if "revision" not in spec: raise ValueError
        return {
            "kind": "URSTEST",
            "url": spec["url"],
            "revision": spec["revision"]
        }
    raise ValueError

@apifunc("create/test-suite")
def af_create_test_suite(connection, msg):
    title = get(msg, "title", str)
    spec = normalize_test_suite_spec(get(msg, "spec", dict))
    with db.DB: rowid = db.create_test_suite(title, json.dumps(spec))
    respond(connection, msg, rowid)
    messageall({
        "m": "insert", "table": "test_suites",
        "value": { "id": rowid, "title": title, "spec": spec }
    })

@apifunc("create/machine-group")
def af_create_machine_group(connection, msg):
    title = get(msg, "title", str)
    with db.DB: rowid = db.create_machine_group(title)
    respond(connection, msg, rowid)
    messageall({
        "m": "insert", "table": "machine_groups",
        "value": {"id": rowid, "title": title}
    })

@apifunc("create/machine")
def af_create_machine(connection, msg):
    machine_group_id = getn(msg, "machine_group_id", int)
    hostname = get(msg, "hostname", str).lower()
    description = get(msg, "description", str)
    # XXX: check hostname uniqueness
    with db.DB:
        rowid = db.create_machine(machine_group_id, hostname, description)
    respond(connection, msg, rowid)
    messageall({
        "m": "insert", "table": "machines",
        "value": {
            "id": rowid,
            "machine_group_id": machine_group_id,
            "hostname": hostname,
            "description": description,
            "valid": None
        }
    })
    job_check_machine(rowid, hostname)

@apifunc("create/product")
def af_create_product(connection, msg):
    title = get(msg, "title", str)
    path_pattern = get(msg, "path_pattern", str)
    with db.DB: rowid = db.create_product(title, path_pattern)
    respond(connection, msg, rowid)
    messageall({
        "m": "insert", "table": "products",
        "value": {"id": rowid, "title": title, "path_pattern": path_pattern}
    })

@apifunc("create/autorun-item")
def af_create_autorun_item(connection, msg):
    product_id = get(msg, "product_id", int)
    test_suite_id = get(msg, "test_suite_id", int)
    machine_group_id = get(msg, "machine_group_id", int)
    with db.DB:
        rowid = db.create_autorun_item(
                    product_id, test_suite_id, machine_group_id)
    respond(connection, msg, None)
    messageall({
        "m": "insert", "table": "autorun_items",
        "value": {
            "id": rowid,
            "product_id": product_id,
            "test_suite_id": test_suite_id,
            "machine_group_id": machine_group_id
        }
    })

@apifunc("create/contact")
def af_create_contact(connection, msg):
    name = get(msg, "name", str)
    with db.DB:
        rowid = db.create_contact(name)
    respond(connection, msg, None)
    messageall({
        "m": "insert", "table": "contacts",
        "value": { "id": rowid, "name": name }
    })

@apifunc("create/contact-assignment")
def af_create_contact(connection, msg):
    contact_id = get(msg, "contact_id", int)
    test_suite_id = get(msg, "test_suite_id", int)
    test_name_pattern = get(msg, "test_name_pattern", str)
    with db.DB:
        rowid = db.create_contact_assignment(
            contact_id, test_suite_id, test_name_pattern)
    respond(connection, msg, None)
    messageall({
        "m": "insert", "table": "contact_assignments",
        "value": {
            "id": rowid,
            "contact_id": contact_id,
            "test_suite_id": test_suite_id,
            "test_name_pattern": test_name_pattern
        }
    })

### PART 2: UPDATE/*

@apifunc("update/test-suite")
def af_update_test_suite(connection, msg):
    rowid = get(msg, "id", int)
    title = get(msg, "title", str)
    spec = normalize_test_suite_spec(get(msg, "spec", dict))
    with db.DB: db.update_test_suite(rowid, title, json.dumps(spec))
    respond(connection, msg, None)
    messageall({
        "m": "update", "table": "test_suites",
        "value": { "id": rowid, "title": title, "spec": spec }
    })

@apifunc("update/machine-group")
def af_update_machine_group(connection, msg):
    rowid = get(msg, "id", int)
    title = get(msg, "title", str)
    with db.DB: db.update_machine_group(rowid, title)
    respond(connection, msg, None)
    messageall({
        "m": "update", "table": "machine_groups",
        "value": {"id": rowid, "title": title}
    })

@apifunc("update/machine")
def af_update_machine(connection, msg):
    rowid = get(msg, "id", int)
    machine_group_id = get(msg, "machine_group_id", int)
    hostname = get(msg, "hostname", str).lower()
    description = get(msg, "description", str)
    row = db.get_machine(rowid)
    valid = row["valid"] if row["hostname"] == hostname else None
    with db.DB:
        db.update_machine(rowid, machine_group_id, hostname, description, valid)
    respond(connection, msg, None)
    messageall({
        "m": "update", "table": "machines",
        "value": {
            "id": rowid,
            "machine_group_id": machine_group_id,
            "hostname": hostname,
            "description": description,
            "valid": valid
        }
    })
    if not valid: job_check_machine(rowid, hostname)

@apifunc("update/product")
def af_update_product(connection, msg):
    rowid = get(msg, "id", int)
    title = get(msg, "title", str)
    path_pattern = get(msg, "path_pattern", str)
    with db.DB: db.update_product(rowid, title, path_pattern)
    respond(connection, msg, None)
    messageall({
        "m": "update", "table": "products",
        "value": {"id": rowid, "title": title, "path_pattern": path_pattern}
    })

@apifunc("update/result-item")
def af_update_result_item(connection, msg):
    rowid = get(msg, "id", int)
    test_name = get(msg, "test_name", str)
    test_passed = get(msg, "test_passed", bool)
    with db.DB: db.update_result_item(rowid, test_name, int(test_passed))
    respond(connection, msg, None)
    messageall({
        "m": "update", "table": "result_items",
        "value": {
            "id": rowid,
            "test_name": test_name,
            "test_passed": test_passed
        }
    })

@apifunc("update/contact")
def af_update_contact(connection, msg):
    rowid = get(msg, "id", int)
    name = get(msg, "name", str)
    with db.DB: db.update_contact(rowid, name)
    respond(connection, msg, None)
    messageall({
        "m": "update", "table": "contacts",
        "value": { "id": rowid, "name": name }
    })

@apifunc("update/contact-assignment")
def af_update_contact(connection, msg):
    rowid = get(msg, "id", int)
    contact_id = get(msg, "contact_id", int)
    test_suite_id = get(msg, "test_suite_id", int)
    test_name_pattern = get(msg, "test_name_pattern", str)
    with db.DB:
        db.update_contact_assignment(
            rowid, contact_id, test_suite_id, test_name_pattern)
    respond(connection, msg, None)
    messageall({
        "m": "update", "table": "contact_assignments",
        "value": {
            "id": rowid,
            "contact_id": contact_id,
            "test_suite_id": test_suite_id,
            "test_name_pattern": test_name_pattern
        }
    })

### PART 3: DELETE/*

@apifunc("delete/test-suite")
def af_delete_test_suite(connection, msg):
    rowid = get(msg, "id", int)
    with db.DB: db.delete_test_suite(rowid)
    respond(connection, msg, None)
    messageall({ "m": "delete", "table": "test_suites", "id": rowid })

@apifunc("delete/machine-group")
def af_delete_machine_group(connection, msg):
    rowid = get(msg, "id", int)
    with db.DB: db.delete_machine_group(rowid)
    respond(connection, msg, None)
    messageall({ "m": "delete", "table": "machine_groups", "id": rowid })

@apifunc("delete/machine")
def af_delete_machine(connection, msg):
    rowid = get(msg, "id", int)
    with db.DB: db.delete_machine(rowid)
    respond(connection, msg, None)
    messageall({ "m": "delete", "table": "machines", "id": rowid })

@apifunc("delete/product")
def af_delete_product(connection, msg):
    rowid = get(msg, "id", int)
    with db.DB: db.delete_product(rowid)
    respond(connection, msg, None)
    messageall({ "m": "delete", "table": "products", "id": rowid })

@apifunc("delete/autorun-item")
def af_delete_autorun_item(connection, msg):
    rowid = get(msg, "id", int)
    with db.DB: db.delete_autorun_item(rowid)
    respond(connection, msg, None)
    messageall({ "m": "delete", "table": "autorun_items", "id": rowid })

@apifunc("delete/result")
def af_delete_result(connection, msg):
    rowid = get(msg, "id", int)
    # XXX: This is not fool-proof; if the build, test suite or
    # XXX: machine group of the running result is deleted, the
    # XXX: result will be deleted too (due to foreign key
    # XXX: constraints).
    if rowid in RESULT_TASK_MAP:
        respond(connection, msg, False)
    else:
        with db.DB: db.delete_result(rowid)
        respond(connection, msg, True)
        messageall({ "m": "delete", "table": "results", "id": rowid })

@apifunc("delete/contact")
def af_contact(connection, msg):
    rowid = get(msg, "id", int)
    with db.DB: db.delete_contact(rowid)
    respond(connection, msg, None)
    messageall({ "m": "delete", "table": "contacts", "id": rowid })

@apifunc("delete/contact-assignment")
def af_contact_assignment(connection, msg):
    rowid = get(msg, "id", int)
    with db.DB: db.delete_contact_assignment(rowid)
    respond(connection, msg, None)
    messageall({ "m": "delete", "table": "contact_assignments", "id": rowid })

### PART 4: LIST/*

@apifunc("list/test-suites/all")
def af_list_test_suites_all(connection, msg):
    respond(connection, msg, [{
            "id": row["id"],
            "title": row["title"],
            "spec": json.loads(row["spec"])
        } for row in db.iter_test_suites()
    ])

@apifunc("list/machine-groups/all")
def af_list_machine_groups_all(connection, msg):
    respond(connection, msg, [{
            "id": row["id"],
            "title": row["title"]
        } for row in db.iter_machine_groups()
    ])

@apifunc("list/machines/all")
def af_list_machines_all(connection, msg):
    respond(connection, msg, [{
            "id": row["id"],
            "machine_group_id": row["machine_group_id"],
            "hostname": row["hostname"],
            "description": row["description"],
            "valid": row["valid"]
        } for row in db.iter_machines()
    ])

@apifunc("list/products/all")
def af_list_products_all(connection, msg):
    respond(connection, msg, [{
            "id": row["id"],
            "title": row["title"],
            "path_pattern": row["path_pattern"]
        } for row in db.iter_products()
    ])

@apifunc("list/autorun-items/all")
def af_list_autorun_items_all(connection, msg):
    respond(connection, msg, [{
            "id": row["id"],
            "product_id": row["product_id"],
            "test_suite_id": row["test_suite_id"],
            "machine_group_id": row["machine_group_id"]
        } for row in db.iter_autorun_items()
    ])

@apifunc("list/builds/before-date")
def af_list_builds_before_date(connection, msg):
    date = get(msg, "date", int)
    limit = get(msg, "limit", int)
    respond(connection, msg, [{
            "id": row["id"],
            "product_id": row["product_id"],
            "name": row["name"],
            "path": row["path"],
            "date": row["date"]
        } for row in db.iter_builds_before_date(date, limit)
    ])

@apifunc("list/builds/by-id")
def af_list_builds_by_id(connection, msg):
    rowid = get(msg, "id", int)
    row = db.get_build(rowid)
    respond(connection, msg, [{
        "id": row["id"],
        "product_id": row["product_id"],
        "name": row["name"],
        "path": row["path"],
        "date": row["date"]
    }] if row else [])

@apifunc("list/test-runs/by-id")
def af_list_test_runs_by_id(connection, msg):
    rowid = get(msg, "id", int)
    row = db.get_test_run(rowid)
    respond(connection, msg, [{
        "id": row["id"],
        "result_id": row["result_id"],
        "machine_id": row["machine_id"],
        "log_path": row["log_path"],
        "start_date": row["start_date"],
        "end_date": row["end_date"]
    }] if row else [])

@apifunc("list/test-runs/by-result")
def af_list_test_runs_by_result(connection, msg):
    result_id = get(msg, "result_id", int)
    respond(connection, msg, [{
            "id": row["id"],
            "result_id": row["result_id"],
            "machine_id": row["machine_id"],
            "log_path": row["log_path"],
            "start_date": row["start_date"],
            "end_date": row["end_date"]
        } for row in db.iter_test_runs_by_result(result_id)
    ])

@apifunc("list/results/by-id")
def af_list_results_by_id(connection, msg):
    rowid = get(msg, "id", int)
    row = db.get_result(rowid)
    respond(connection, msg, [{
        "id": row["id"],
        "test_suite_id": row["test_suite_id"],
        "machine_group_id": row["machine_group_id"],
        "build_id": row["build_id"],
        # XXX: do I also need build_product_id here?
        "build_name": row["build_name"],
        "log_path": row["log_path"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "status": RESULT_STATUS_MAP.get(row["id"], db.get_status(row["id"]))
    }] if row else [])

@apifunc("list/results/before-date")
def af_list_results_before_date(connection, msg):
    date = get(msg, "date", int)
    limit = get(msg, "limit", int)
    respond(connection, msg, [{
            "id": row["id"],
            "test_suite_id": row["test_suite_id"],
            "machine_group_id": row["machine_group_id"],
            "build_id": row["build_id"],
            "build_name": row["build_name"],
            "log_path": row["log_path"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "status": RESULT_STATUS_MAP.get(row["id"], None)
        } for row in db.iter_results_before_date(date, limit)
    ])

@apifunc("list/results/by-build")
def af_list_results_by_build(connection, msg):
    build_id = get(msg, "build_id", int)
    respond(connection, msg, [{
            "id": row["id"],
            "test_suite_id": row["test_suite_id"],
            "machine_group_id": row["machine_group_id"],
            "build_id": row["build_id"],
            "build_name": row["build_name"],
            "log_path": row["log_path"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "contact": row["contact"],
            "status": RESULT_STATUS_MAP.get(row["id"], None)
        } for row in db.iter_results_by_build(build_id)
    ])

@apifunc("list/result-items/by-result")
def af_list_result_items_by_result(connection, msg):
    result_id = get(msg, "result_id", int)
    respond(connection, msg, [{
            "id": row["id"],
            "result_id": row["result_id"],
            "test_name": row["test_name"],
            "test_passed": bool(row["test_passed"])
        } for row in db.iter_result_items_by_result(result_id)
    ])

@apifunc("list/contacts/all")
def af_list_contacts_all(connection, msg):
    respond(connection, msg, [{
            "id": row["id"],
            "name": row["name"]
        } for row in db.iter_contacts()
    ])

@apifunc("list/contact-assignments/all")
def af_list_contacts_all(connection, msg):
    respond(connection, msg, [{
            "id": row["id"],
            "contact_id": row["contact_id"],
            "test_suite_id": row["test_suite_id"],
            "test_name_pattern": row["test_name_pattern"]
        } for row in db.iter_contact_assignments()
    ])

## PART 5: MISC

@apifunc("update/build-list")
def af_update_build_list(connection, msg):
    respond(connection, msg, None)
    job_update_build_list()

@apifunc("launch/tests/by-build")
def af_launch_tests_by_build(connection, msg):
    build_id = get(msg, "build_id", int)
    contact = get(msg, "contact", str)
    respond(connection, msg, None)
    launch_all_the_tests(build_id, contact=contact)

@apifunc("launch/test")
def af_launch_test(connection, msg):
    build_id = get(msg, "build_id", int)
    tsuite_id = get(msg, "test_suite_id", int)
    mgroup_id = get(msg, "machine_group_id", int)
    contact = get(msg, "contact", str)

    respond(connection, msg, None)

    build = db.get_build(build_id)
    tsuite = db.get_test_suite(tsuite_id)
    mgroup = db.get_machine_group(mgroup_id)

    if build is not None and tsuite is not None and mgroup is not None:
        EVENT_LOOP.create_task(run_one_result(build, mgroup, tsuite, contact=contact))

@apifunc("launch/test-subset")
def af_launch_test(connection, msg):
    build_id = get(msg, "build_id", int)
    tsuite_id = get(msg, "test_suite_id", int)
    mgroup_id = get(msg, "machine_group_id", int)
    test_groups = get(msg, "test_groups", list)
    contact = get(msg, "contact", str)
    print("test_groups: ", test_groups)
    for group in test_groups:
        if not isinstance(group, list): raise TypeError
        for testname in group:
            if not isinstance(testname, str): raise TypeError

    respond(connection, msg, None)

    build = db.get_build(build_id)
    tsuite = db.get_test_suite(tsuite_id)
    mgroup = db.get_machine_group(mgroup_id)

    if build is not None and tsuite is not None and mgroup is not None:
        EVENT_LOOP.create_task(
                run_one_result(build, mgroup, tsuite, test_groups, contact))

@apifunc("stop/result")
def af_stop_result(connection, msg):
    rowid = get(msg, "id", int)
    if rowid in RESULT_TASK_MAP:
        RESULT_TASK_MAP[rowid].cancel()
        respond(connection, msg, True)
    else:
        respond(connection, msg, False)

## ASYNC JOBS

def job_check_machine(rowid, hostname):
    def work():
        try:
            socket.getaddrinfo(hostname, None)
            valid = True
        except socket.error:
            valid = False
        EVENT_LOOP.call_soon_threadsafe(finish, int(valid))
    def finish(valid):
        row = db.get_machine(rowid)
        if row is not None and row["hostname"] == hostname:
            with db.DB: db.update_machine_valid(rowid, valid)
            messageall({
                "m": "update", "table": "machines",
                "value": {"id": rowid, "valid": valid}
            })
    return EVENT_LOOP.run_in_executor(None, work)

def job_merge_testcomplete_logs(pjs_path, src_logdirs, tag, title):
    def work():
        dst_logdir = os.path.join(
            os.path.dirname(pjs_path), "[TESTBOT]", "Log", tag
        )
        logs = []
        for logdir in src_logdirs:
            try:
                tcLog_path = os.path.join(logdir, "Description.tcLog")
                logs.extend(tclog.read_all_testlogs(tcLog_path))
            except Exception as e:
                print("ERROR WHEN READING TestComplete LOG:", e)
        if len(logs) == 0:
            return
        merged_log = tclog.merge_testlogs(logs, name=title)
        os.makedirs(os.path.dirname(dst_logdir), exist_ok=True)
        tclog.save_testlog(merged_log, dst_logdir)
        merged_log = logs = None
        tclog.register_testbot_log(pjs_path,
                os.path.join(tag, "Description.tcLog"))
        return dst_logdir + os.path.sep
    return EVENT_LOOP.run_in_executor(None, work)

def job_merge_urstest_logs(dst_logpath, src_logpaths, metainfo=None):
    def work():
        utlog.merge_logs(dst_logpath, src_logpaths, metainfo)
    return EVENT_LOOP.run_in_executor(None, work)

## PERIODIC JOBS

async def periodic(seconds, job):
    minsleep = max(seconds*3//4, 1)
    maxsleep = max(seconds*5//4, minsleep + 1)
    await asyncio.sleep(random.randrange(minsleep))
    while True:
        try:
            future = job()
            if future is not None: await future
        except Exception as e:
            print("PERIODIC JOB FAILED:", e)
        await asyncio.sleep(random.randrange(minsleep, maxsleep))

def register_a_build(product, path, mtime):
    path = os.path.normpath(path)
    name = os.path.basename(path)
    row = db.get_latest_build_by_path(path)
    if row is not None and mtime <= row["date"] + 1:
        return
    if row is None:
        save_log(message=f"Line 915: row is None. {name}, {path}, {mtime}", suffix=name)
        with db.DB:
            rowid = db.create_build(product["id"], name, path, mtime)
        messageall({
            "m": "insert", "table": "builds",
            "value": {
                "id": rowid,
                "product_id": product["id"],
                "name": name,
                "path": path,
                "date": mtime
            }
        })
        launch_all_the_tests(rowid)
        with db.DB:
            db.create_build_to_send_result(product["id"], name, path, mtime)
        bot.auto_send_message(name, mtime)
        create_shortcut(path)
    else:
        save_log(message=f"Line 936: row is not None. {name}, {path}, {mtime}", suffix=name)
        with db.DB:
            db.update_build_date(row["id"], mtime)
        messageall({
            "m": "update", "table": "builds",
            "value": { "id": row["id"], "date": mtime }
        })
        save_log(message=f"Line 943: launch_all_the_tests. {name}, {path}, {mtime} with row[id]={row['id']}", suffix=name)
        launch_all_the_tests(row["id"])

def job_create_build(path, product):
    def work(path, product):
        try:
            st = os.stat(path)
            if stat.S_ISREG(st.st_mode):
                EVENT_LOOP.call_soon_threadsafe(
                    register_a_build, product, path, int(st.st_mtime)
                )
        except IOError as e:
            print("IOError while updating build list", e)
    return EVENT_LOOP.run_in_executor(None, work, path, product)

def job_update_build_list():
    def work(products):
        for p in products:
            try:
                files = []
                for filename in glob.glob(p["path_pattern"]):
                    st = os.stat(filename)
                    if not stat.S_ISREG(st.st_mode): continue
                    files.append((int(st.st_mtime), filename))
                files.sort(key=lambda f: f[0], reverse=True)
            except IOError as e:
                print("IOError while updating build list", e)
                continue
            EVENT_LOOP.call_soon_threadsafe(finish, p, files[:10])
    def finish(product, files):
        for mtime, path in files:
            register_a_build(product, path, mtime)
    products = list(db.iter_products())
    return EVENT_LOOP.run_in_executor(None, work, products)

## TESTING, PART I: THE JOB CENTER

class JobFuture(asyncio.Future):
    def __init__(self, id, timeout, args):
        self.jobid = id
        self.jobstarttime = None
        self.jobtimeout = timeout
        self.jobargs = args
        asyncio.Future.__init__(self)
    def __str__(self):
        return "JobFuture(id=%s, args=%s)" % (self.jobid, self.jobargs)

# JobCenter is similar to an asyncio.Queue, with two differences:
# 1) when take()ing from the queue, you can specify a filter on
#    what sort of items you want to obtain (_match_job() is the filter);
# 2) removing from a queue is a two-step process: first you take()
#    a job, then you finish() it; if you didn't finish it in time,
#    the same job will return to the queue again (via update_pending()),
#    and someone else may take it as well.

class JobCenter:
    def __init__(self):
        self.counter = int.from_bytes(os.urandom(4), 'little')
        self.available = {}
        self.pending = {}
        self.waiters = []
    def _make_available(self, job):
        random.shuffle(self.waiters)
        for i, (future, args) in enumerate(self.waiters):
            if self._match_job(job, args):
                self.waiters.pop(i)
                future.set_result(job)
                return
        self.available[job.jobid] = job
    def _match_job(self, job, args):
        for k, v in args.items():
            if job.jobargs[k] != v:
                return False
        return True
    def perform(self, timeout, args):
        self.counter += 1
        job = JobFuture(self.counter, timeout, args)
        # _remove_job() is only needed if the job is cancelled
        job.add_done_callback(self._remove_job)
        self._make_available(job)
        return job
    def take_nowait(self, args, workerid):
        items = list(self.available.items())
        random.shuffle(items)
        for jobid, job in items:
            if self._match_job(job, args):
                del self.available[jobid]
                job.jobstarttime = time.monotonic()
                self.pending[jobid] = (job, workerid)
                return job
        return None
    async def take(self, args, workerid):
        job = self.take_nowait(args, workerid)
        if job is not None:
            return job
        future = asyncio.Future()
        self.waiters.append((future, args))
        try:
            job = await future
            job.jobstarttime = time.monotonic()
            self.pending[job.jobid] = (job, workerid)
            return job
        finally:
            # This is for the case when the task was cancelled.
            # Normally _make_available cleans up the waiters entry.
            try: self.waiters.remove((future, args))
            except ValueError: pass
    def finish(self, jobid, result):
        if jobid in self.pending:
            job, workerid = self.pending[jobid]
            job.set_result(result)
            del self.pending[jobid]
        if jobid in self.available:
            job = self.available[jobid]
            job.set_result(result)
            del self.available[jobid]
    def giveup(self, jobid):
        if jobid in self.pending:
            job, workerid = self.pending[jobid]
            del self.pending[jobid]
            self._make_available(job)
    def giveup_all_by_worker(self, workerid):
        for jobid, (job, w_id) in list(self.pending.items()):
            if w_id == workerid:
                del self.pending[jobid]
                self._make_available(job)
    def _remove_job(self, job):
        try:
            del self.available[job.jobid]
        except KeyError:
            pass
        try:
            del self.pending[job.jobid]
        except KeyError:
            pass
    # Run this method periodically.
    def update_pending(self):
        timestamp = time.monotonic()
        # list() is here so that the 'del' would not cause the loop to fail
        for jobid, (job, _) in list(self.pending.items()):
            if timestamp >= job.jobstarttime + job.jobtimeout:
                print("JOB IS OVERDUE: " + repr(jobid) + "; RETURNING IT")
                del self.pending[jobid]
                self._make_available(job)

## TESTING, PART II: THE WORKERS

def launch_all_the_tests(build_id, contact="Main Log"):
    print("1: ", contact)
    build = db.get_build(build_id)
    if build is None:
        print("build", build_id, "does not exist, can't test it")
        return
    for rs in db.iter_autorun_items_by_product(build["product_id"]):
        ts_id = rs["test_suite_id"]
        mg_id = rs["machine_group_id"]
        ts = db.get_test_suite(ts_id)
        mg = db.get_machine_group(mg_id)
        if ts is not None and mg is not None:
            EVENT_LOOP.create_task(run_one_result(build, mg, ts, contact=contact))

async def run_one_result(build, mg, ts, test_groups=None, contact="Main Log"):
    def update_result_status(result_id, status, send=True):
        if result_id in RESULT_TASK_MAP:
            RESULT_STATUS_MAP[result_id] = status
            if send:
                messageall({
                    "m": "update", "table": "results",
                    "value": { "id": result_id, "status": status }
                })
    start_date = int(time.time())
    with db.DB:
        result_id = db.create_result(
                build["id"], ts["id"], mg["id"], start_date, contact)
    result_log_path = None
    ts_spec = json.loads(ts["spec"])
    RESULT_TASK_MAP[result_id] = asyncio.Task.current_task(loop=EVENT_LOOP)
    try:
        update_result_status(result_id, "fetching the script", False)
        messageall({
            "m": "insert", "table": "results",
            "value": {
                "id": result_id,
                "test_suite_id": ts["id"],
                "machine_group_id": mg["id"],
                "build_id": build["id"],
                "build_name": build["name"],
                "log_path": None,
                "start_date": start_date,
                "end_date": None,
                "contact": contact,
                "status": RESULT_STATUS_MAP.get(result_id, None)
            }
        })
        if test_groups is None:
            # XXX: this section needs timeouts, or maybe a way
            # XXX: to manually cancel it from the API.
            tests_list = await JOBS.perform(15*60, {
                "action": "list-tests",
                "machine_group_id": mg["id"],
                "result_id": result_id,
                "build_path": build["path"],
                "test_suite_spec": ts_spec
            })
            test_groups = []
            for script, units in tests_list.items():
                test_groups.append([script + "." + unit for unit in units])
                print("script: ", script, "units: ", units)
        random.shuffle(test_groups)
        update_result_status(result_id,
                "dispatching (%d groups in total)" % len(test_groups))
        jobs = []
        def run_tests_done(job):
            N = 0
            for j in jobs:
                if j.done():
                    N += 1
            update_result_status(result_id,
                "testing; finished %d out of %d test groups" % (N, len(jobs)))
            _do_one_test_finish(result_id, job)
        for test_group in test_groups:
            job = JOBS.perform(1*60*60, {
                "action": "run-tests",
                "tests": test_group,
                "machine_group_id": mg["id"],
                "result_id": result_id,
                "build_path": build["path"],
                "test_suite_spec": ts_spec
            })
            job.add_done_callback(run_tests_done)
            jobs.append(job)
        try:
            await asyncio.wait(jobs)
        finally:
            for job in jobs: job.cancel()
        if ts_spec["kind"] == "URSTEST":
            update_result_status(result_id, "fetching log root")
            log_root = await JOBS.perform(15*60, {
                "action": "get-log-root",
                "machine_group_id": mg["id"],
                "result_id": result_id,
                "build_path": build["path"],
                "test_suite_spec": ts_spec
            })
        update_result_status(result_id, "waiting for all test runs to end")
        test_runs = list(db.iter_test_runs_by_result(result_id))
         # Todo: If path is not valid -> remove from db
        futures = [
            TEST_RUN_FUTURE_MAP[row["id"]]
            for row in test_runs
            if row["id"] in TEST_RUN_FUTURE_MAP
        ]
        try:
            await asyncio.wait(futures, timeout=5*60)
        finally:
            for f in futures: f.cancel()
        if ts_spec["kind"] == "TC":
            update_result_status(result_id, "merging TestComplete logs")
            datestring = \
                time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime(start_date))
            result_log_path = \
                await job_merge_testcomplete_logs(
                    ts_spec["path"],
                    [row["log_path"] for row in test_runs],
                    "%s-result%s" % (datestring, result_id),
                    "%s (%s)" % (build["name"], mg["title"]))
        if ts_spec["kind"] == "URSTEST":
            update_result_status(result_id, "merging URS-Test logs")
            datestring = \
                time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime(start_date))
            result_log_path = os.path.join(
                    log_root,
                    "%s-result%s.ulg" % (datestring, result_id))
            metainfo = {
                "build_id": build["id"],
                "build_name": build["name"],
                "test_suite_id": ts["id"],
                "test_suite_title": ts["title"],
                "machine_group_id": mg["id"],
                "machine_group_title": mg["title"],
                "date": start_date,
                "result_id": result_id
            }
            await job_merge_urstest_logs(
                    result_log_path,
                    [row["log_path"] for row in test_runs],
                    metainfo)
    finally:
        end_date = int(time.time())
        del RESULT_STATUS_MAP[result_id]
        del RESULT_TASK_MAP[result_id]
        with db.DB:
            db.update_result_end_date(result_id, end_date)
            db.update_result_log_path(result_id, result_log_path)
        try:
            statistics = db.tester_statistics(result_id)
            build = db.build_by_result_id(result_id)
            send_mes = db2.insert_statistics(statistics, build)
            if send_mes:
                bot.send_to_contact([bot.ayelagin_id], f"Statistics for {build} has been added")
            # else:
                # bot.send_to_contact([bot.ayelagin_id], f"Statistics for {build} not added")
        except Exception as e:
            bot.send_to_contact([bot.ayelagin_id], "main 1234" + str(e))
        try:
            recolor_scripts(result_id)
        except Exception as e:
            save_log(str(e), result_id)
        messageall({
            "m": "update", "table": "results",
            "value": {
                "id": result_id,
                "end_date": end_date,
                "log_path": result_log_path,
                "status": None
            }
        })

def _do_one_test_finish(result_id, job):
    results = job.result()
    messages = []
    with db.DB:
        for test_name, result in results.items():
            passed = \
                result if isinstance(result, bool) else \
                result == 0 if isinstance(result, int) else \
                result.get("errors", 1) == 0
            rowid = db.create_result_item(result_id, test_name, int(passed))
            messages.append({
                "m": "insert", "table": "result_items",
                "value": {
                    "id": rowid,
                    "result_id": result_id,
                    "test_name": test_name,
                    "test_passed": passed
                }
            })
    for m in messages:
        messageall(m)
## OTHER FUNCTIONS
def create_shortcut(source_file_path):
    try:
        if(source_file_path.find("URSApplicationStudio-12") != -1):
            target_dir = r"\\file-server\B-Test\MUA"
            path_parts = source_file_path.split("\\")
            for i in range(5, len(path_parts)-1):
                target_dir = os.path.join(target_dir, path_parts[i])
            shortcut_name = path_parts[-1]
            path = os.path.join(target_dir, shortcut_name + ".lnk")
            source_dir = os.path.dirname(source_file_path)
            # # # Path to icon
            # # icon = r"C:\Users\Public\Desktop\Microsoft Edge.lnk"
            # Using the Dispatch method, we declare work with Wscript (work with shortcuts, registry and other system information in Windows)
            shell = Dispatch('WScript.Shell')
            # Create shortcut
            shortcut = shell.CreateShortCut(path)
            shortcut.Targetpath = source_file_path
            shortcut.WorkingDirectory = target_dir
            # # Steal an icon
            # shortcut.IconLocation = icon
            # Save shortcut
            shortcut.save()
            remove_temp_txt()
    except Exception as e:
        bot.send_to_contact([bot.ayelagin_id], "main 1291" + str(e))


def remove_old_logs():
    try:
        N = 4 #The number of days the log file is kept
        log_path = r"\\file-server\B-Test\URS-Test-Logs"
        cur_date = datetime.date.today()
        cur_year = datetime.date.today().strftime("%Y")
        cur_month = datetime.date.today().strftime("%m")
        first = cur_date.replace(day=1)
        prev_month = (first - datetime.timedelta(days=1)).strftime("%m")
        log_files = [filename for filename in os.listdir(path = log_path) if filename.endswith(".ulg")]
        log_folders = [folder for folder in os.listdir(log_path) if os.path.isdir(os.path.join(log_path,folder))]
        for folder in log_folders:
            if folder==str(int(cur_year)-1):
                path = os.path.join(log_path, folder)
                t = datetime.date.fromtimestamp(os.path.getmtime(path))
                if(cur_date - t).days > N+3:
                    shutil.rmtree(path)
            if folder==cur_year:
                prev_month_path = os.path.join(log_path, folder, prev_month)
                if os.path.exists(prev_month_path):
                    t = datetime.date.fromtimestamp(os.path.getmtime(prev_month_path))
                    if(cur_date - t).days > N:
                        text = "{} folder has been deleted".format(prev_month_path)
                        shutil.rmtree(prev_month_path)
                        print(text)
                # else:
                #     print("No path exists", prev_month_path)
                cur_month_path = os.path.join(log_path, folder, cur_month)
                for folder in os.listdir(cur_month_path):
                    path3 = os.path.join(cur_month_path,folder)
                    t = datetime.date.fromtimestamp(os.path.getmtime(path3))
                    if(cur_date - t).days > N:
                            text = "{} folder has been deleted".format(path3)
                            shutil.rmtree(path3)
                            print(text)
    except Exception as e:
        text = "Cannot remove folder: {}".format(str(e))
        bot.send_to_contact([bot.ayelagin_id], text)
    try:
        for file in log_files:
            path = os.path.join(log_path, file)
            t = datetime.date.fromtimestamp(os.path.getmtime(path))
            if(cur_date - t).days > N:
                os.remove(path)
    except Exception as e:
        text = "Cannot remove file: {}".format(str(e))
        bot.send_to_contact([bot.ayelagin_id], text)

def get_bug_list(version):
    bugs = {}
    current_date = datetime.date.today()
    response = requests.get("{}bug?creation_time={}&version={}".format(URL, current_date, version))
    for bug in response.json()["bugs"]:
        if bug["is_open"]:
            bugs.update({bug["id"]:bug["summary"]})
    return bugs

def build_result(build_name):
    build_id = db.get_build_id_by_name(build_name)['id']
    print(build_id)
    result_id = db.get_results_id(build_id)['id']
    print(result_id)
    return db.get_status(result_id)

def send_message(addresses, message, build_name):
    HOST = 'net-server'
    FROM = 'testrobot@ultimaterisk.dp.ua'
    SUBJECT = 'TESTING RESULTS for {}'.format(build_name)
    BODY = "\r\n".join((
    "From: %s" % FROM,
    "To: %s" % addresses,
    "Subject: %s" % SUBJECT ,
    "",
    message))
    server = smtplib.SMTP(HOST)
    server.sendmail(FROM, addresses, BODY)
    server.quit()

def sending_testing_results():
    now = time.strftime('%H:%M')
    receivers = ['<ayelagin@ultimaterisk.dp.ua>', '<osemencha@ultimaterisk.dp.ua>', '<ybarannik@ultimaterisk.dp.ua>']
    bot_receivers = [bot.ayelagin_id, bot.ybarannik_id, bot.voronina_id, bot.kozhushkina_id, bot.yakunina_id, bot.smola_id]
    rows = db.rows_in_table()
    if('13:00'<now<'14:00' and rows):
        try:
            for _ in range(rows):
                product_name = db.get_name_to_send_result()['name']
                print(product_name)
                status = build_result(product_name)
                build_name = product_name.split("-64bit.msi")[0]
                print(build_name)
                version = build_name[21:23]
                message = "{} has been checked ".format(build_name)
                bug_list = get_bug_list(version)
                if not bug_list:
                    message+="successfully\n"
                else:
                    message+="with opened bugs:\n"
                    for id in bug_list:
                        message+="\tBug {}: {}\n".format(id, bug_list[id])
                message+="Status: {}".format(status)
                send_message(receivers, message, build_name)
                bot.send_to_contact(bot_receivers, message)
                with db.DB:
                    db.delete_build_after_send(product_name)
        except Exception as e:
            bot.send_to_contact([bot.ayelagin_id], str(e))
    else:
        pass

def remove_temp_txt(log_path = r"\\DOMAIN16\MUA\temp_vp"):
    # count = 0
    for file in glob.glob(os.path.join(log_path, "*")):
        os.remove(file)
        # count += 1
        # print("{} file has been deleted".format(file))
    # print("Total removed {} objects".format(count))

def recolor_scripts(result_id):
    main_build_id = db.main_build_id(result_id)["build_id"]
    # print(main_build_id)
    build_name = db.build_name_by_id(main_build_id)["name"]
    main_log_data = db.get_main_log_date(main_build_id)[-1]
    main_log_id = main_log_data["id"]
    print(main_build_id, build_name, main_log_id)
    # print("main start date: ", datetime.datetime.fromtimestamp(main_start_date))
    # print("main log id: ", main_log_id)

    main_scripts = db.result_by_id(main_log_id)
    count = 0
    colored_scripts = []

    rerun_scripts = db.result_by_id(result_id)
    for item1 in rerun_scripts:
        for item2 in main_scripts:
            if item1["test_name"] == item2["test_name"] :
                if item1["test_passed"] > item2["test_passed"] :
                    # item2["test_passed"] = item1["test_passed"]
                    # print(item1["test_name"], "   ", item1["test_passed"])
                    # print(item2["test_name"], "   ", item2["test_passed"])
                    db.update_item_by_result_id(main_log_id, item1["test_name"], item1["test_passed"])
                    colored_scripts.append((f"{main_log_id} from {result_id}", item1["test_name"]))
                    count += 1
    # print(f"Recolored {count} scripts: \n_______________________________")
    # for script in colored_scripts:
    #     print(script)
    if(colored_scripts):
        save_log(f"Recolored {count} scripts: \n____________________________________", build_name)
        for script in colored_scripts:
            save_log(script, build_name)


## GLOBALS

CLIENTS = []

EVENT_LOOP = None

JOBS = JobCenter()

RESULT_STATUS_MAP = {}

RESULT_TASK_MAP = {}

TEST_RUN_FUTURE_MAP = {}

## MAIN

if __name__ == "__main__":
    db.init()
    if os.name == "nt":
        asyncio.set_event_loop(asyncio.ProactorEventLoop())
    EVENT_LOOP = asyncio.get_event_loop()
    EVENT_LOOP.run_until_complete(init(EVENT_LOOP, None, 80))
    EVENT_LOOP.create_task(periodic(3*60, job_update_build_list))
    EVENT_LOOP.create_task(periodic(1*60, JOBS.update_pending))
    EVENT_LOOP.create_task(periodic(24*60*60, db.backup))
    EVENT_LOOP.create_task(periodic(1*20, bot.main))
    EVENT_LOOP.create_task(periodic(20*60, sending_testing_results))
    EVENT_LOOP.create_task(periodic(24*60*60, remove_old_logs))
    EVENT_LOOP.run_forever()

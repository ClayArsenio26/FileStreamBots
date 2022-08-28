# Taken from megadlbot_oss <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/webserver/routes.py>
# Thanks to Eyaadh <https://github.com/eyaadh>

import re
import time
import math
import logging
import secrets
import mimetypes
import shutil
import psutil
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from WebStreamer.bot import multi_clients, work_loads, class_cache
from WebStreamer.server.exceptions import FIleNotFound, InvalidHash
from WebStreamer import Var, utils, StartTime, __version__, StreamBot


routes = web.RouteTableDef()

@routes.get("/", allow_head=True)
async def root_route_handler(_):
    return web.json_response(
        {
            "server_status": "running",
            "uptime": utils.get_readable_time(time.time() - StartTime),
            "connected_bots": len(multi_clients),
            "loads": dict(
                ("bot" + str(c + 1), l)
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            ),
            "version": __version__,
        }
    )

@routes.get("/status", allow_head=True)
async def status_handler(request: web.Request):
    uptime = utils.get_readable_time((time.time() - StartTime))
    total, used, free = shutil.disk_usage('.')

    total = utils.human_size(total)
    used = utils.human_size(used)
    free = utils.human_size(free)

    sent = utils.human_size(psutil.net_io_counters().bytes_sent)
    recv = utils.human_size(psutil.net_io_counters().bytes_recv)

    cpuUsage = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent

    html_resp = f"""<html><head><title>Status</title></head>
<body>
    <table border="1">
        <tr><td>Bot Uptime </td><td>{uptime} </td></tr>
        <tr><td>Total Disk Space </td><td>{total} </td></tr>
        <tr><td>Used Disk Space </td><td>{used} </td></tr>
        <tr><td>Free Disk Space </td><td>{free} </td></tr>
        <tr><td>Data Sent </td><td>{sent} </td></tr>
        <tr><td>Data Received </td><td>{recv} </td></tr>
        <tr><td>CPU </td><td>{cpuUsage}% </td></tr>
        <tr><td>RAM </td><td>{memory}% </td></tr>
        <tr><td>Disk </td><td>{disk}% </td></tr>
    </table>
</body>
</html>"""
    return web.Response(text=html_resp, content_type='text/html')

@routes.get(r"/{path:\S+}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            message_id = int(match.group(2))
        else:
            message_id = int(re.search(r"(\d+)(?:\/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")
        return await media_streamer(request, message_id, secure_hash)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))

async def media_streamer(request: web.Request, message_id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)
    
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]
    
    if Var.MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.remote}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = utils.ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(message_id)
    logging.debug("after calling get_file_properties")
    
    if file_id.unique_id[:6] != secure_hash:
        logging.debug(f"Invalid hash for message with ID {message_id}")
        raise InvalidHash
    
    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = request.http_range.stop or file_size - 1

    part_size = 1024 * 1024
    first_part = math.floor(from_bytes / part_size)
    last_part = math.ceil(until_bytes / part_size)
    body=tg_connect.stream_media(index, file_id, file_size, last_part, first_part)

    mime_type = file_id.mime_type
    file_name = file_id.file_name
    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_id.file_name)
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"
    return_resp = web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": f"{mime_type}",
            "Range": f"bytes={from_bytes}-{until_bytes}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )

    if return_resp.status == 200:
        return_resp.headers.add("Content-Length", str(file_size))

    return return_resp
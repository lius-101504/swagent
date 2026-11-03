"""MCP server over stdio for SOLIDWORKS 2022.

The transport is the one MCP calls "stdio": newline-delimited JSON-RPC 2.0 on
stdin/stdout. stdout carries protocol messages only, so every diagnostic goes
to stderr - a stray print() on stdout silently corrupts the stream.

Run it with the interpreter that has pywin32 installed, not with a system
python:

    D:\\d\\swagent\\.venv\\Scripts\\python.exe -m swagent
"""
import json
import os
import sys
import traceback

from .errors import SwError
from .session import SessionWithExtras
from .tools import TOOLS, call

SERVER_NAME = "solidworks-mcp"
SERVER_VERSION = "1.0.0"
# MCP revisions this server is written against; the client's own revision is
# echoed back when it is one of these, so older clients keep working.
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_VERSION = "2025-06-18"

_session = None


def log(message):
    sys.stderr.write(f"[{SERVER_NAME}] {message}\n")
    sys.stderr.flush()


def _session_for_call():
    global _session
    if _session is None:
        _session = SessionWithExtras()
    return _session


def _result(msg_id, payload):
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


def _error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def handle(request):
    """Handle one JSON-RPC request; returns the response dict or None."""
    method = request.get("method")
    msg_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else DEFAULT_VERSION
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Drives a local SOLIDWORKS 2022 install. Start with sw_status: it reports the "
                    "attached instance and whether the licence server answers. All modelling "
                    "tools take millimetres and return the measured volume and bounding box, so "
                    "compare them with a hand calculation instead of trusting the call to succeed."
                ),
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _result(
                msg_id,
                {
                    "content": [{"type": "text", "text": "arguments must be a JSON object"}],
                    "isError": True,
                },
            )
        try:
            payload = call(_session_for_call(), name, arguments)
            text = json.dumps(payload, ensure_ascii=False, default=str)
            return _result(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except SwError as exc:
            log(f"{name} failed: {exc.code}: {exc}")
            return _result(
                msg_id,
                {
                    "content": [{"type": "text", "text": json.dumps(exc.to_dict(), ensure_ascii=False)}],
                    "isError": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 - must not kill the server
            log(f"{name} crashed:\n{traceback.format_exc()}")
            payload = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "code": "internal",
                "hint": "This is a bug in the server, not in the model. The traceback is on the server's stderr.",
            }
            return _result(
                msg_id,
                {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": True},
            )

    return _error(msg_id, -32601, f"Method not found: {method}")


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    log(f"started, python {sys.version.split()[0]}, pid {os.getpid()}")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            stdout.write(json.dumps(_error(None, -32700, f"Parse error: {exc}")) + "\n")
            stdout.flush()
            continue
        if isinstance(request, list):  # batch, tolerated but not required
            responses = [r for r in (handle(item) for item in request) if r]
            if responses:
                stdout.write(json.dumps(responses, ensure_ascii=False) + "\n")
        else:
            response = handle(request)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()
    log("stdin closed, exiting")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--list-tools" in argv:
        print(json.dumps({"tools": [t["name"] for t in TOOLS]}, indent=2))
        return 0
    if "--version" in argv:
        print(f"{SERVER_NAME} {SERVER_VERSION}")
        return 0
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

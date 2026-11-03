"""Talk to the server over stdio exactly as an MCP client would.

This exists because "the module imports" is not evidence the server works. It
runs the real JSON-RPC handshake, lists the tools, and calls sw_status - which
on a machine without a licence is the call that must fail with a useful
diagnosis instead of a stack trace.
"""
import json
import sys as _sys
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
LAUNCHER = "-m"
LAUNCH_MODULE = "swagent"


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            [PYTHON, LAUNCHER, LAUNCH_MODULE],
            cwd=HERE,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.next_id = 1
        self.stderr = ""

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self.next_id
            self.next_id += 1
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"server exited; stderr:\n{self.proc.stderr.read()}")
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        self.stderr = self.proc.stderr.read()


def text_of(response):
    result = response.get("result") or {}
    content = result.get("content") or []
    if not content:
        return json.dumps(result, ensure_ascii=False)[:2000]
    return content[0].get("text", "")


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    client = Client()
    failures = []
    try:
        init = client.send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp_probe", "version": "1.0"},
        })
        print("== initialize ==")
        print(json.dumps(init.get("result"), ensure_ascii=False, indent=2)[:800])
        if init.get("result", {}).get("serverInfo", {}).get("name") != "solidworks-mcp":
            failures.append("initialize did not identify the server")

        client.send("notifications/initialized", notify=True)

        listing = client.send("tools/list")
        tools = listing.get("result", {}).get("tools", [])
        print(f"\n== tools/list == {len(tools)} tools")
        for tool in tools:
            required = tool["inputSchema"].get("required", [])
            print(f"  {tool['name']:<20} required={required}")
        if len(tools) < 16:
            failures.append(f"expected 16 tools, got {len(tools)}")

        print("\n== tools/call sw_status ==")
        status = client.send("tools/call", {"name": "sw_status", "arguments": {}})
        payload = text_of(status)
        is_error = (status.get("result") or {}).get("isError")
        print("isError:", is_error)
        print(payload[:2000].encode('utf-8', 'replace').decode('utf-8'))
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {}
            failures.append("sw_status returned non-JSON text")
        if not is_error:
            failures.append("sw_status reported success but no session is available here")
        if is_error and parsed.get("code") != "not_connected":
            failures.append(f"expected code not_connected, got {parsed.get('code')!r}")
        blob = json.dumps(parsed, ensure_ascii=False).lower()
        if is_error and not ("licence" in blob or "solidworks" in blob):
            failures.append("the failure explained neither the licence nor SolidWorks")

        print("\n== tools/call sw_new_part (must refuse cleanly) ==")
        newpart = client.send("tools/call", {"name": "sw_new_part", "arguments": {}})
        print("isError:", (newpart.get("result") or {}).get("isError"))
        print(text_of(newpart)[:600])

        print("\n== tools/call sw_export outside the allowed root (must refuse) ==")
        escape = client.send("tools/call", {
            "name": "sw_export",
            "arguments": {"path": r"C:\Windows\Temp\escape.step"},
        })
        print("isError:", (escape.get("result") or {}).get("isError"))
        print(text_of(escape)[:400])

        print("\n== unknown method ==")
        bogus = client.send("resources/list")
        print(json.dumps(bogus.get("error"), ensure_ascii=False))
        if bogus.get("error", {}).get("code") != -32601:
            failures.append("unknown method did not return -32601")
    finally:
        client.close()

    print("\n== server stderr ==")
    print(client.stderr.strip()[:3000])

    print("\n== verdict ==")
    if failures:
        for item in failures:
            print("FAIL:", item)
        return 1
    print("PASS: handshake, tool list, and the licence-blocked failure path all behave.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

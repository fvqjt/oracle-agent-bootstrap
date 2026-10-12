"""MCP proxy: tunnel MCP calls from server to local MCP service.

Server sends mcp_call via WS -> runner forwards to 127.0.0.1:6942
-> response sent back via WS as mcp_response.
"""
import json
import urllib.error
import urllib.request


MCP_BASE = "http://127.0.0.1:6942"


def handle_mcp_call(msg: dict, ws_client):
    """Handle mcp_call message from server.

    msg: {type: mcp_call, session_id, method, path, body}
    """
    session_id = msg.get("session_id", "")
    method = msg.get("method", "GET")
    path = msg.get("path", "/mcp")
    body = msg.get("body")

    url = f"{MCP_BASE}{path}"
    headers = {"Content-Type": "application/json"}

    try:
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            resp_body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            resp_body = json.loads(e.read().decode())
        except Exception:
            resp_body = {"error": str(e)}
    except Exception as e:
        status = 0
        resp_body = {"error": str(e)}

    try:
        ws_client.send_json({
            "type": "mcp_response",
            "session_id": session_id,
            "status": status,
            "body": resp_body,
        })
    except Exception:
        pass

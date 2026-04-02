"""
RenderDoc Bridge Client
Communicates with the RenderDoc extension via file-based IPC.
"""

import json
import os
import tempfile
import time
import uuid
from typing import Any


# IPC directory (must match renderdoc_extension/socket_server.py)
IPC_DIR = os.path.join(tempfile.gettempdir(), "renderdoc_mcp")
REQUEST_FILE = os.path.join(IPC_DIR, "request.json")
RESPONSE_FILE = os.path.join(IPC_DIR, "response.json")
LOCK_FILE = os.path.join(IPC_DIR, "lock")


class RenderDocBridgeError(Exception):
    """Error communicating with RenderDoc bridge"""

    pass


class RenderDocBridge:
    """Client for communicating with RenderDoc extension via file-based IPC"""

    def __init__(self, host: str = "127.0.0.1", port: int = 19876):
        # host/port are kept for API compatibility but not used
        self.host = host
        self.port = port
        self.timeout = 30.0  # seconds
        self.method_timeouts = {
            "save_mesh_csv": 300.0,
            "save_texture": 120.0,
            "export_event_assets": 480.0,
        }

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Call a method on the RenderDoc extension"""
        # Check if IPC directory exists
        if not os.path.exists(IPC_DIR):
            raise RenderDocBridgeError(
                f"Cannot connect to RenderDoc MCP Bridge at {self.host}:{self.port}. "
                "Make sure RenderDoc is running with the MCP Bridge extension loaded."
            )

        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = self.method_timeouts.get(method, self.timeout)

        request = {
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }

        try:
            # Clean up any stale response file
            if os.path.exists(RESPONSE_FILE):
                os.remove(RESPONSE_FILE)

            # Create lock file to signal we're writing
            with open(LOCK_FILE, "w") as f:
                f.write("lock")

            # Write request
            with open(REQUEST_FILE, "w", encoding="utf-8") as f:
                json.dump(request, f)

            # Remove lock file to signal write complete
            os.remove(LOCK_FILE)

            # Wait for response
            start_time = time.time()
            while True:
                if os.path.exists(RESPONSE_FILE):
                    # Small delay to ensure file is fully written
                    time.sleep(0.01)

                    try:
                        # Read response
                        with open(RESPONSE_FILE, "r", encoding="utf-8") as f:
                            response = json.load(f)
                    except json.JSONDecodeError:
                        # The extension may have created the file but not yet
                        # finished replacing/flushing it. Retry until timeout.
                        time.sleep(0.05)
                        continue

                    # Clean up response file
                    os.remove(RESPONSE_FILE)

                    if "error" in response:
                        error = response["error"]
                        message = f"[{error['code']}] {error['message']}"
                        if error["code"] == -32601:
                            message = self._format_method_not_found_error(
                                method, error["message"]
                            )
                        raise RenderDocBridgeError(message)

                    return response.get("result")

                # Check timeout
                if time.time() - start_time > effective_timeout:
                    raise RenderDocBridgeError(
                        f"Request timed out after {effective_timeout:.0f}s"
                    )

                # Poll interval
                time.sleep(0.05)

        except RenderDocBridgeError:
            raise
        except Exception as e:
            raise RenderDocBridgeError(f"Communication error: {e}")

    def _format_method_not_found_error(self, method: str, original_message: str) -> str:
        """Provide a more actionable error for extension/server version mismatches."""
        extension_dir = os.path.join(
            os.environ.get("APPDATA", ""), "qrenderdoc", "extensions", "renderdoc_mcp_bridge"
        )
        return (
            f"[-32601] {original_message}. "
            f"This usually means the RenderDoc extension loaded by qrenderdoc is older than "
            f"the MCP server and does not implement '{method}' yet. "
            f"Reinstall the extension into '{extension_dir}' and restart RenderDoc."
        )

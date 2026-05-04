"""LSP JSON-RPC client with message queue for Toroidal-Indexer Tier 2.

Key design: a background reader thread consumes all stdout from the LSP server,
routing responses (by ID) to waiting Events and notifications to handlers.
This solves the interleaved notification problem — send/recv never blocks on
unexpected messages.
"""

import json
import logging
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class LSPClient:
    def __init__(
        self,
        command: str,
        args: list[str],
        root_uri: str,
        capabilities: dict | None = None,
    ):
        self._next_id = 1
        self._lock = threading.Lock()
        self._responses: dict[int, threading.Event] = {}
        self._response_data: dict[int, dict] = {}
        self._diagnostics_events: dict[str, threading.Event] = {}
        self._shutdown_flag = False
        self._initialized = False
        self._server_capabilities = None

        full_cmd = [command] + args
        self._process = subprocess.Popen(
            full_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        self._do_initialize(root_uri, capabilities)

    def _reader_loop(self):
        try:
            while not self._shutdown_flag:
                msg = self._read_message()
                if msg is None:
                    break
                self._dispatch(msg)
        except Exception:
            pass

    def _read_message(self) -> dict | None:
        stdout = self._process.stdout
        if stdout is None:
            return None
        content_length = -1
        while True:
            line = stdout.readline()
            if not line:
                return None
            line_str = line.decode("utf-8", errors="replace").strip()
            if line_str == "":
                break
            if line_str.lower().startswith("content-length:"):
                content_length = int(line_str.split(":")[1].strip())
        if content_length < 0:
            return None
        body = stdout.read(content_length)
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def _dispatch(self, msg: dict):
        if "id" in msg and "method" not in msg:
            msg_id = msg["id"]
            event = self._responses.get(msg_id)
            if event:
                self._response_data[msg_id] = msg
                event.set()
        elif msg.get("method") == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            uri = params.get("uri", "")
            event = self._diagnostics_events.get(uri)
            if event:
                event.set()

    def _send_message(self, msg: dict):
        if self._process.stdin is None:
            return
        body = json.dumps(msg)
        header = f"Content-Length: {len(body)}\r\n\r\n"
        try:
            self._process.stdin.write(header.encode("utf-8"))
            self._process.stdin.write(body.encode("utf-8"))
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _do_initialize(self, root_uri: str, capabilities: dict | None):
        caps = capabilities or {}
        caps.setdefault("textDocument", {})
        caps["textDocument"].setdefault("publishDiagnostics", {})

        result = self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": caps,
                "initializationOptions": {},
            },
            timeout=30.0,
        )

        if result:
            self._server_capabilities = result.get("capabilities", {})
            self._initialized = True
            self.notify("initialized", {})

    def request(self, method: str, params: dict, timeout: float = 10.0) -> dict | None:
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1

        event = threading.Event()
        self._responses[msg_id] = event

        self._send_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params,
            }
        )

        if not event.wait(timeout=timeout):
            self._responses.pop(msg_id, None)
            return None

        self._responses.pop(msg_id, None)
        data = self._response_data.pop(msg_id, None)
        if data is None:
            return None
        if "error" in data:
            logger.debug("LSP error for %s: %s", method, data["error"])
            return None
        return data.get("result")

    def notify(self, method: str, params: dict):
        self._send_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    def did_open(self, file_path: str, language_id: str):
        uri = Path(file_path).as_uri()
        try:
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except (OSError, FileNotFoundError):
            text = ""
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                },
            },
        )

    def wait_for_diagnostics(self, uri: str, timeout: float = 30.0) -> bool:
        event = self._diagnostics_events.get(uri)
        if event and event.is_set():
            return True
        event = threading.Event()
        self._diagnostics_events[uri] = event
        return event.wait(timeout=timeout)

    def did_close(self, file_path: str):
        uri = Path(file_path).as_uri()
        self.notify(
            "textDocument/didClose",
            {
                "textDocument": {"uri": uri},
            },
        )
        self._diagnostics_events.pop(uri, None)

    def document_symbol(self, file_path: str) -> list[dict] | None:
        uri = Path(file_path).as_uri()
        result = self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": uri}},
        )
        if result is None:
            return None
        return result

    def definition(
        self, file_path: str, line: int, character: int
    ) -> list[dict] | None:
        uri = Path(file_path).as_uri()
        result = self.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
            },
        )
        if result is None:
            return None
        if isinstance(result, dict):
            return [result]
        return result

    def implementation(
        self, file_path: str, line: int, character: int
    ) -> list[dict] | None:
        uri = Path(file_path).as_uri()
        result = self.request(
            "textDocument/implementation",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
            },
        )
        if result is None:
            return None
        if isinstance(result, dict):
            return [result]
        return result

    def references(
        self, file_path: str, line: int, character: int
    ) -> list[dict] | None:
        uri = Path(file_path).as_uri()
        result = self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": True},
            },
        )
        if result is None:
            return None
        return result

    def prepare_call_hierarchy(
        self, file_path: str, line: int, character: int
    ) -> list[dict] | None:
        uri = Path(file_path).as_uri()
        result = self.request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
            },
        )
        if result is None:
            return None
        return result

    def incoming_calls(self, item: dict) -> list[dict] | None:
        result = self.request("callHierarchy/incomingCalls", {"item": item})
        if result is None:
            return None
        return result

    def outgoing_calls(self, item: dict) -> list[dict] | None:
        result = self.request("callHierarchy/outgoingCalls", {"item": item})
        if result is None:
            return None
        return result

    def hover(self, file_path: str, line: int, character: int) -> dict | None:
        uri = Path(file_path).as_uri()
        result = self.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
            },
        )
        return result

    def shutdown(self):
        if self._shutdown_flag:
            return
        self._shutdown_flag = True

        try:
            self.request("shutdown", {}, timeout=5.0)
            self.notify("exit", {})
        except Exception:
            pass

        try:
            self._process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=2.0)

        self._reader_thread.join(timeout=2.0)

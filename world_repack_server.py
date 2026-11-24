import base64
import binascii
import json
import re
import secrets
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
PAGE_PATH = ROOT / "World_Repack.html"
COMPILER_PATH = ROOT / "EaglerBinaryTools.jar"
DATABASE_NAME = "_net_lax1dude_eaglercraft_v1_8_internal_PlatformFilesystem_1_12_2_"
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
CHUNK_SIZE = 512 * 1024
UPLOAD_TTL_SECONDS = 3600
PORT = 8001
UPLOADS = {}
UPLOADS_LOCK = threading.Lock()


class RepackHandler(BaseHTTPRequestHandler):
    server_version = "WorldRepack/1.0"
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def send_text(self, status, message):
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path not in ("/", "/World_Repack.html"):
            self.send_text(404, "Not found")
            return
        try:
            body = PAGE_PATH.read_bytes()
        except OSError:
            self.send_text(500, "World_Repack.html is missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if urlparse(self.path).path not in ("/", "/World_Repack.html"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            content_length = PAGE_PATH.stat().st_size
        except OSError:
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/upload/start":
            self.start_upload()
        elif path == "/upload/chunk":
            self.receive_chunk()
        elif path == "/repack":
            self.finish_repack()
        else:
            self.send_text(404, "Not found")

    def do_DELETE(self):
        if urlparse(self.path).path != "/upload/cancel":
            self.send_text(404, "Not found")
            return
        token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        with UPLOADS_LOCK:
            session = UPLOADS.pop(token, None)
        if session:
            session["temporary"].cleanup()
        self.send_json(200, {"cancelled": session is not None})

    def start_upload(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_text(400, "Invalid Content-Length")
            return
        if content_length <= 0 or content_length > 4096:
            self.send_text(400, "Invalid upload initialization request")
            return
        try:
            request = json.loads(self.rfile.read(content_length))
            total = request.get("total")
        except (json.JSONDecodeError, AttributeError):
            self.send_text(400, "Invalid upload initialization request")
            return
        if type(total) is not int or total <= 0 or total > MAX_UPLOAD_BYTES:
            self.send_text(413, "Choose a JSON backup smaller than 256 MB")
            return

        now = time.time()
        expired = []
        with UPLOADS_LOCK:
            for old_token, old_session in list(UPLOADS.items()):
                if now - old_session["updated"] > UPLOAD_TTL_SECONDS:
                    expired.append(UPLOADS.pop(old_token))
        for old_session in expired:
            old_session["temporary"].cleanup()

        temporary = tempfile.TemporaryDirectory(prefix="eagler-world-upload-")
        backup_path = Path(temporary.name) / "backup.json"
        with backup_path.open("wb") as backup_file:
            backup_file.truncate(total)
        token = secrets.token_urlsafe(24)
        with UPLOADS_LOCK:
            UPLOADS[token] = {
                "temporary": temporary,
                "backup_path": backup_path,
                "total": total,
                "received": 0,
                "chunks": set(),
                "lock": threading.Lock(),
                "updated": now,
            }
        self.send_json(201, {"token": token, "chunkSize": CHUNK_SIZE})

    def receive_chunk(self):
        token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        with UPLOADS_LOCK:
            session = UPLOADS.get(token)
        if not session:
            self.send_text(404, "Upload session expired; start again")
            return
        content_range = self.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        if not match:
            self.send_text(400, "Missing or invalid Content-Range")
            return
        start, end, total = map(int, match.groups())
        length = end - start + 1
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_text(400, "Invalid Content-Length")
            return
        expected_length = min(CHUNK_SIZE, session["total"] - start) if 0 <= start < session["total"] else 0
        if total != session["total"] or start % CHUNK_SIZE != 0 or length != expected_length or content_length != length:
            self.send_text(400, "Chunk range does not match this upload")
            return
        chunk = self.rfile.read(content_length)
        if len(chunk) != content_length:
            self.send_text(400, "The uploaded chunk was incomplete")
            return
        with session["lock"]:
            if start in session["chunks"]:
                self.send_text(409, "This chunk was already received")
                return
            with session["backup_path"].open("r+b") as backup_file:
                backup_file.seek(start)
                backup_file.write(chunk)
            session["chunks"].add(start)
            session["received"] += content_length
            session["updated"] = time.time()
            received = session["received"]
        self.send_json(200, {"received": received, "total": session["total"]})

    def finish_repack(self):
        if not COMPILER_PATH.is_file():
            self.send_text(500, "EaglerBinaryTools.jar was not found beside the repack server")
            return
        query = parse_qs(urlparse(self.path).query)
        token = query.get("token", [""])[0]
        world_name = query.get("world", [""])[0]
        compression = query.get("compression", ["gzip"])[0]
        if not world_name.strip() or "/" in world_name or "\\" in world_name or world_name in (".", ".."):
            self.send_text(400, "Enter the exact world name shown in World Vault")
            return
        if compression not in ("gzip", "zlib", "none"):
            self.send_text(400, "Compression must be gzip, zlib, or none")
            return
        with UPLOADS_LOCK:
            session = UPLOADS.get(token)
        if not session:
            self.send_text(404, "Upload session expired; choose the backup and try again")
            return
        expected_chunks = (session["total"] + CHUNK_SIZE - 1) // CHUNK_SIZE
        with session["lock"]:
            if len(session["chunks"]) != expected_chunks:
                self.send_text(400, "The backup upload is incomplete; retry the missing chunks")
                return
            with UPLOADS_LOCK:
                UPLOADS.pop(token, None)

        try:
            with tempfile.TemporaryDirectory(prefix="eagler-world-repack-") as temporary:
                temporary_path = Path(temporary)
                with session["backup_path"].open("r", encoding="utf-8") as backup_file:
                    backup = json.load(backup_file)
                if not isinstance(backup, dict):
                    raise ValueError("The selected backup must contain a JSON object")
                if backup.get("format") != "eaglercraft-indexeddb-backup":
                    raise ValueError("This is not an Eaglercraft IndexedDB backup")
                if backup.get("database") != DATABASE_NAME or backup.get("version") != 1:
                    raise ValueError("This backup is for a different Eaglercraft database or version")
                records = backup.get("stores", {}).get("filesystem")
                if not isinstance(records, list):
                    raise ValueError("The backup does not contain a filesystem store")

                prefix = f"eaglercraft/worlds/{world_name}/"
                world_directory = temporary_path / "world"
                world_directory.mkdir()
                written_paths = set()
                file_count = 0
                for record in records:
                    value = record.get("value", {}) if isinstance(record, dict) else {}
                    path = value.get("path")
                    if not isinstance(path, str) or not path.startswith(prefix):
                        continue
                    relative_name = path[len(prefix):]
                    relative_path = PurePosixPath(relative_name)
                    if not relative_name or relative_path.is_absolute() or any(part in ("", ".", "..") for part in relative_path.parts):
                        raise ValueError("The selected world contains an invalid file path")
                    if relative_name in written_paths:
                        raise ValueError("The selected world contains duplicate file paths")
                    written_paths.add(relative_name)
                    data = value.get("data", {})
                    if data.get("type") != "ArrayBuffer" or not isinstance(data.get("base64"), str):
                        raise ValueError("A selected world file has an unsupported data format")
                    try:
                        file_bytes = base64.b64decode(data["base64"], validate=True)
                    except (ValueError, binascii.Error) as error:
                        raise ValueError("A selected world file contains invalid base64 data") from error
                    destination = world_directory.joinpath(*relative_path.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(file_bytes)
                    file_count += 1

                if file_count == 0:
                    raise ValueError(f"No files found for world '{world_name}' in this backup")

                output_path = temporary_path / "world.epk"
                result = subprocess.run(
                    ["java", "-jar", str(COMPILER_PATH), "epkcompiler", str(world_directory), str(output_path), compression],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if result.returncode != 0 or not output_path.is_file():
                    detail = (result.stderr or result.stdout).strip()
                    raise RuntimeError(detail or "The EPK compiler could not package this world")

                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", world_name).strip("._") or "world"
                download_name = f"eaglercraft-{safe_name}.epk"
                output_size = output_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(output_size))
                self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
                self.send_header("X-World-File-Count", str(file_count))
                self.end_headers()
                with output_path.open("rb") as output_file:
                    while chunk := output_file.read(1024 * 1024):
                        self.wfile.write(chunk)
        except json.JSONDecodeError:
            self.send_text(400, "Could not parse the selected JSON backup")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            status = 504 if isinstance(error, subprocess.TimeoutExpired) else 400
            self.send_text(status, str(error))
        finally:
            session["temporary"].cleanup()


if __name__ == "__main__":
    if not PAGE_PATH.is_file():
        raise SystemExit("World_Repack.html is missing")
    print(f"World Repack Lab listening on http://0.0.0.0:{PORT}/")
    ThreadingHTTPServer(("0.0.0.0", PORT), RepackHandler).serve_forever()
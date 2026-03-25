#!/usr/bin/env python3
"""
Syllabus Editor Server — port 9092
Parses COURSE_DATA from index.html, serves editor.html, saves changes back.
"""
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import urllib.parse
from pathlib import Path

PORT = 9092
BASE_DIR = Path(__file__).parent.resolve()
INDEX_HTML = BASE_DIR / "index.html"
EDITOR_HTML = BASE_DIR / "editor.html"
IMAGES_DIR = BASE_DIR / "images"


# ---------------------------------------------------------------------------
# COURSE_DATA extraction / injection
# ---------------------------------------------------------------------------

def find_course_data_bounds(html: str):
    """Return (start, end) char positions of 'const COURSE_DATA = [...];' in html."""
    start_match = re.search(r'const COURSE_DATA\s*=\s*(\[)', html)
    if not start_match:
        return None, None

    bracket_start = start_match.start(1)
    depth = 0
    i = bracket_start
    in_string = False
    string_char = None

    while i < len(html):
        c = html[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == string_char:
                in_string = False
        else:
            if c in ('"', "'", '`'):
                in_string = True
                string_char = c
            elif c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    if end < len(html) and html[end] == ';':
                        end += 1
                    return start_match.start(), end
        i += 1

    return None, None


def extract_modules(html: str):
    """Extract COURSE_DATA from HTML and return as Python list."""
    start, end = find_course_data_bounds(html)
    if start is None:
        return []

    # Grab just the array portion
    array_start = html.index('[', start)
    array_str = html[array_start:end].rstrip(';')

    # Try direct JSON parse (works after first editor save)
    try:
        modules = json.loads(array_str)
        for m in modules:
            m.setdefault('rendus', [])
            m.setdefault('extraBlocks', [])
            m.setdefault('colors', {})
            m.setdefault('heights', {})
        return modules
    except json.JSONDecodeError:
        pass

    # Fallback: convert JS object literal → JSON via tokenizer
    return _js_to_json(array_str)


def _js_to_json(js: str) -> list:
    """Best-effort JS object literal to Python list converter (string-aware)."""
    transformed = _transform_js_to_json_str(js)
    # Remove trailing commas before } or ]
    transformed = re.sub(r',\s*([}\]])', r'\1', transformed)
    try:
        result = json.loads(transformed)
        for m in result:
            m.setdefault('rendus', [])
            m.setdefault('extraBlocks', [])
            m.setdefault('colors', {})
            m.setdefault('heights', {})
        return result
    except json.JSONDecodeError as e:
        print("[WARN] JS-to-JSON parse failed: " + str(e))
        return []


def _transform_js_to_json_str(js: str) -> str:
    """
    Single-pass, string-aware JS object literal -> JSON string converter.
    Handles: unquoted keys, single-quoted strings, // and /* */ comments.
    Never mangles content inside string values.
    """
    result = []
    i = 0
    n = len(js)

    while i < n:
        c = js[i]

        # Single-line comment
        if c == '/' and i + 1 < n and js[i + 1] == '/':
            while i < n and js[i] != '\n':
                i += 1
            continue

        # Multi-line comment
        if c == '/' and i + 1 < n and js[i + 1] == '*':
            i += 2
            while i < n - 1:
                if js[i] == '*' and js[i + 1] == '/':
                    i += 2
                    break
                i += 1
            continue

        # Single-quoted string: convert to double-quoted
        if c == "'":
            j = i + 1
            inner = []
            while j < n:
                ch = js[j]
                if ch == '\\' and j + 1 < n:
                    nxt = js[j + 1]
                    if nxt == "'":
                        inner.append("'")
                    elif nxt == '"':
                        inner.append('\\"')
                    else:
                        inner.append('\\' + nxt)
                    j += 2
                    continue
                if ch == "'":
                    j += 1
                    break
                if ch == '"':
                    inner.append('\\"')
                elif ch == '\n':
                    inner.append('\\n')
                elif ch == '\r':
                    pass
                else:
                    inner.append(ch)
                j += 1
            result.append('"')
            result.extend(inner)
            result.append('"')
            i = j
            continue

        # Double-quoted string: copy verbatim
        if c == '"':
            result.append(c)
            i += 1
            while i < n:
                ch = js[i]
                result.append(ch)
                if ch == '\\' and i + 1 < n:
                    i += 1
                    result.append(js[i])
                elif ch == '"':
                    i += 1
                    break
                i += 1
            continue

        # Identifier: check if it is an unquoted object key
        if c.isalpha() or c in ('_', '$'):
            j = i
            while j < n and (js[j].isalnum() or js[j] in ('_', '$')):
                j += 1
            word = js[i:j]
            k = j
            while k < n and js[k] in (' ', '\t', '\n', '\r'):
                k += 1
            if k < n and js[k] == ':' and (k + 1 >= n or js[k + 1] != ':'):
                result.append('"' + word + '"')  # quote the key
            else:
                result.append(word)
            i = j
            continue

        result.append(c)
        i += 1

    return ''.join(result)


def modules_to_js(modules: list) -> str:
    """Serialise modules list as a JS const declaration (pure JSON syntax)."""
    return 'const COURSE_DATA = ' + json.dumps(modules, ensure_ascii=False, indent=2) + ';'


def update_html_modules(html: str, modules: list) -> str:
    """Replace the COURSE_DATA declaration in html with updated modules."""
    start, end = find_course_data_bounds(html)
    if start is None:
        return html
    return html[:start] + modules_to_js(modules) + html[end:]


# ---------------------------------------------------------------------------
# SITE_SETTINGS extraction / injection
# ---------------------------------------------------------------------------

def find_site_settings_bounds(html: str):
    """Return (start, end) char positions of 'const SITE_SETTINGS = {...};' in html."""
    start_match = re.search(r'const SITE_SETTINGS\s*=\s*(\{)', html)
    if not start_match:
        return None, None

    brace_start = start_match.start(1)
    depth = 0
    i = brace_start
    in_string = False
    string_char = None

    while i < len(html):
        c = html[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == string_char:
                in_string = False
        else:
            if c in ('"', "'", '`'):
                in_string = True
                string_char = c
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    if end < len(html) and html[end] == ';':
                        end += 1
                    return start_match.start(), end
        i += 1

    return None, None


def extract_site_settings(html: str) -> dict:
    """Extract SITE_SETTINGS from HTML and return as Python dict."""
    start, end = find_site_settings_bounds(html)
    if start is None:
        return {"siteTitle": "3D Masterclass", "siteSubtitle": "Syllabus Interactif", "siteBadge": "Parcours Pro • 10 Semaines", "labelPlanning": "Planning de la semaine", "labelDay": "Jour", "labelObjective": "Objectif", "labelDeliverables": "Livrables", "pages": []}

    obj_start = html.index('{', start)
    obj_str = html[obj_start:end].rstrip(';')

    try:
        return json.loads(obj_str)
    except json.JSONDecodeError:
        return {"siteTitle": "3D Masterclass", "siteSubtitle": "Syllabus Interactif", "siteBadge": "Parcours Pro • 10 Semaines", "labelPlanning": "Planning de la semaine", "labelDay": "Jour", "labelObjective": "Objectif", "labelDeliverables": "Livrables", "pages": []}


def site_settings_to_js(settings: dict) -> str:
    """Serialise site settings as a JS const declaration."""
    return 'const SITE_SETTINGS = ' + json.dumps(settings, ensure_ascii=False, indent=2) + ';'


def update_html_site_settings(html: str, settings: dict) -> str:
    """Replace the SITE_SETTINGS declaration in html."""
    start, end = find_site_settings_bounds(html)
    if start is None:
        return html
    return html[:start] + site_settings_to_js(settings) + html[end:]


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class EditorHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._send(code, 'application/json; charset=utf-8', body)

    def _send_error(self, msg: str, code: int = 500):
        self._send_json({'error': msg}, code)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ('/', '/editor', '/editor.html'):
            self._serve_file(EDITOR_HTML, 'text/html; charset=utf-8')

        elif path == '/api/modules':
            try:
                html = INDEX_HTML.read_text(encoding='utf-8')
                modules = extract_modules(html)
                self._send_json(modules)
            except Exception as e:
                self._send_error(str(e))

        elif path == '/api/settings':
            try:
                html = INDEX_HTML.read_text(encoding='utf-8')
                settings = extract_site_settings(html)
                self._send_json(settings)
            except Exception as e:
                self._send_error(str(e))

        elif path.startswith('/images/'):
            rel = urllib.parse.unquote(path[1:])  # strip leading / and decode %20, %C3%A9, etc.
            # Prevent path traversal
            full = (BASE_DIR / rel).resolve()
            try:
                full.relative_to(BASE_DIR)
            except ValueError:
                self._send_error('Forbidden', 403)
                return
            self._serve_file(full)

        else:
            self._send_error('Not found', 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/api/modules':
            self._handle_save_modules()
        elif path == '/api/settings':
            self._handle_save_settings()
        elif path == '/api/upload':
            self._handle_upload()
        elif path == '/api/publish':
            self._handle_publish()
        else:
            self._send_error('Not found', 404)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_publish(self):
        """git add index.html + commit + push, then return ok."""
        try:
            def run_git(*args):
                return subprocess.run(
                    ['git'] + list(args),
                    cwd=str(BASE_DIR),
                    capture_output=True, text=True, timeout=60
                )
            r = run_git('add', 'index.html')
            if r.returncode != 0:
                self._send_json({'ok': False, 'error': (r.stderr or r.stdout or 'git add failed').strip()})
                return
            r = run_git('commit', '-m', 'Syllabus update')
            # exit 1 = nothing to commit — not a real error
            if r.returncode not in (0, 1):
                self._send_json({'ok': False, 'error': (r.stderr or r.stdout or 'git commit failed').strip()})
                return
            r = run_git('push')
            if r.returncode != 0:
                self._send_json({'ok': False, 'error': (r.stderr or r.stdout or 'git push failed').strip()})
                return
            self._send_json({'ok': True})
        except subprocess.TimeoutExpired:
            self._send_json({'ok': False, 'error': 'git timeout (>60s)'})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_save_modules(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            modules = json.loads(body)

            html = INDEX_HTML.read_text(encoding='utf-8')
            new_html = update_html_modules(html, modules)

            # Backup before writing
            backup = INDEX_HTML.with_suffix('.html.bak')
            shutil.copy2(INDEX_HTML, backup)

            INDEX_HTML.write_text(new_html, encoding='utf-8')
            self._send_json({'ok': True})
        except Exception as e:
            self._send_error(str(e))

    def _handle_save_settings(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            settings = json.loads(body)

            html = INDEX_HTML.read_text(encoding='utf-8')
            new_html = update_html_site_settings(html, settings)

            backup = INDEX_HTML.with_suffix('.html.bak')
            shutil.copy2(INDEX_HTML, backup)

            INDEX_HTML.write_text(new_html, encoding='utf-8')
            self._send_json({'ok': True})
        except Exception as e:
            self._send_error(str(e))

    def _handle_upload(self):
        """Multipart upload: expects fields 'moduleId', 'student', 'file'."""
        try:
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._send_error('Expected multipart/form-data', 400)
                return

            boundary_match = re.search(r'boundary=(.+)', content_type)
            if not boundary_match:
                self._send_error('No boundary in Content-Type', 400)
                return
            boundary = boundary_match.group(1).strip().encode()

            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)

            parts = _parse_multipart(raw, boundary)
            module_id = parts.get('moduleId', b'unknown').decode()
            filename = parts.get('filename', b'image.jpg').decode()

            # Sanitise filename
            filename = os.path.basename(filename)
            filename = re.sub(r'[^\w.\-]', '_', filename)

            dest_dir = IMAGES_DIR / f'rendus' / f'module_{module_id}'
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / filename

            # Avoid overwrite collisions
            if dest.exists():
                stem, suffix = os.path.splitext(filename)
                import time
                filename = f"{stem}_{int(time.time())}{suffix}"
                dest = dest_dir / filename

            dest.write_bytes(parts.get('file', b''))

            rel_path = dest.relative_to(BASE_DIR).as_posix()
            self._send_json({'ok': True, 'path': rel_path})
        except Exception as e:
            self._send_error(str(e))

    def _serve_file(self, path: Path, content_type: str = None):
        path = Path(path)
        if not path.exists():
            self._send_error('Not found', 404)
            return
        if content_type is None:
            suffix = path.suffix.lower()
            content_type = {
                '.html': 'text/html; charset=utf-8',
                '.css': 'text/css; charset=utf-8',
                '.js': 'application/javascript; charset=utf-8',
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif',
                '.webp': 'image/webp', '.svg': 'image/svg+xml',
            }.get(suffix, 'application/octet-stream')
        self._send(200, content_type, path.read_bytes())


# ---------------------------------------------------------------------------
# Multipart parser (stdlib only)
# ---------------------------------------------------------------------------

def _parse_multipart(data: bytes, boundary: bytes) -> dict:
    """Simple multipart/form-data parser. Returns {field_name: value_bytes}."""
    result = {}
    delimiter = b'--' + boundary
    parts = data.split(delimiter)
    for part in parts[1:]:
        if part.startswith(b'--') or not part.strip():
            continue
        # Split headers from body
        if b'\r\n\r\n' in part:
            headers_raw, body = part.split(b'\r\n\r\n', 1)
        else:
            continue
        body = body.rstrip(b'\r\n')
        headers_raw = headers_raw.decode('utf-8', errors='replace')
        disp_match = re.search(r'Content-Disposition:.*?name="([^"]+)"', headers_raw, re.I)
        if not disp_match:
            continue
        name = disp_match.group(1)
        filename_match = re.search(r'filename="([^"]+)"', headers_raw, re.I)
        if filename_match:
            result['filename'] = filename_match.group(1).encode()
            result[name] = body
        else:
            result[name] = body
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    os.chdir(BASE_DIR)
    print(f"Syllabus Editor  →  http://localhost:{PORT}/")
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(('', PORT), EditorHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")

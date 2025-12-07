import docker
import urllib.parse
import webbrowser
import time
import threading
import asyncio
import io
import tarfile
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver
from typing import Dict, Any, List, Tuple

from shlex import quote
import sys # Needed for sys.exit in main()

# --- Configuration ---
MONITOR_PORT = 8090
REFRESH_INTERVAL = 1  # Faster polling/refresh: 1 second

# --- Global State for the Server ---
# Stores the history of all rollouts received
ROLLOUT_HISTORY: Dict[str, Dict[str, Any]] = {} 
# Stores the currently active rollout ID and its container ID
ACTIVE_ROLLOUT: Dict[str, str] = {"rollout_id": None, "container_id": None}
# Lock to synchronize access to shared state (ROLLOUT_HISTORY, ACTIVE_ROLLOUT)
STATE_LOCK = threading.Lock() 

# Place these functions immediately after the DOCKER_CLIENT definition 
# and before the start_monitor_loop function.



# 1. Asynchronous Execute (The logic wrapped from your DockerEnv)
async def execute_in_container_async(container, command: str, timeout: int = 5, workdir: str = "/workspace"):
    """Core async wrapper to execute a command."""
    loop = asyncio.get_running_loop()
    
    def _exec():
        # This is the synchronous, blocking Docker SDK call
        exit_code, output = container.exec_run(
            f"bash -c {quote(command)}", 
            demux=True, 
            workdir=workdir,
            user="root",
        )
        return exit_code, output

    try:
        exit_code, output = await loop.run_in_executor(None, _exec)
        stdout, stderr = output if output else (None, None)
        
        stdout = stdout if isinstance(stdout, bytes) else b''
        stderr = stderr if isinstance(stderr, bytes) else b''
        
        return exit_code, (stdout, stderr) 
        
    except Exception as e:
        return -1, (b'', str(e).encode())

# 2. Synchronous Wrapper (Needed because start_monitor_loop is a synchronous thread)
def sync_exec_in_container(container, command: str) -> Tuple[int, Tuple[bytes, bytes]]:
    """Synchronously executes async code in a background thread."""
    
    # 💡 FIX: Explicitly create, set, run, and close a new loop for this thread.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Note the change to use the new function name `execute_in_container_async`
        return loop.run_until_complete(execute_in_container_async(container, command, timeout=5))
    finally:
        # IMPORTANT: Clean up the loop we created
        loop.close()

# 3. Core Monitoring Logic (Uses the new synchronous wrapper)
# 3. Core Monitoring Logic (Uses the stable wrapper)
# 3. Core Monitoring Logic (Uses the stable wrapper)
def get_workspace_state_via_bash(container) -> Dict[str, str]:
    """Reads workspace files and directories by synchronously running bash commands."""
    
    # 1. Get a list of ALL contents (files and directories) recursively.
    exit_code, (stdout, stderr) = sync_exec_in_container(
        container, 
        'find /workspace -mindepth 1 -print' # Use -mindepth 1 to skip /workspace itself
    )

    if exit_code != 0:
        return {"error": f"Failed to list contents (Exit {exit_code}). STDOUT: {stdout.decode()}, STDERR: {stderr.decode()}"}

    # Clean the list of paths
    full_path_list = [p.strip() for p in stdout.decode().split('\n') if p.strip()]
    file_map = {}

    # 2. Process each path, checking if it's a file or a directory before reading
    for full_path in full_path_list:
        if not full_path or full_path == "/workspace":
            continue
            
        # Keys are relative paths
        relative_path = full_path.replace('/workspace/', '', 1) 

        # A. Check if the path is a regular file using bash 'test -f'
        # This is the key difference and provides reliable type checking.
        is_file_exit_code, _ = sync_exec_in_container(
            container, 
            f'test -f {full_path}'
        )
        
        if is_file_exit_code == 0:
            # It is a regular file, so read its contents
            exit_code, (content, _) = sync_exec_in_container(
                container, 
                f'cat {full_path}'
            )
            
            if exit_code == 0:
                file_map[relative_path] = content.decode('utf-8', errors='ignore')
            else:
                file_map[relative_path] = f"[ERROR READING FILE: cat exit {exit_code}]"

        else:
            # It is not a regular file (i.e., it's a directory or link/other)
            # Use the directory placeholder
            file_map[relative_path] = "[[DIRECTORY]]"
            
    return file_map


# --- Docker Client (Synchronous) ---
DOCKER_CLIENT = docker.from_env()

# --- Utility Functions (Place these in monitor_server.py) ---
# Use the fast, command-based execution from the previous fix.
# NOTE: You MUST include the full definitions of 'sync_exec_in_container', 
# 'execute_in_container' (which uses loop.run_until_complete), and 
# 'get_workspace_state_via_bash' from your environment file here!

# For brevity, I'll assume get_workspace_state_via_bash is defined here.
# def get_workspace_state_via_bash(container) -> Dict[str, str]: ... 





# --- Monitoring Loop Thread ---
def start_monitor_loop():
    """Runs a background loop to poll the active container's state."""
    print("Starting background Docker polling loop...")
    while True:
        with STATE_LOCK:
            rollout_id = ACTIVE_ROLLOUT.get("rollout_id")
            container_id = ACTIVE_ROLLOUT.get("container_id")

        if rollout_id and container_id:
            try:
                container = DOCKER_CLIENT.containers.get(container_id)
                # Fetch state using the fast bash commands
                file_map = get_workspace_state_via_bash(container) 
                
                with STATE_LOCK:
                    # Update history with the latest files
                    if rollout_id in ROLLOUT_HISTORY:
                        ROLLOUT_HISTORY[rollout_id]['files'] = file_map
                        ROLLOUT_HISTORY[rollout_id]['status'] = "Active"
                        ROLLOUT_HISTORY[rollout_id]['last_updated'] = time.strftime('%H:%M:%S')

            except docker.errors.NotFound:
                # Container likely cleaned up by environment's teardown_state
                with STATE_LOCK:
                    if rollout_id in ROLLOUT_HISTORY:
                        ROLLOUT_HISTORY[rollout_id]['status'] = "Finished (Container Gone)"
                    ACTIVE_ROLLOUT["container_id"] = None
                    ACTIVE_ROLLOUT["rollout_id"] = None
            except Exception as e:
                print(f"Polling error for {container_id}: {e}")

        time.sleep(REFRESH_INTERVAL)

# --- HTTP Request Handler (Modified to read history) ---
class MonitorHandler(BaseHTTPRequestHandler):
    def _set_cors_headers(self):
        """Adds necessary CORS headers to allow cross-origin requests from the UI app."""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')


    def do_OPTIONS(self):
        """Handles the CORS preflight request."""
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    # --- GET Handler ---
    def do_GET(self):
        self.send_response(200)
        self._set_cors_headers() # Add CORS headers here
        
        # 1. Handle JSON API call from the external UI tool
        if self.path.startswith('/api/history'):
            self._serve_json_history() # This method will now ONLY set content-type and write data
            return
            
        # 2. Handle HTML Web Page View (Fallback/Default)
        self._serve_web_page()

    # --- POST Handler ---
    def do_POST(self):
        self.send_response(200)
        self._set_cors_headers() # Add CORS headers here
        
        # 1. Handle Setup API Calls from the environment (Client)
        if self.path.startswith('/api/start'):
            self._handle_api_start()
            return
        if self.path.startswith('/api/end'):
            self._handle_api_end()
            return

        # Fallback for unhandled POSTs
        self.send_response(404)
        self.end_headers() # This will send the headers including CORS

    def _serve_json_history(self):
        # NOTE: send_response(200) and _set_cors_headers() are now called in do_GET
        self.send_header("Content-type", "application/json")
        self.end_headers() # Only send the final headers here

        with STATE_LOCK:
            response_data = {
                "active_id": ACTIVE_ROLLOUT.get("rollout_id"),
                "history": ROLLOUT_HISTORY
            }
        
        response_json = json.dumps(response_data, indent=2)
        self.wfile.write(response_json.encode("utf-8"))


    def _handle_api_start(self):
        """Receives new rollout information from DockerEnv."""
        # ... (The rest of this function is correct) ...
        content_len = int(self.headers.get('Content-Length', 0))
        post_body = self.rfile.read(content_len)
        data = json.loads(post_body.decode('utf-8'))

        rollout_id = data.get("rollout_id", f"run_{int(time.time())}")
        container_id = data["container_id"]
        
        with STATE_LOCK:
            # Add to history
            ROLLOUT_HISTORY[rollout_id] = {
                "container_id": container_id,
                "files": {"info": "Waiting for first poll..."},
                "status": "Starting",
                "last_updated": time.strftime('%H:%M:%S'),
                "start_time": time.time(),
            }
            # Set as active
            ACTIVE_ROLLOUT["rollout_id"] = rollout_id
            ACTIVE_ROLLOUT["container_id"] = container_id

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "monitor started"}')

    def _handle_api_end(self):
        """Clears the active rollout flag."""
        # ... (The rest of this function is correct) ...
        with STATE_LOCK:
            ACTIVE_ROLLOUT["rollout_id"] = None
            ACTIVE_ROLLOUT["container_id"] = None

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "monitor ended"}')


    def _serve_web_page(self):
        # We don't need query parsing or refresh headers for the JSON API,
        # but we'll keep the existing HTML logic for now as a simple fallback
        # and to keep your current browser tab working.
        
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.send_header("Refresh", f"{REFRESH_INTERVAL}") # Keep refresh for simple UI
        self.end_headers()

        # ... (keep existing query parsing and HTML generation logic) ...
        # NOTE: Make sure you reverted the changes from the previous answer
        # back to the simpler HTML generation if you intend to use the
        # external UI tool exclusively, as the query parsing and toggling
        # will now be done by the external tool. 
        # For simplicity, revert back to the original simple HTML logic on `/`.
        # You will be removing the whole `generate_history_html` function later.
        
        # For a clean transition, let's just make sure _serve_web_page only
        # uses the simplest possible history generation if you keep it.
        # But if you are using an external tool, this whole method and 
        # generate_history_html can eventually be deleted.
        
        # For now, just ensure the new _serve_json_history is added, and it will work!
        
        # (Assuming you keep the latest HTML/Query fix for backward compatibility)
        query_components = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        requested_run_id = query_components.get('run_id', [None])[0] 

        with STATE_LOCK:
            display_id = requested_run_id
            if display_id not in ROLLOUT_HISTORY:
                display_id = ACTIVE_ROLLOUT.get("rollout_id")
            if display_id not in ROLLOUT_HISTORY:
                display_id = next(iter(ROLLOUT_HISTORY.keys()), None)

            html = generate_history_html(ROLLOUT_HISTORY, ACTIVE_ROLLOUT, display_id=display_id)
        
        self.wfile.write(html.encode("utf-8"))


# --- HTML Generation (Must be modified to display history) ---
def generate_history_html(history: Dict[str, Any], active_rollout: Dict[str, str], display_id: Optional[str] = None) -> str:
    """Generates the HTML page with a list of all rollouts and the active one's files."""
    active_id = active_rollout.get("rollout_id")
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>VF-Eval Persistent Monitor</title>
        <style>...</style> </head>
    <body>
        <h1>VF-Eval Persistent Monitor ({time.strftime('%H:%M:%S')})</h1>

        <h2>Rollout History (Select a Run)</h2>
        <ul style="list-style: none; padding: 0;">
    """
    
    # List all rollouts
    for r_id, run_data in history.items():
        is_active = "background-color: #e0ffe0;" if r_id == active_id else ""
        
        # Link to the same page with the run ID as a hash fragment for easy navigation
        html_content += f"""
        <li style="border: 1px solid #ccc; padding: 10px; margin-bottom: 5px; {is_active}">
            <a href="/?run_id={r_id}" style="text-decoration: none; color: black;">
                <strong>{r_id}</strong> - Status: {run_data['status']} 
                (Container: {run_data['container_id'][:12]})
            </a>
        </li>"""
    html_content += "</ul><hr>"

    # Display file contents for the active/most recent rollout
    if display_id and display_id in history:
        run_data = history[display_id]
        html_content += f'<h2 id="run-{display_id}">Viewing Run: {display_id} (Status: {run_data["status"]})</h2>'
        html_content += f'<p>Last Container Update: {run_data["last_updated"]}</p>'

        for filename, content in run_data['files'].items():
            if filename in ["error", "info"]: continue
            import html
            safe_content = html.escape(content)
            html_content += f"""
            <div class="file-entry">
                <h2>📁 {filename}</h2>
                <pre>{safe_content}</pre>
            </div>
            """
    else:
        html_content += "<h2>Waiting for the first evaluation run...</h2>"
            
    html_content += "</body></html>"
    return html_content

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Allows the server to handle requests without blocking the main thread."""
    pass

# --- Main Server Start ---
def main():
    # 1. Start the background Docker Polling Thread
    monitor_thread = threading.Thread(target=start_monitor_loop, daemon=True)
    monitor_thread.start()
    
    # 2. Start the HTTP Server (Blocking)
    server_address = ('', MONITOR_PORT)
    
    # 🎯 CORRECT FIX: Instantiating the class directly
    httpd = ThreadedHTTPServer(server_address, MonitorHandler)
    
    print(f"✨ Persistent Monitor Server running at http://127.0.0.1:{MONITOR_PORT}/")
    webbrowser.open_new_tab(f"http://127.0.0.1:{MONITOR_PORT}/")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nMonitor server shutting down.")
        httpd.shutdown()
        sys.exit(0)

if __name__ == "__main__":
    main()

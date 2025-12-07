import threading
import io
import docker
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver
import time
from typing import Dict, Any

# --- Synchronous File Reading Utility (from previous response, refined) ---


# --- Synchronous Wrapper to Run Async Tool ---
# You need a synchronous version of execute_in_container for the MonitorHandler.

def sync_exec_in_container(container, command: str) -> Tuple[int, Tuple[bytes, bytes]]:
    """Synchronously executes a command using an asyncio event loop."""
    try:
        # Get or create a new event loop for this thread (essential for sync context)
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    # Run the async execution function blocking the thread
    return loop.run_until_complete(execute_in_container(container, command, timeout=5))

# --- New Monitoring Core ---

def get_workspace_state_via_bash(container) -> Dict[str, str]:
    """
    Reads workspace files by synchronously running bash commands (ls/cat), 
    which is much faster than container.get_archive().
    """
    # 1. Get a list of all files in the workspace directory.
    # We use sync_exec_in_container which wraps your async execute_in_container
    exit_code, (stdout, stderr) = sync_exec_in_container(
        container, 
        'find /workspace -type f -print' # List all files recursively
    )

    if exit_code != 0:
        return {"error": f"Failed to list files: {stderr.decode()}"}

    file_list = stdout.decode().strip().split('\n')
    file_map = {}

    # 2. CAT each file to get its content (fast operation)
    for file_path in file_list:
        if not file_path:
            continue
            
        # Cat the file content
        exit_code, (content, _) = sync_exec_in_container(
            container, 
            f'cat {file_path}'
        )
        
        if exit_code == 0:
            file_map[file_path.replace('/workspace/', '')] = content.decode('utf-8', errors='ignore')
        else:
            file_map[file_path.replace('/workspace/', '')] = "[ERROR READING FILE]"

    return file_map

# --- Refined read_workspace_contents ---
def read_workspace_contents(container, path: str = "/workspace") -> Dict[str, str]:
    """Synchronously reads all files and subdirectories."""
    contents = {}
    monitor_path = '/'
    
    try:
        strm, stat = container.get_archive(monitor_path)
    except docker.errors.NotFound:
        return {"error": f"Base path not found in container: {monitor_path}"}
    except Exception as e:
        return {"error": f"Error accessing container files: {e}"}

    # 1. READ THE STREAM: Safely read the entire stream into a bytes object.
    # The 'strm' object is usually a generator/iterator.
    tar_data = b''
    try:
        # Check if 'strm' has a standard 'read' method (like a file stream)
        if hasattr(strm, 'read'):
            tar_data = strm.read()
        else:
            # If it's a generator/iterator, collect all chunks
            for chunk in strm:
                tar_data += chunk
    except Exception as e:
        # This catches errors during the reading/iteration process itself
        return {"error": f"Error reading Docker stream: {e}"}
        
    # 2. Extract contents using the collected bytes
    # Only proceed if we actually collected data
    if not tar_data:
        # This can happen if the path is valid but empty, or if an early error 
        # caused an empty generator to be returned.
        return {"error": "Received empty or invalid data stream from container."}
        
    with io.BytesIO(tar_data) as tar_bytes:
        with tarfile.open(fileobj=tar_bytes, mode='r') as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.startswith('workspace/'):
                    f = tar.extractfile(member)
                    if f:
                        contents[member.name] = f.read().decode('utf-8', errors='ignore')
    return contents

# --- HTML Generator ---

def generate_html_monitor(file_map: Dict[str, str], container_id: str) -> str:
    """Generates an HTML page displaying container files."""

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Container Monitor: {container_id[:12]}</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f9; }}
            ... (rest of CSS) ...
        </style>
    </head>
    <body>
        <h1>Monitoring Container: <span style="color: blue;">{container_id[:12]}</span></h1>
        <p>Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}. Auto-refreshing every 3s.</p>
    """

    if "error" in file_map:
        html_content += f"<p style='color: red;'>Error: {file_map['error']}</p>"
    else:
        for filename, content in file_map.items():
            # Skip empty files or directories
            if not content:
                continue 
            
            # Escape HTML characters for safe display
            import html
            safe_content = html.escape(content)
            
            html_content += f"""
            <div class="file-entry">
                <h2>📁 {filename}</h2>
                <pre>{safe_content}</pre>
            </div>
            """
            
    html_content += "</body></html>"
    return html_content

# --- HTTP Server Class ---

# --- Updated MonitorHandler Class ---

class MonitorHandler(BaseHTTPRequestHandler):
    """Custom handler to serve the container file contents."""
    
    # Static reference to the Docker client and active container ID
    DOCKER_CLIENT = None
    ACTIVE_CONTAINER_ID = None
    REFRESH_INTERVAL = 3  # Set refresh interval to 3 seconds
    IS_READY = False

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.send_header("Refresh", f"{self.REFRESH_INTERVAL}")
        self.end_headers()
        
        container_id = self.ACTIVE_CONTAINER_ID
        
        # We must keep the IS_READY gate to prevent interference during setup
        if not container_id or not self.IS_READY:
            html = "<h1>Container is initializing... Please wait for setup to complete.</h1>"
        else:
            try:
                container = self.DOCKER_CLIENT.containers.get(container_id)
                
                # 💡 FIX: Call the new, fast command-based utility
                file_map = get_workspace_state_via_bash(container)
                
                html = generate_html_monitor(file_map, container_id)
            except docker.errors.NotFound:
                html = "<h1>Container not found. It may have been killed or removed.</h1>"
            except Exception as e:
                # Use str(e) to capture the exact error
                html = f"<h1>Internal Server Error: {e}</h1>" 
        
        self.wfile.write(html.encode("utf-8"))


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Allows the server to handle requests without blocking the main thread."""
    pass

import asyncio
import requests
import uuid
import webbrowser
import threading
import docker
import contextlib
import atexit
import io
import tarfile
from shlex import quote
import time
from typing import List, Dict, Any, Optional, Tuple

import verifiers as vf
from verifiers.types import State
from datasets import Dataset

from src.monitor import MonitorHandler, ThreadedHTTPServer

MONITOR_PORT = 8090
MONITOR_URL = f"http://127.0.0.1:{MONITOR_PORT}"

# --- Docker Utils ---

ORPHAN_CONTAINER_TRACKER = []

async def create_container(client: docker.DockerClient, image: str, **kwargs):
    """Async wrapper to spin up a container and keep it alive."""
    loop = asyncio.get_running_loop()
    
    # Run the blocking docker call in a separate thread
    container = await loop.run_in_executor(
        None, 
        lambda: client.containers.run(
            image,
            command="tail -f /dev/null",
            detach=True,
            remove=True, # Auto-cleanup on kill/stop
            **kwargs
        )
    )
    ORPHAN_CONTAINER_TRACKER.append(container.id)
    return container

async def stop_container(container):
    """Async wrapper to kill and remove a container."""
    loop = asyncio.get_running_loop()
    if container:
        try:
            await loop.run_in_executor(None, container.kill)
        except Exception:
            pass # Ignore errors on cleanup (e.g., container already dead)

async def execute_in_container(container, command: str, timeout: int = 30, workdir: str = "/workspace"):
    """Async wrapper to execute a command inside a specific container."""
    loop = asyncio.get_running_loop()
    
    def _exec():
        # exec_run returns (exit_code, output)
        exit_code, output = container.exec_run(
            f"bash -c {quote(command)}", 
            demux=True,  # Split stdout/stderr
            workdir=workdir,
            user="root"
        )
        return exit_code, output

    # ... (try/except block remains the same) ...
    try:
        exit_code, output = await loop.run_in_executor(None, _exec)
        stdout, stderr = output if output else (None, None)
        
        # 💡 CRITICAL FIX: Ensure streams are bytes objects, defaulting to b''
        stdout = stdout if isinstance(stdout, bytes) else b''
        stderr = stderr if isinstance(stderr, bytes) else b''
        
        return exit_code, (stdout, stderr) # Return the tuple of bytes objects
        
    except Exception as e:
        # On execution failure, return an error exit code and the exception message
        return -1, (b'', str(e).encode())

async def _exec_setup_cmd(container, command: str):
    """Internal helper for running setup commands that must succeed."""
    # This call now returns (exit_code, (stdout_bytes, stderr_bytes))
    exit_code, (stdout, stderr) = await execute_in_container(container, command, workdir="/")
    
    if exit_code != 0:
        error_msg = f"Setup command failed: {command}\nSTDERR: {stderr.decode()}\nSTDOUT: {stdout.decode()}"
        raise RuntimeError(error_msg)
    
    return stdout.decode()

def cleanup_orphans():
    """Synchronously kills and removes all containers in the tracker list."""
    print("\n--- Starting Global Orphan Container Cleanup ---")
    client = docker.from_env()
    
    # We must use a synchronous loop here since atexit runs sync code
    for container_id in ORPHAN_CONTAINER_TRACKER:
        try:
            # Get the container object by ID (it might not be in self.client.containers.list() if stopped)
            container = client.containers.get(container_id)
            
            # Kill and remove the container
            container.kill()
            print(f"Killed and removed container: {container_id}")
            
        except docker.errors.NotFound:
            # Container might have been cleaned up by teardown_state already
            pass 
        except Exception as e:
            print(f"Error cleaning up container {container_id}: {e}")

atexit.register(cleanup_orphans)


# --- Docker Env ---

class DockerEnv(vf.StatefulToolEnv):
    def __init__(
        self,
        dataset: vf.Dataset,
        rubric: vf.Rubric,
        image: str = "python:3.11-slim",
        timeout: int = 30,
        network_disabled: bool = False, # Safer default for code execution
        system_prompt: Optional[str] = None,
        **kwargs
    ):
        super().__init__(dataset=dataset, rubric=rubric, system_prompt=system_prompt, **kwargs)
        self.client = docker.from_env()
        self.image = image
        self.timeout = timeout
        self.network_disabled = network_disabled
        self.add_tool(self.bash, args_to_skip=[])

    def _start_monitor_server(self):
        """Starts the monitoring web server in a background thread and opens the browser."""
        
        server_address = ('', self._monitor_port)
        self._monitor_server = ThreadedHTTPServer(server_address, MonitorHandler)
        
        self._monitor_thread = threading.Thread(
            target=self._monitor_server.serve_forever,
            daemon=True
        )
        self._monitor_thread.start()
        
        monitor_url = f"http://127.0.0.1:{self._monitor_port}/"
        print(f"\n✨ Live Monitor running at {monitor_url}")
        
        # 💡 AUTOMATE BROWSER OPENING
        # Use open_new_tab to launch the URL in a new browser tab/window
        try:
            webbrowser.open_new_tab(monitor_url)
        except Exception as e:
            print(f"Warning: Could not automatically open browser. Please navigate to {monitor_url} manually. Error: {e}")



    #################
    # Env Utilities #
    #################

    def _copy_to_container(self, container, dest_path: str, content: str):
        """Synchronous helper to write string content to a file in the container."""
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode='w') as tar:
            data = content.encode('utf-8')
            tar_info = tarfile.TarInfo(name=dest_path.lstrip('/')) # Remove leading / for TarInfo name
            tar_info.size = len(data)
            tar_info.mtime = time.time()
            tar.addfile(tar_info, io.BytesIO(data))
        
        tar_stream.seek(0)
        # Put the archive at the root (/) which contains the file
        container.put_archive(path="/", data=tar_stream)


    async def setup_state(
        self, 
        state: State, 
    ) -> State:
        """Initializes container, installs tools, sets up directories, and copies tests."""
        rollout_id = str(uuid.uuid4())
        
        # 1. Create the container
        container = await create_container(
            self.client, 
            self.image,
            network_mode="none" if self.network_disabled else "bridge",
            mem_limit="512m"
        )

        #MonitorHandler.ACTIVE_CONTAINER_ID = container.id
        
        state["container"] = container

        try:
            requests.post(
                f"{MONITOR_URL}/api/start", 
                json={"rollout_id": rollout_id, "container_id": container.id},
                timeout=2
            )
        except requests.exceptions.ConnectionError:
            print(f"WARNING: Monitor server not running at {MONITOR_URL}. Skipping monitoring.")
        
        # 4. Store ID in state for teardown
        state["rollout_id"] = rollout_id
        # 4. Standard Container Setup
        await _exec_setup_cmd(container, "pip install uv pytest")
        await _exec_setup_cmd(container, "mkdir -p /workspace/code /workspace/tests")
        

        return state

    async def teardown_state(self, state: State):
        # 1. 💡 API CALL: Notify the monitor server that the rollout has ended
        rollout_id = state.get("rollout_id")
        if rollout_id:
            try:
                requests.post(f"{MONITOR_URL}/api/end", timeout=2)
            except requests.exceptions.ConnectionError:
                pass # Ignore if server is already down

        # 2. Cleanup (as before)
        container = state.get("container")
        if container:
            await stop_container(container)
            if container.id in ORPHAN_CONTAINER_TRACKER:
                 ORPHAN_CONTAINER_TRACKER.remove(container.id)

    def update_tool_args(
        self, 
        tool_name: str, 
        tool_args: Dict[str, Any],
        messages: List[Dict[str, str]],
        state: State,
        **kwargs
    ) -> Dict[str, Any]:
        if tool_name == "bash":
            updated_args = dict(tool_args)
            updated_args["state"] = state
            return updated_args
        else:
            return tool_args
        
    ##########
    # Tools ##
    ##########

    async def bash(self, command: str, state: str) -> str:
        """
        Execute a bash command inside the rollouts assigned container.
        
        Args:
            command: The bash command to execute.
        """
        container = state.get("container")
        if not container:
            return "Error: No container found for this session."

        exit_code, (stdout, stderr) = await execute_in_container(
            container, 
            command, 
            self.timeout
        )
        
        # Format the output for the LLM
        out_str = stdout.decode('utf-8', errors='ignore') if stdout else ""
        err_str = stderr.decode('utf-8', errors='ignore') if stderr else ""
        
        if exit_code != 0:
            return f"Exit Code: {exit_code}\nSTDERR:\n{err_str}\nSTDOUT:\n{out_str}"
            
        return out_str

################
# Entrypoint   #
################

async def test_execution_reward(completion: str, state: State, **kwargs) -> float:
    """
    A reward function that runs the tests written by the agent in the container.
    It awards 1.0 for success (Exit Code 0).
    """
    container = state.get("container")
    if not container:
        return 0.0 # Cannot run tests

    # 💡 CRITICAL: Command to run the agent's self-written tests
    exit_code, (stdout, stderr) = await execute_in_container(
        container, 
        "uv run pytest /workspace/tests/test_solution.py",
        timeout=10 # Short timeout for test execution
    )

    # For training/evaluation, we want explicit pass/fail
    if exit_code == 0:
        return 1.0 # Tests passed (meaning agent completed both code and tests)
    else:
        # Optionally, you can log the output for debugging:
        # print(f"Tests failed. STDOUT: {stdout.decode()}, STDERR: {stderr.decode()}")
        return 0.0 # Tests failed or execution error


SYSTEM_PROMPT = """
... (initial setup remains the same) ...

### 📂 Required File Structure and Paths
You must create and modify these two specific files.

| Path | Purpose | Constraint |
| :--- | :--- | :--- |
| **`/workspace/code/solution.py`** | **This is where all your solution logic MUST reside.** | **Write all final code here.** |
| **`/workspace/tests/test_solution.py`** | **This is where your unit tests MUST reside.** These tests define your success and will be executed for the final reward. | **Write all tests here.** |

... (Tool definition remains the same) ...

Example Strategy and Usage (CRITICAL):

1.  **Write Code:** Implement your solution:

    ```bash
    echo 'def add(a, b):\n    return a + b' > /workspace/code/solution.py
    ```

2.  **Write Tests:** Create a test file that imports and tests your solution:

    ```bash
    echo 'import sys' > /workspace/tests/test_solution.py
    echo 'sys.path.append("/workspace/code")' >> /workspace/tests/test_solution.py
    echo 'from solution import add' >> /workspace/tests/test_solution.py
    echo 'def test_add_works():' >> /workspace/tests/test_solution.py
    echo '    assert add(2, 3) == 5' >> /workspace/tests/test_solution.py
    ```
    *Note: The sys.path.append is crucial for the tests to find your code.*

3.  **Verify:** Run the tests to confirm your implementation is correct and your tests are passing:

    ```bash
    uv run pytest /workspace/tests/test_solution.py
    ```

Make sure to write both the code and the tests.
Continue making tool calls until everythinh is done.

"""

def load_environment(
    image: str = "python:3.11-slim",
    **kwargs
) -> vf.Environment:
    """
    Verifiers entrypoint for the DockerEnv.
    """
    
    # 1. Define the Rubric using the test execution function
    rubric = vf.Rubric(
        funcs=[test_execution_reward], 
        weights=[1.0]
    )

    # 2. Define a simple dataset (The task is implied by the test file)
    dataset = Dataset.from_dict({
        "prompt": [
            # The prompt guides the LLM on what code to write
            [{"role": "user", "content": "Write a Python function `add(a, b)` and save it to `/workspace/code/main.py` that correctly returns the sum of two numbers. You can only use the `bash` tool."}],
            [{"role": "user", "content": "Write a Python FastAPI server that servers a hello world endpoint` and save it to `/workspace/code/main.py` that correctly returns the sum of two numbers. You can only use the `bash` tool."}],
        ],
        "answer": [
            "The correct implementation of add(a, b) is trivial math.",
            "The correct implementation has a server with a hello_world endpoint"
        ]
    })

    # 3. Instantiate the DockerEnv
    env = DockerEnv(
        dataset=dataset,
        rubric=rubric,
        image=image,
        system_prompt=SYSTEM_PROMPT,
        **kwargs
    )
    
    return env

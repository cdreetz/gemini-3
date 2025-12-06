import asyncio
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
    ):
        super().__init__(dataset=dataset, rubric=rubric)
        self.client = docker.from_env()
        self.image = image
        self.timeout = timeout
        self.network_disabled = network_disabled

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
        
        # 1. Create the container
        container = await create_container(
            self.client, 
            self.image,
            network_mode="none" if self.network_disabled else "bridge",
            mem_limit="512m"
        )
        
        state["container"] = container

        # 4. Standard Container Setup
        await _exec_setup_cmd(container, "pip install uv pytest")
        await _exec_setup_cmd(container, "mkdir -p /workspace/code /workspace/tests")
        
        # 5. Copy a challenge test file (e.g., test for a simple addition function)
        TEST_CONTENT = """
import pytest
import sys
# Add code directory to path so the agent's code can be imported
sys.path.append('/workspace/code') 
try:
    from main import add
except ImportError:
    # Define a dummy function to prevent crash if agent hasn't written code yet
    def add(a, b): return a + b # Will fail test cases if this is the actual code

def test_add_function():
    # This test will fail if the agent does not define a correct 'add' function 
    # in /workspace/code/main.py
    assert add(2, 3) == 5
    assert add(10, -5) == 5
"""
        # Note: self._copy_to_container is synchronous, so we run it directly
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, 
            lambda: self._copy_to_container(
                container, 
                "/workspace/tests/test_main.py", 
                TEST_CONTENT
            )
        )

        return state

    async def teardown_state(self, state: State):
        """Cleanup handled by AsyncExitStack, this is just a placeholder."""
        container = state.get("container")
        if container:
            await stop_container(container)

            if container.id in ORPHAN_CONTAINER_TRACKER:
                ORPHAN_CONTAINER_TRACKER.remove(container.id)

    def update_tool_args(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "bash":
            pass

        return args
        
    ##########
    # Tools ##
    ##########

    async def bash(self, command: str, state: State) -> str:
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
    A reward function that runs the tests in the container and awards 1.0 for success.
    
    This function is called by the Rubric at the end of the rollout.
    It expects the LLM agent to have used the 'bash' tool to write its code 
    to /workspace/code/main.py.
    """
    container = state.get("container")
    if not container:
        return 0.0 # Cannot run tests

    # Command to run tests: uv run pytest /workspace/tests/test_main.py
    exit_code, (stdout, stderr) = await execute_in_container(
        container, 
        "uv run pytest /workspace/tests/test_main.py",
        timeout=10 # Short timeout for test execution
    )

    # For training/evaluation, we want explicit pass/fail
    if exit_code == 0:
        return 1.0 # Tests passed
    else:
        # Optionally, you can log the output for debugging:
        # print(f"Tests failed. STDOUT: {stdout.decode()}, STDERR: {stderr.decode()}")
        return 0.0 # Tests failed or execution error

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
        ],
        "answer": ["The correct implementation of add(a, b) is trivial math."]
    })

    # 3. Instantiate the DockerEnv
    env = DockerEnv(
        dataset=dataset,
        rubric=rubric,
        image=image,
        **kwargs
    )
    
    return env

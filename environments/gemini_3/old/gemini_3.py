import asyncio
import docker
import verifiers as vf
from verifiers.types import State, ChatMessage
from datasets import Dataset
from typing import List, Dict, Any, Optional

################
# Docker Uitls #
################

async def create_container(client: docker.DockerClient, image: str, **kwargs):
    loop = asyncio.get_running_loop()
    container = await loop.run_in_executor(
        None, 
        lambda: client.containers.run(
            image,
            command="tail -f /dev/null",
            detach=True,
            remove=True,
            **kwargs
        )
    )
    return container

async def stop_container(container):
    loop = asyncio.get_running_loop()
    if container:
        try:
            await loop.run_in_executor(None, container.kill) 
        except Exception:
            pass


async def execute_in_container(container, command: str, timeout: int = 30):
    loop = asyncio.get_running_loop()
    
    def _exec():
        exit_code, output = container.exec_run(
            f"bash -c {quote(command)}", 
            demux=True
        )
        return exit_code, output

    from shlex import quote
    try:
        return await loop.run_in_executor(None, _exec)
    except Exception as e:
        return -1, (b'', str(e).encode())


################
# Docker Env ##
################

class DockerEnv(vf.StatefulToolEnv):
    def __init__(
        self,
        image: str = "python:3.11-slim",
        timeout: int = 30,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.client = docker.from_env()
        self.image = image
        self.timeout = timeout

    #################
    # Env Utilities #
    #################

    async def setup_state(
        self, 
        state: State,
        stack: contextlib.AsyncExitStack
    ):
        container = await create_container(
            self.client,
            self.image,
            mem_limit="512m"
        )
        stack.push(contextlib.closing(container))
        stack.push(lambda *args: stop_container(container))

        state["container"] = container
        return state

    async def is_completed(self, state: State, **kwargs):
        completed = await self.is_completed(**kwargs)

        pass


    ##########
    # Tools ##
    ##########
    async def bash(
        self, 
        command: str,
        state: State
    ):
        """Execute command inside rollouts assigned container"""
        container = state.get("container")

        exit_code, (stdout, stderr) = await execute_in_container(
            container,
            command,
            self.timeout
        )
        out_str = stdout.decode('utf-8', errors='ignore') if stdout else ""
        err_str = stderr.decode('utf-8', errors='ignore') if stderr else ""

        if exit_code != 0:
            return f"Exit code: {exit_code}\nSTDERR: \n{err_str}\nSTDOUT: \n{out_str}"

        return out_str







def load_environment(**kwargs) -> vf.Environment:
    dataset = Dataset.from_dict({
        "question": ["write a python file :write"],
        "answer": []
    })

    rubric = Rubric()

    env = DockerEnv(
        dataset=dataset,
        rubric=rubric
    )

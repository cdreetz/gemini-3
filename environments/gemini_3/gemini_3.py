import verifiers as vf
from verifiers.types import State

################
# Docker Uitls #
################

async def create_container():
    pass

def container_context_manager():
    pass

def execute_in_container(container, command):
    pass



################
# Docker Env ##
################

class DockerEnv(vf.StatefulToolEnv):
    def __init__(
        self,
        image: str = "python:3.11-slim",
        timeout: int = 30,
    ):
        self.client = docker.from_env()
        self.image = image
        self.timeout = timeout
        self.containers = []


    #################
    # Env Utilities #
    #################

    async def setup_state(self, state: State):
        container = await create_container()
        state["container"] = container
        self.containers.append(container)
        return state

    async def is_completed(self, state: State, **kwargs):
        completed = await self.is_completed(**kwargs)

        pass


    ##########
    # Tools ##
    ##########
    async def bash(self, command: str):
        """Execute command inside rollouts assigned container"""
        pass






def load_environment(**kwargs) -> vf.Environment:
    dataset = Dataset.from_dict({
        "question": [],
        "answer": []
    })

    rubric = Rubric()

    env = DockerEnv(
        dataset=dataset,
        rubric=rubric
    )

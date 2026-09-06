import os
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from tools import ALL_TOOLS


SOURCE_DIR = Path(__file__).resolve().parent


def load_instructions() -> str:
    return (SOURCE_DIR / "contract" / "zava_system_prompt.md").read_text(encoding="utf-8")


def create_agent() -> Agent:
    load_dotenv(override=False)
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )
    return Agent(
        client=client,
        name="zava-traces-demo",
        description="日本語の購入後サポートを再現し、tool-use traceを生成するデモ用agent。",
        instructions=load_instructions(),
        tools=ALL_TOOLS,
        default_options={"store": False},
    )


def main() -> None:
    ResponsesHostServer(create_agent()).run()


if __name__ == "__main__":
    main()

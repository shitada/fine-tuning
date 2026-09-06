# Coding Agent Instructions

This project is the `zava-traces-demo` Microsoft Foundry Hosted Agent. It uses direct code deployment, the Responses protocol, and six in-process Python Function Tools backed only by deterministic synthetic data.

Before working on or answering questions about Foundry agents, read the `microsoft-foundry` skill first.

## Key files

- `azure.yaml`: Existing Foundry project binding and direct code deployment.
- `src/zava-traces-demo/main.py`: Agent and Responses hosting server.
- `src/zava-traces-demo/tools.py`: Function Tool registration.
- `src/zava-traces-demo/synthetic_store.py`: Side-effect-free synthetic order logic.
- `src/zava-traces-demo/contract/`: Approved Japanese prompt and tool schema.

Do not add external APIs, MCP servers, real customer data, secrets, or persistent mutations. Keep the packaged contract byte-identical to `Demos/TracesDistillation/fixtures/`.

For local azd commands, honor an explicit process-level `AZD_CONFIG_DIR`. Otherwise, when `.azd-client.json` exists, use its `config_dir` as `AZD_CONFIG_DIR` for that command process. Keep direct CLI operations and the traffic runner on the same profile. An invalid configured profile is an error, not a reason to fall back to another profile. Never store credentials in this local configuration file.
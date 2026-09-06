# Zava Traces Demo Hosted Agent

Traces Distillation用の日本語購入後サポートagentです。Microsoft Agent FrameworkのResponses host上で、6つのPython Function Toolを同一プロセス内実行します。外部API、MCP、実顧客データ、永続的な更新は使用しません。

```text
利用者 -> Foundry Responses endpoint -> Agent Framework
                                      -> gpt-5.4-pro
                                      -> local Function Tools
                                      -> deterministic synthetic store
```

`contract/`はStep 4で承認したsystem promptとtool schemaの正本コピーです。Local testではrepoの`fixtures/`とbyte単位で一致することを確認します。

## Local validation

```powershell
..\.venv\Scripts\python.exe -m unittest discover src\zava-traces-demo\tests -v
```

## Deployment

既存projectへdirect code deploymentします。新しいprojectやACRは作成しません。

```powershell
$env:AZURE_DEV_USER_AGENT = "microsoft_foundry_skill"; azd deploy --no-prompt
```

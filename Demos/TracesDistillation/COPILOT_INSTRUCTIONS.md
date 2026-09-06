# GitHub Copilot で実行する Traces Distillation

このガイドは、`Demos\TracesDistillation` のデモを GitHub Copilot 内で段階的に実行するための手順です。ローカルコマンドはCopilotがPowerShellで実行し、Azureリソースの参照・更新は原則としてAzure MCPを使用します。Hosted agentの作成・デプロイ・呼び出しだけは、公式の`azd ai agent`経路を使用します。

別PCで既存の検証を再開する場合は、先に [MACHINE_HANDOFF.md](MACHINE_HANDOFF.md) に従います。初回構築用のStep 0から順に再実行せず、Git管理外の`EXECUTION_LOG.md`と`run\`から再開位置と未承認の操作を確認してください。

## 実行ルール

1. Copilot は一度に 1 ステップだけ実行し、結果と完了条件を示して停止する。
2. ローカル操作は Copilot の PowerShell から実行する。ユーザーに手動実行を依頼しない。
3. Azure の参照・更新には Azure MCP を使う。ただし、Step 5とStep 6でhosted agentを作成・デプロイ・呼び出す操作に限り、Foundry hosted agentの公式経路である`azd ai agent`と`azd deploy`を使用する。`az`、Azure PowerShell、Portal の手動操作へフォールバックしない。
4. API キーやアクセストークンを取得、表示、保存しない。SDK 認証には `DefaultAzureCredential` を使う。
5. Fine-tuning ジョブの送信、モデルのデプロイ、トラフィック生成など、課金または Azure を変更する操作の直前でユーザーの承認を得る。
6. 既存トレースの候補と生成データは、個人情報をマスクした要約だけをチャットに表示する。
7. 認証・権限・ネットワークの問題が発生した場合は停止し、許可された`azd`操作以外のCLIで回避しない。
8. Demoの人間向け・業務上のデータは日本語で統一する。Protocolやprogram codeが要求する識別子だけを英語またはASCIIで保持する。

## 現在の notebook に対する重要な注意

`notebook.ipynb` は実装の参照元として使用しますが、次のセルはそのまま実行しません。

| 対象 | 理由 | Copilot での置換 |
|---|---|---|
| Setup セル | `AZURE_OPENAI_API_KEY` を要求する | `AIProjectClient(..., DefaultAzureCredential())` と `project_client.get_openai_client()` を使用 |
| Data Generation helper | 古い SDK 呼び出しを含む | `azure-ai-projects>=2.5.0` の `project_client.beta.datasets.begin_create_generation_job(job=...)` を使用 |
| Deploy セル | `az account get-access-token` を実行する | Foundry MCP を優先し、必要なら Azure ARM MCP で直接デプロイ |
| JSONL出力 | `json.dumps()`の既定値では日本語がUnicode escapeになり、upload fileにBOMが付かない | 可読データは`ensure_ascii=False`で書き、train/validationはUTF-8 BOM付きで生成 |

元の notebook を一括実行せず、Copilot が各処理をステップ単位で実行します。

## Copilot に渡す開始指示

次のメッセージで開始します。

```text
Demos\TracesDistillation\COPILOT_INSTRUCTIONS.md に従って Step 0 から開始してください。
一度に 1 ステップだけ実行し、完了条件を確認して停止してください。
ローカルコマンドは GitHub Copilot 内で実行し、Azure 操作は原則として Azure MCP を使用してください。
hosted agentの作成・デプロイ・呼び出しに限り、手順書で指定されたazdコマンドを使用してください。
az CLI、Azure PowerShell、Portal の手動操作、API キーの取得は使用しないでください。
Demoのprompt、description、商品、状態、理由、tool result、assistant responseは日本語で統一してください。
```

## 実行時に保持する値

Copilot は次の値を実行中の状態として保持します。秘密情報ではありませんが、ユーザーの承認なしにリポジトリへ保存しません。

| 値 | 取得元 |
|---|---|
| Subscription | Azure MCP subscription |
| Resource group | Azure MCP group/ARM |
| Foundry project endpoint | Azure MCP Foundry/ARM |
| Foundry account | Azure MCP Foundry/ARM |
| Hosted agent name/version | Azure MCP Foundry |
| Hosted agent source path | Step 5で作成したローカルsource |
| Application Insights resource ID | Azure MCP Foundry/Monitor/ARM |
| Student base deployment | Azure MCP Foundry |
| Trace start/end time | ユーザー選択。既定は直近 7 日 |
| Traffic seed/count/category | Step 6のdry-runと実行結果 |
| Fine-tuned deployment name | ユーザー承認後に決定 |
| Japanese data contract hash | Step 4で承認したsystem prompt、tools、語彙のhash |

## このガイドで実施すること

このガイドでは、Microsoft Foundry にデプロイ済みの hosted agent が実際に処理した会話トレースを教師データとして使い、より小さく低コストな student model に agent の tool-use behavior を学習させます。人手で正解ラベルを新規作成するのではなく、既存の実行履歴から supervised fine-tuning（SFT）用データを生成する **Traces Distillation** の一連の流れを実行します。

元の `Demos\TracesDistillation\notebook.ipynb` が持つ処理を基礎にしつつ、GitHub Copilot が安全な認証、Azure 構成の確認、承認ゲート、データ検証、実行結果の記録を補います。Notebook は一括実行せず、Step 0～14を次の4段階で進めます。

| 段階 | 対応 Step | 概要 |
|---|---:|---|
| 実行準備 | 0～5 | ローカル環境と Azure の対象を特定し、trace生成専用hosted agentを作成して、teacher model、student deployment、Application Insights、権限、quota、system prompt、tools を確定する |
| 学習データ作成 | 6～8 | 専用agentへtrafficを生成し、agent tracesからSFTデータを作成して、重複・不正なrole sequence・tool call形式・個人情報を検証する |
| Baselineと学習 | 9～12 | データをtrain/validation/testに分割し、base studentを評価した後、承認済み条件でfine-tuningを実行して新しいmodelをデプロイする |
| 比較評価と確定 | 13～14 | 同じheld-out test setでbase、fine-tuned、可能ならteacherを比較し、改善効果、コスト、制約、次の判断を記録する |

### 処理の全体像

```text
Hosted agent
  └─ Application Insights の traces
       └─ Data Generation
            └─ raw trace JSONL
                 └─ 5-step transform と検証
                      └─ train / validation / test
                           ├─ base student の baseline
                           └─ fine-tuning
                                └─ fine-tuned deployment
                                     └─ base / fine-tuned / teacher の比較
```

学習データ変換では、元デモと同じく次の問題を補正します。

1. Overlapping snapshots の重複
2. Tool callを含まないfragment row
3. Assistant tool-call rowの文字列 `"null"` content
4. 連続するassistant tool calls
5. 欠落しているsystem promptとtools

### 日本語化の境界

このデモでは、ユーザーやレビュー担当者が読む意味データを日本語化します。

| 日本語にする | 英語またはASCIIのまま保持する |
|---|---|
| System prompt、tool/parameter description | JSON key、JSON Schema keyword |
| User prompt、assistant response | `role`、`type`などAPI規定値 |
| 商品名、category、loyalty tier | Function name、property name |
| 配送status、reason、action | Order/item/SKU/case ID |
| Tool resultの説明、error message、resolution summary | Model/deployment名、ISO 8601日時、`JPY`などの標準code |

Function nameはAzure OpenAIの制約により英数字、underscore、dashで保持します。日本語のenum/valueを使う場合は、system prompt、tool schema、agent実装、traffic generator、training/evaluation dataで完全に一致させます。

日本向けsynthetic dataでは、実在する個人・注文・住所・決済情報を使いません。金額は円、currency codeは`JPY`とし、元デモの判断構造を保ったままshipping creditなどの金額を日本向けに置き換えます。

## このガイドから得られるもの

完了時には、単にfine-tuned modelを作るだけでなく、元のstudent modelに対して改善したかを再現可能な形で判断できます。

| 成果 | 内容 |
|---|---|
| 確定した実行構成 | Subscription、Foundry project、agent name/version、teacher model、student deployment、Application Insights、trace期間 |
| 検証済みSFTデータ | 生トレース、変換済みデータ、除外理由、schema・tool・個人情報の検証結果 |
| 再現可能なデータ分割 | Conversationや重複contentがsplitをまたがないtrain/validation/test |
| Baseline | Fine-tuning前のstudent modelのtool match score、pass rate、error、代表的な失敗 |
| Fine-tuned model | 成功したfine-tuning job、model ID、Azure deployment |
| 比較評価 | Base / Fine-tuned / Teacherの品質、lift、teacher gap closure、latency、token usage |
| 意思決定材料 | 成功条件を満たしたか、追加データやhyperparameter調整が必要か、不要なAzureリソースをどう扱うか |

主なローカル成果物は `Demos\TracesDistillation\run` に保存します。

```text
traces-raw.jsonl
traces-clean.jsonl
traffic-scenarios.jsonl
traffic-summary.json
train.jsonl
val.jsonl
test.jsonl
eval_data.jsonl
baseline_eval_results.json
ft_eval_results.json
```

元デモでは、Zava retail agentの約100会話を`gpt-4.1-nano`へdistillationし、tool-call評価が7.38から8.60、pass rateが60%から100%へ改善しました。これは英語データと元デモ固有のteacher/traceによる実績であり、日本語化したこのガイドの実行結果を保証する値ではありません。絶対値は比較せず、同じ日本語held-out test setに対するbaseとfine-tunedの改善率を測定します。

## Step 0: リポジトリと実行環境を確認する

Copilot が次を確認します。

- 作業ディレクトリがリポジトリルートである。
- `Demos\TracesDistillation\notebook.ipynb` と `fixtures` が存在する。
- `git status --short` を確認し、既存変更を上書きしない。
- Python 3.11 以上が利用できる。
- `Demos\TracesDistillation\run` は生成物専用で、Git 管理対象外である。

**完了条件:** 対象ファイル、Python、作業ツリーの状態が報告される。変更は行わない。

## Step 1: Azure の対象を直接取得する

Copilot は Azure MCP でサブスクリプション一覧を取得します。既定サブスクリプションがない、または候補が複数ある場合は、ユーザーに 1 つ選択してもらいます。

選択後、Azure MCP で次を取得します。

1. Resource group 一覧
2. Foundry account/project 候補
3. Application Insights 候補
4. 対象 Resource group 内の関連リソース

リソース名で推測せず、リソース ID と関連付けを確認します。

**完了条件:** Subscription、Resource group、Foundry project 候補が 1 つに確定する。

## Step 2: Foundry 構成と前提条件を確認する

Copilot は Foundry MCP の機能を discovery してから、対象プロジェクトを直接照会します。

確認項目:

- Project endpoint
- Step 5で作成するhosted agentが使用するteacher model deployment
- Student 候補の base deployment
- Project に接続された Application Insights
- Fine-tuning と Data Generation を利用できるリージョン
- Fine-tuning と deployment に必要な quota/capacity
- 実行ユーザーが Foundry User 以上であること
- Project managed identity が Application Insights に `Log Analytics Reader` を持つこと
- 保護テーブルを使う場合は `Privileged Monitoring Data Reader` を持つこと
- Application Insights のクエリアクセスが Data Generation の制約を満たすこと

Foundry MCP の認証に失敗した場合は停止し、IDE/Copilot 側の Azure サインインを完了してから再実行します。CLI 認証へ切り替えません。

このデモではsource agentと学習に使えるtraceがまだ存在しないことを前提とします。既存agentのtrace件数は棚卸しせず、source agentのname/versionはStep 5で確定します。

**完了条件:** Project、Application Insights、teacher deployment、student deploymentが確定し、専用hosted agentの作成、trace生成、Data Generationに必要な権限・リージョン・ネットワーク・quota/capacityにblockerがない。

## Step 3: ローカル Python 環境を準備する

Copilot が `Demos\TracesDistillation` で次の処理を実行します。仮想環境を activate せず、常に仮想環境内の Python を明示して使います。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install `
  "openai>=3.0,<4" `
  "azure-ai-projects>=2.5,<3" `
  "azure-identity>=1.25,<2" `
  "azure-ai-evaluation>=1.18,<2"
```

この範囲は2026-08-30時点の現行GA major versionに合わせています。`azure-ai-projects>=2.5`が`openai>=3.0`を要求するため、OpenAI SDKも3.xへ揃えます。`project_client.beta.datasets`を使うData Generation API自体はPreviewであり、SDK packageがGAでもAPI変更の可能性があります。

その後、4パッケージのimport、実際に解決されたversion、Python requirementを確認します。依存関係は実行時に解決し、APIキーを`.env`に作成しません。

**完了条件:** 4パッケージをimportでき、`openai` 3.x、`azure-ai-projects` 2.5以上3未満、`azure-identity` 1.25以上2未満、`azure-ai-evaluation` 1.18以上2未満であり、現在のPython versionが各packageのrequirementを満たす。

## Step 4: 日本語agent contractを作成・承認する

次の英語fixtureを、agent作成からfine-tuning・評価まで共通利用する日本語仕様へ変換します。

- `fixtures\zava_system_prompt.md`: Agentの役割、業務policy、必須tool順序
- `fixtures\zava_tools.json`: 6つのfunctionと引数のJSON Schema

実施内容:

1. Policyとtool/parameter descriptionを日本語化し、金額を円へ合わせる。
2. Function/property名とAPI規定値は維持し、status、reason、action、商品などの意味データを日本語へ統一する。
3. Tool schema、共通語彙、policyに矛盾がなく、UTF-8で直接読めることを検証する。
4. 秘密情報・個人情報・許可していない英語文がないことを確認する。
5. 差分とSHA-256を提示し、ユーザー承認後にagent contractとして確定する。

確定したcontractは、Step 5のagent/tool実装、Step 6のtraffic生成、Step 8のSFTデータへの注入、Step 9・13の評価で同じ内容を使用します。このステップではsynthetic orderの生成、agent deployment、Azure変更は行いません。

**完了条件:** 日本語system prompt、tool schema、共通語彙が一貫し、安全性とencodingの検証を通過してユーザー承認済みである。

## Step 5: Trace生成専用hosted agentを作成する

このデモではsource agentが存在しない前提のため、このステップを実行します。最初にFoundry MCPで`zava-traces-demo`と同名のagentがないことだけを確認します。既存agentのtrace履歴は確認しません。

既存の`Demos\ZavaRetailAgent`は旧Agents API、外部MCP、異なるtool catalogを使うため、そのままデプロイしません。Policyとsynthetic dataの考え方だけを参考にし、Traces Distillation専用の小さなhosted agentを作成します。

### 構成

| 項目 | 既定値 |
|---|---|
| Agent name | `zava-traces-demo` |
| Agent type | Microsoft Agent Frameworkを使うhosted agent |
| Runtime | Python 3.13 |
| Protocol | Responses |
| Teacher deployment | Step 2で確定した強いmodel。現在の既定は`gpt-5.4-pro` |
| Foundry project | Step 2で確定した既存project |
| Deploy method | `codeConfiguration`を使うdirect code deployment |

ローカルsourceは`Demos\TracesDistillation\agent`配下の独立したazd projectとして作成します。

```text
agent\
  azure.yaml
  src\zava-traces-demo\
    main.py
    tools.py
    synthetic_store.py
    requirements.txt
    .agentignore
```

Copilotは`azd ai agent sample list`で現行のPython basic starterを取得し、既存Foundry projectのresource IDを指定して`azd ai agent init`します。新しいFoundry projectは作成せず、`azd provision`も実行しません。標準的なPython agentはdirect code deploymentを使うため、Dockerfileや新しいACRを追加しません。

すべてのazd commandでは、次のようにuser agentをそのcommandだけに設定します。リポジトリやazd environmentへ永続化しません。

```powershell
$env:AZURE_DEV_USER_AGENT = "microsoft_foundry_skill"; azd <command>
```

### Tool実装

6 toolsは外部サービスや実顧客データを使わず、固定seedとorder IDから再現可能な日本語synthetic resultを返します。JSON key、function/property name、IDは英語またはASCIIのままにし、値と説明を日本語化します。

| Tool | Synthetic result |
|---|---|
| `get_order_details` | 日本語の商品、tier、category、金額、sale status、注文日 |
| `get_fulfillment_status` | 日本語の配送status、予定日、配達日、遅延情報 |
| `check_resolution_policy` | Return window、reason、tier、sale statusに基づく日本語のeligibility理由とfee rate |
| `check_inventory` | SKUごとの在庫数、交換可否、日本語の商品候補 |
| `calculate_resolution` | 円建ての返金、restocking fee、shipping credit、差額 |
| `submit_resolution` | 外部更新をせず、synthetic case IDと日本語の`処理シミュレーション完了` statusを返す |

`submit_resolution`を含めて実システムへの副作用は発生させません。Synthetic dataには実在人物の氏名、メール、住所、決済情報を含めません。

### ローカル検証

デプロイ前に次を確認します。

1. 6 toolsのunit testが成功する。
2. 同じorder IDは常に同じorder、fulfillment、policy resultを返す。
3. Traffic scenarioに登場するitemが`get_order_details`のresultに含まれる。
4. Status、reason、action、tier、categoryがStep 4の日本語語彙だけを使う。
5. Agentから生成されるtool schemaが`fixtures\zava_tools.json`と一致する。
6. 返品、交換、遅配、紛失、キャンセル、複数商品の日本語promptで期待するtool sequenceになる。
7. Tool result、assistant response、error messageに文字化けがなく、日本語で理解できる。
8. Agent sourceとdeployment packageにsecretが含まれない。

### デプロイ

ローカル検証後、agent name、teacher deployment、既存project、課金を表示してユーザー承認を得ます。承認後に`azd deploy`を実行し、activeなimmutable agent versionを取得します。Remote smoke testは1件だけ実行します。

デプロイ後、新しいagent name/version/teacherを実行時の保持値へ追加し、Step 4で承認したsystem promptとtoolsの内容またはhashをそのagent versionへ対応付けます。以後はversionを固定します。

**完了条件:** `zava-traces-demo`のversionがactiveで、fixtureと一致する6 toolsを使ったremote smoke testに成功し、agent name/version/teacher deploymentが実行状態へ記録される。

## Step 6: 段階的にtrafficを生成してトレースを確認する

`fixtures\push_prompts.py`はagentのsynthetic storeを参照する日本語traffic generatorです。固定manifestを先に作成し、承認済みの範囲だけ送信します。

- Agentの`synthetic_store.py`と同じ規則からorderとitemを生成する。
- 固定seedを使い、同じ入力から同じscenario setを再現する。
- 返品、交換、配送問題・紛失・遅配、キャンセル、複数商品、曖昧・policy質問に分類する。
- Prompt template、商品名、理由、ユーザー向け文章をすべて自然な日本語にする。
- Categoryごとの生成件数を集計する。
- 各requestを独立したconversationとして送信する。
- `--dry-run`でprompt数、category分布、重複、order/item整合性を確認できるようにする。
- Response ID、成功・失敗、category、所要時間、input/output token数だけを記録し、response本文や個人情報を保存しない。
- JSONL/JSONは`ensure_ascii=False`、UTF-8で保存し、日本語を直接読めるようにする。
- CLIの進捗、error、summaryも日本語で表示する。

生成したscenarioと集計結果は、再現と監査のため次へ保存します。

```text
Demos\TracesDistillation\run\traffic-scenarios.jsonl
Demos\TracesDistillation\run\traffic-summary.json
Demos\TracesDistillation\run\traffic-requests.jsonl
```

まず`.\.venv\Scripts\python.exe fixtures\push_prompts.py --dry-run --num-prompts 170 --seed 42`で通信なしの検証を行います。既存manifest、contract hash、送信履歴が一致しない場合は上書き・再送せず停止します。

推奨する最終構成の目安:

| Category | 件数 |
|---|---:|
| 返品 | 35 |
| 交換 | 30 |
| 配送問題、紛失、遅配 | 30 |
| キャンセル | 20 |
| 複数商品 | 25 |
| 曖昧、policy質問 | 20～30 |

一度に全件を送信せず、次の順で進めます。

### Stage 1: Smoke traffic

5～10件のparameter、teacher model使用料、想定変更を表示し、ユーザー承認後に送信します。90秒以上待ち、Azure Monitor MCPで次を確認します。

- 対象agent name/versionの`requests`が増えている。
- Tool-bearing promptに`dependencies`のtool call/response spanがある。
- Agent input/output messagesが記録されている。
- 重大なfailureやschema mismatchがない。
- User prompt、tool argument/result、assistant responseが日本語で、文字化けがない。
- 英語のprotocol/identifier allowlistを除き、人間向け英語文が混入していない。

条件を満たさない場合は本trafficへ進みません。

### Stage 2: Pilot traffic

25件を送信し、成功率、category分布、平均input/output token、平均tool call数、日本語応答率を確認します。日本語は英語とtoken数が異なる可能性があるため、文字数から推測せず実測tokenを使って残りtrafficの概算費用を計算し、ユーザーへ提示します。

### Stage 3: Full traffic

ユーザーが件数と概算費用を承認した後、合計150～200件を目標に残りを送信します。現在のazd runnerは会話cacheの競合を防ぐためconcurrency=1に固定し、毎回`--new-conversation`と明示的なversionを使用します。429、service error、timeoutは成功扱いにせず、最初の失敗で停止します。結果不明のrequestも自動再送しません。
送信先は承認済みproject endpointとagent名からResponses endpointを組み立て、`azd ai agent invoke --agent-endpoint ... --version ...`で指定します。毎回のagent情報再照会を省き、固定versionのsessionと同じ認証方式を維持します。

```powershell
.\.venv\Scripts\python.exe fixtures\push_prompts.py `
  --send --scenario-count 170 --seed 42 `
  --agent-name <agent-name> `
  --agent-version <agent-version> `
  --num-prompts <approved-count> `
  --offset <start-index> --stage <unique-stage-name> `
  --project-endpoint <project-endpoint>
```

170件の標準分割はSmoke `offset=0/count=8`、Pilot `8/25`、Full `33/137`です。各段階を承認後に個別実行します。Agent応答本文はmemory内で日本語を検査し、保存するのはID・時刻・usage・判定などのmetadataだけです。

Runnerは明示的な`AZD_CONFIG_DIR`を優先し、未指定ならmachine-localな`agent\.azd-client.json`の`config_dir`を使用します。この設定ファイルはGit管理外で、profileの絶対パスだけを保持し、credentialやtokenは記載しません。指定profileが無効な場合、既定profileへ切り替えず送信前に停止します。
`azd ai agent show`や`monitor`、`sessions`を直接実行する場合も、同じ値をそのコマンドのプロセス環境へ設定し、Runnerとprofileを揃えます。

最初に保持するtraffic start timeは最初の採用対象request送信直前のUTC、end timeは最後の成功request後に90秒以上待った時刻とします。そのwindowでAzure Monitor MCPのresource log queryを実行します。KQLは実行前に表示します。

Request件数と成功率を確認するKQL例:

```kusto
requests
| where timestamp between (datetime(<start-utc>) .. datetime(<end-utc>))
| extend
    agentName = coalesce(
        tostring(customDimensions["gen_ai.agent.name"]),
        tostring(customDimensions["azure.ai.agentserver.agent_name"])
    ),
    agentId = tostring(customDimensions["gen_ai.agent.id"])
| where agentName == "<agent-name>"
| extend agentVersion = coalesce(
    tostring(customDimensions["gen_ai.agent.version"]),
    extract(@":([^:]+)$", 1, agentId),
    tostring(customDimensions["azure.ai.agentserver.agent_version"])
  )
| where isempty(agentVersion) or agentVersion == "<agent-version>"
| summarize
    requestCount=count(),
    successCount=countif(success == true),
    failureCount=countif(success == false),
    firstSeen=min(timestamp),
    lastSeen=max(timestamp)
```

Tool-use traceは、同じwindowとagent name/versionで絞ったtop-level `requests`の`operation_Id`を使い、`dependencies`へ展開して確認します。Hosted agentの名前を`dependencies.gen_ai.agent.name`へ直接適用しません。

`fixtures\audit_traffic.kql`は、requestごとのtool argument/result、agent入出力、日本語文字の有無、文字化け、token使用量をまとめる再実行用queryです。対象windowへ置換してMCPで実行します。日本語文字の存在だけで英語混入なしとは判定せず、識別子allowlistとの照合も行います。費用にはagentの累積usageを優先し、`chat` span合計との差を確認します。Usage欠落を0件・無料として扱いません。

Telemetryにversion属性がない場合は、Step 5でactiveになったversion、最初のtraffic送信直前から始まる専用window、同じ時間帯に別versionへtrafficを送っていないことを根拠にversionを固定します。

最終的に次を確定します。

- Agent name/version
- UTC start/end
- Request件数と成功率
- Tool callを含むtrace件数
- Category coverage
- 日本語prompt/tool result/assistant responseの保存状態
- Data Generationに十分な多様性があるか

不足するcategoryがある場合だけ追加trafficを提案し、承認後に補います。件数だけを増やすための同一prompt大量送信は行いません。

**完了条件:** 生成件数、失敗件数、category分布、日本語応答率、費用の基礎となるtoken使用量、App Insightsへの日本語trace取り込みが確認され、固定agent versionに対するUTC start/endと十分なtool-use traceが確定する。

## Step 7: トレースから SFT データを生成する

Copilot は current SDK を使い、次の構成で Data Generation job を作成します。

- Client: `AIProjectClient(endpoint, DefaultAzureCredential())`
- API: `project_client.beta.datasets.begin_create_generation_job(job=...)`
- Scenario: `SUPERVISED_FINETUNING`
- Source: `TracesDataGenerationJobSource`
- Agent name/version: Step 5で確定した値
- Window: Step 6で確定したUTC start/end time
- Options: 初回 `max_samples=100`
- Output name: 50 文字以下で一意

送信前に Copilot がパラメーターを表示し、ユーザー承認を得ます。完了後は次を報告します。

- Job ID と最終 status
- `generated_samples`
- Output file count
- 0 件または想定より少ない場合の理由
- Generated sampleに日本語のuser/tool/assistant contentが保持されているか

Application Insights の取り込み直後なら、end time から 90 秒以上経過していることを確認します。

**完了条件:** Data Generation job が成功し、15 件以上のsampleと1件以上のfile outputを取得し、sampleの意味データが日本語で保持されている。

## Step 8: 変換、検証、レビューを行う

Copilot は notebook の transform 実装を再利用し、次の 5 修正を適用します。

1. Overlapping snapshots の重複除去
2. Assistant tool call がない fragment row の除外
3. Assistant tool-call row の文字列 `"null"` content の除去
4. 連続する assistant tool calls の結合
5. 承認済み system prompt と tools の注入

日本語を直接読めるよう、JSON serializationは`json.dumps(..., ensure_ascii=False)`を使います。`traces-raw.jsonl`はserviceから取得した原本を変更せず保存し、`traces-clean.jsonl`はUTF-8で出力します。

`run\traffic-quality-exclusions.json`に承認済みの除外対象がある場合、識別情報を除去する前にtrace ID、response ID、order IDで照合し、該当会話由来の行を学習候補から除外します。除外数と理由を記録し、対象を識別できない場合は採用を保留します。SDKエラー文を翻訳して成功結果として扱いません。

生成先:

```text
Demos\TracesDistillation\run\traces-raw.jsonl
Demos\TracesDistillation\run\traces-clean.jsonl
```

その後、Copilot がschema、role sequence、空response、tool name、重複、個人情報に加え、次を検証します。

- UTF-8として全行を再読込できる。
- `\uXXXX`だけで日本語が表現されず、日本語を直接読める。
- Protocol/identifier allowlist以外の意味データが日本語である。
- Tool argument/resultの日本語valueがStep 4のschemaと語彙に一致する。

最大20件をマスクして日本語で要約し、ユーザーが学習への採用を承認します。

**完了条件:** 変換前後の件数、除外理由、schema・日本語・encoding検証結果が報告され、データがユーザー承認済みである。

## Step 9: Split と baseline evaluation を実行する

デモ互換の初回実行では 80/10/10 の train/validation/test split を使用できます。ただし、同一会話または重複 content が split をまたがないことを hash で確認します。本番評価では、学習とは別の時間帯から test dataset を生成する方法を優先します。

Copilot は次を生成します。

```text
run\train.jsonl
run\val.jsonl
run\test.jsonl
run\eval_data.jsonl
run\baseline_eval_results.json
```

すべてのJSON serializationで`ensure_ascii=False`を使います。内部レビュー用の`test.jsonl`と`eval_data.jsonl`はUTF-8、fine-tuningへuploadする`train.jsonl`と`val.jsonl`はMicrosoft Learnの要件に従いUTF-8 BOM付き（Pythonの`utf-8-sig`）で出力します。BOM付きfileを読む処理も`utf-8-sig`を使います。

Split後に次を確認します。

- `train.jsonl`と`val.jsonl`の先頭にUTF-8 BOMが1つだけある。
- 全fileを指定encodingで再読込し、各行をJSON parseできる。
- 日本語が`\uXXXX`だけにescapeされず直接読める。
- File sizeが512 MB未満である。

Notebookと同じstructural tool-call evaluatorを使い、base student deploymentを日本語held-out test setで評価します。Tool callを評価するrowでは、tool nameと日本語argument valueの一致を測定します。Natural-language assistant contentを含むrowは、日本語で理解できるかを最大20件human reviewします。

報告する値:

- Train/validation/test の件数
- Tool match score
- Pass rate
- 日本語tool argument/value適合率
- Natural-language responseの日本語適合率またはreview結果
- Error count
- 代表的な失敗パターン

**完了条件:** Base studentの日本語baselineが保存され、tool-useと日本語適合の改善余地が確認でき、upload fileのschema・encoding・BOM要件を満たす。

## Step 10: Fine-tuning 送信前の承認ゲート

Copilot は課金前に次を 1 画面で提示します。

- Base student model/deployment
- Train/validation row count
- Hyperparameters: `n_epochs=3`, `learning_rate_multiplier=1.0`
- Training type
- 利用可能 quota
- Baseline score/pass rate
- 日本語tool argument/value適合率とresponse review結果
- 想定する成功条件

デモの既定成功条件は、base studentに対してtool match scoreが5%以上向上し、pass rateと日本語適合率が低下しないことです。元デモの英語datasetの絶対scoreとは比較しません。ユーザーが明示承認するまでジョブを送信しません。

**完了条件:** ユーザーが model、data、hyperparameters、課金を承認する。

## Step 11: Fine-tuning を送信して監視する

CopilotはEntra IDで取得したOpenAI clientを使い、UTF-8 BOM付きのtrain/validation filesをuploadしてSFT jobを送信します。Upload直前にBOM、file size、JSONL parse、10件以上のtraining example、日本語の直接表示を再検証します。Job IDを`run`の実行サマリーへ保存します。

監視では次を確認します。

- File processing status
- Fine-tuning job status
- Training/validation loss
- Event message
- Fine-tuned model ID

`failed` または `cancelled` は成功扱いにせず、エラーをそのまま報告して停止します。

**完了条件:** Job が `succeeded` になり、fine-tuned model ID が取得できる。

## Step 12: Fine-tuned model を Azure MCP でデプロイする

`notebook.ipynb` の Azure CLI を使う deploy セルは実行しません。

Copilot は次の順で直接操作します。

1. Foundry MCP で fine-tuned model の deployment capability と利用可能 SKU/capacity を取得
2. 対応する Foundry MCP deployment 操作を優先
3. Foundry MCP に該当操作がない場合のみ Azure ARM MCP deployment を使用
4. Provisioning state が `Succeeded` になるまで直接照会
5. 日本語promptで推論を1回実行し、readiness、文字化け、日本語tool argumentを確認

SKU は notebook の固定値を使わず、対象リージョンと fine-tuned model に対して Azure から取得した値を使用します。既存 deployment の上書きや削除は行いません。

送信前に deployment name、SKU、capacity、課金をユーザーに確認します。

**完了条件:** 新規 deployment が `Succeeded` で、テスト推論に成功する。

## Step 13: Fine-tuned model を評価する

Step 9と同じ日本語held-out test setとevaluatorを使い、base studentとfine-tuned studentを比較します。可能ならteacher modelも同じtest setで評価します。全modelへ同一の日本語system prompt、tools、promptを渡し、言語やschemaの差を混入させません。

最低限報告する値:

| Metric | 比較 |
|---|---|
| Tool match score | Base / Fine-tuned / Teacher |
| Pass rate | Base / Fine-tuned / Teacher |
| Lift | Fine-tuned 対 Base |
| Teacher gap closure | Fine-tuned が Teacher にどこまで近づいたか |
| 日本語tool value適合率 | Base / Fine-tuned / Teacher |
| 日本語response review | Base / Fine-tuned / Teacher |
| Latency | p50/p95 |
| Token usage | 平均 input/output tokens |

結果は `run\ft_eval_results.json` と実行サマリーへ保存します。

**完了条件:** Tool-useの成功条件と日本語適合条件を満たすか、追加データ・語彙修正・hyperparameter調整が必要かが明確になる。

## Step 14: 結果を確定し、必要なら後片付けする

Copilot は次をまとめます。

- 使用した agent/version と trace window
- 専用agentを作成した場合はsource path、teacher deployment、deploy method
- Traffic生成を行った場合はstage別の成功・失敗件数、category分布、token使用量
- Data Generation job ID と sample count
- Fine-tuning job ID と model ID
- Deployment name
- Baseline/FT/Teacher metrics
- 日本語化したcontractのhash、語彙、encoding検証結果
- 既知の制約と次の判断

Deployment、uploaded files、job records、Step 5で作成したhosted agent/versionの削除は、ユーザーが明示的に依頼した場合だけ実行します。再現に必要なagent source、fixture、traffic scenarioは削除しません。

**完了条件:** 再現に必要な非秘密情報と評価結果が `run` に揃い、不要な Azure リソースの扱いが決まる。

## トラブルシューティング

| 症状 | 対応 |
|---|---|
| Foundry MCP の認証失敗 | IDE/Copilot の Azure サインインを確認して再実行。CLI へ切り替えない |
| Trace count が 0 | Agent name/version、UTC window、App Insights 接続を Azure MCP で再確認 |
| 専用hosted agentを作成できない | 既存project ID、teacher deployment、`codeConfiguration`、agent identityのmodel accessを確認 |
| Tool call traceがない | Agentの6-tool登録、system prompt、representative prompt、`requests`から`dependencies`へのjoinを確認 |
| Promptとtool resultが矛盾する | `push_prompts.py`と`synthetic_store.py`が同じscenario規則とseedを使っているか確認 |
| Trafficのfailureが多い | Agent versionのactive状態、session readiness、429、schema mismatchを確認し、full trafficを停止 |
| `generated_samples == 0` | 取り込みから 90 秒待つ、window を広げる、managed identity の Reader role を確認 |
| 日本語が`\uXXXX`だけで表示される | JSON writeで`ensure_ascii=False`を使い、UTF-8で再出力 |
| 日本語が文字化けする | Source/reader/writerのencodingを確認。Upload用train/validationは`utf-8-sig`で統一 |
| Upload時にencoding error | Train/validationの先頭BOM、JSONL parse、512 MB未満を再検証 |
| 英語の意味データが混入する | Protocol/identifier allowlistを除外してsystem prompt、tool description、argument/result、responseを再検査 |
| 日本語tool argumentが一致しない | Step 4の共通語彙、JSON Schema enum、agent実装、traffic/evaluation dataを同じ値へ統一 |
| SDK method が見つからない | `azure-ai-projects>=2.5.0` と `project_client.beta.datasets.begin_create_generation_job` を確認 |
| 生成件数が少ない | Intelligent sampling と quality filtering を前提に、window または traffic の多様性を増やす |
| Tool-calling row が拒否される | 5-step transform、system prompt、tools、role sequence を再検証 |
| Fine-tuning が失敗 | Job events と file status を取得し、データ schema と quota を確認 |
| Deployment が失敗 | Azure MCP で対応 SKU、capacity、region availability、名前衝突を再取得 |

## 公式リファレンス

- [Convert agent traces into evaluation datasets](https://learn.microsoft.com/azure/foundry/observability/how-to/traces-to-dataset)
- [Customize a model with fine-tuning](https://learn.microsoft.com/azure/foundry/openai/how-to/fine-tuning)
- [Fine-tuning and tool calling](https://learn.microsoft.com/azure/foundry/openai/how-to/fine-tuning-functions)
- [Foundry hosted agents](https://learn.microsoft.com/azure/ai-foundry/agents/concepts/hosted-agents)
- [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/overview/agent-framework-overview)
- [Microsoft Foundry fine-tuning considerations](https://learn.microsoft.com/azure/foundry/openai/concepts/fine-tuning-considerations)
- [Azure AI Evaluation SDK](https://learn.microsoft.com/python/api/overview/azure/ai-evaluation-readme)

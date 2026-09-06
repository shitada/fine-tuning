# OneDriveの同じフォルダから別PCで再開する

この手順はWindows x64の別PCへ、既存のTraces Distillation検証を引き継ぐためのものです。Azure上のagentを作り直す手順ではありません。元PCでは今後このリポジトリのセッションを実行せず、移行先だけで作業します。

## GitHubとOneDriveの役割

GitHubにはソース、テスト、手順を保存します。次の実行固有ファイルはGit管理外のまま、組織のOneDriveで引き継ぎます。GitHubからのcloneだけでは今回の検証を再開できません。

| パス（このフォルダからの相対パス） | 用途 |
|---|---|
| `EXECUTION_LOG.md` | 最新の判断、承認、失敗、再開位置 |
| `run\traffic-scenarios.jsonl` | 固定した170件のシナリオ |
| `run\traffic-requests.jsonl`、`run\traffic-summary.json` | 送信履歴、成否不明の試行、二重送信防止 |
| `run\step6-resume-*.json`、`run\step6-pilot-audit.json` | 再開後Smoke/Pilotの計画、実績、監査 |
| `run\traffic-quality-exclusions.json` | 学習対象から除外する会話 |
| `run\quota-assessment.json`、`run\quota-evidence\` | Full実行前のquota調査 |
| `agent\.azure\` | 既存azd環境の設定。内容をチャットやGitへ公開しない |
| `run\handoff\` | 整合性manifest、依存version、カスタムazd拡張、復旧用snapshot |

`agent\azure.yaml`は既存projectへの非秘密の接続先を保持します。秘密情報を追加しないでください。`.venv`、認証cache、Copilotの会話履歴・ユーザー設定は移行先でそのまま利用できるとは限りません。

## 1. OneDriveとCopilot App

1. 元PCでファイルを保存し、OneDriveが「最新の状態」になり、同期エラーがないことを確認します。元PCのセッション・traffic runnerを再開しません。
2. 移行先で同じ組織のOneDriveを開き、`Documents\VSCode\work\fine-tuning`を探します。`C:\Users\...`のユーザー名部分が異なっていても構いません。
3. `fine-tuning`を右クリックし「このデバイス上で常に保持する」を選択し、ダウンロード完了を待ちます。`.git`や`run`なども必要です。
4. Copilot AppのSessions横の「+」から、**Add project from → Local folder or repository**を選び、この`fine-tuning`フォルダを追加します。GitHub repositoryから新しくcloneしません。
5. プロジェクトで新しいセッションを作り、実行場所に**既存のローカルリポジトリ**、モードに**Interactive**を選びます。新しいworktreeやCloudを選ばないでください。
6. 作業ディレクトリが同期フォルダのリポジトリルートであり、`main`が引き継いだcommitを指すことを確認します。別worktree、別clone、元PCのセッションを同時に動かしません。

既存の`.git`、未コミット変更、`run`をreset/clean/再生成しないでください。Copilotの旧セッションを別PCで復元することを前提にせず、以下の引き継ぎ指示を新しいセッションへ渡します。

## 2. 変更前の整合性確認

Python 3.13系を移行先へ用意します。まだ同期された`.venv`は使用しません。リポジトリルートで、移行先のPythonから次を実行します。

```text
python Demos\TracesDistillation\fixtures\verify_handoff.py
```

この処理は標準ライブラリだけを使い、Azure通信・ファイル変更をしません。`run\handoff\manifest.json`に記録したソース・実行記録・環境設定・配布物のSHA-256を比較します。ファイル名はOS間で扱えるようmanifest内では`/`区切りです。

不足・不一致があれば停止し、OneDriveの同期状態と元PCでの最終保存を確認します。エラーを無視したりmanifestを作り直したりしません。認証設定や実行履歴を変更する前に一度実施するための確認であり、正当に検証を再開した後の変更まで禁止するものではありません。

`run\handoff\state-snapshot.zip`は切替時点の`EXECUTION_LOG.md`、`run`の記録、`agent\.azure`の復旧用コピーです。自動展開しません。復旧が必要なときだけ別の空フォルダへ展開して比較し、新しい履歴を上書きしないでください。snapshotもGitHubへuploadしません。

## 3. Python環境を作り直す

元PCの`.venv`には元PCのPython絶対パスが入っています。移行先でそのまま動いても再利用せず、既存`.venv`を退避してから、Python 3.13系でこのフォルダの`.venv`を新しく作ります。リポジトリや`run`全体は削除しません。

依存関係は`run\handoff\requirements-source.txt`を使用します。元PCから取得した全packageのversion固定リストであり、認証情報やprivate package URLは含みません。新しい`.venv`のPythonを明示して、このリストからインストールします。ネットワーク/TLSやpackage取得に失敗した場合は停止し、証明書検証を無効化しません。

切替時点の主な実version:

| 項目 | Version |
|---|---|
| Python | 3.13.14 |
| openai | 2.54.0 |
| azure-ai-projects | 2.3.0 |
| azure-ai-evaluation | 1.18.3 |
| azure-identity | 1.25.3 |
| agent-framework-foundry | 1.11.0 |
| agent-framework-foundry-hosting | 1.0.0b260821 |

**現在の実環境は、初期手順Step 3に書かれたopenai 3.x / azure-ai-projects 2.5以上とは異なります。** 切替ではStep 6の再現を優先し、依存を一括更新しません。Step 7のData Generationへ進む前に、そのAPIに必要なSDKを別途確認し、必要なら別の仮想環境で準備します。Step 3の完了記録だけで現環境が後続APIにも対応すると判断しないでください。

## 4. azd拡張と認証を移行先で準備する

元PCのazdは1.31.1です。`azure.yaml`の最低versionだけでなく、カスタム拡張との互換性を確認します。移行先ではGit、azd、Azure MCP、`microsoft-foundry`スキルを利用可能にし、GitHubとAzureへそれぞれサインインします。これらのユーザー設定はリポジトリ同期とは別管理です。

今回のStep 6では、公開版beta.9そのものではなく、認証待ちに上限を追加した**`azure.ai.agents` 1.0.0-beta.9+auth-timeout.1**を使用しています。配布物は次の場所に保持します。

```text
run\handoff\azure-ai-agents-auth-timeout-c01766ff0850.zip
```

このzipはWindows x64用のazd extension bundleであり、認証profileのコピーではありません。内部にregistry、実行ファイル、license、noticeを含みます。標準版や最新版への置換を同等とみなさないでください。

この修正は元PCで発生したクライアント側の認証timeout対策であり、Azure上のagent自体には必須ではありません。移行先で公式版へ戻す場合は別途動作を確認し、今回の引き継ぎと同時に暗黙の切り替えを行わないでください。監査・再build用の修正済みsourceと元source archiveも`run\handoff`へ保存します。通常の再開にはbundleだけで十分です。

移行先のCopilotは、次の順で準備します。

1. `%LOCALAPPDATA%`配下など、**OneDrive外の移行先専用ディレクトリ**をazd profileに選びます。元PCの認証cache・token・`auth.json`はコピーしません。
2. 各azdコマンドのプロセスで、その絶対パスを`AZD_CONFIG_DIR`に指定します。`AZURE_DEV_USER_AGENT=microsoft_foundry_skill`もそのプロセスだけに指定します。シェル環境はツール呼び出し間で持続するとは限りません。
3. インストール済みazdの`extension install --help`を確認し、同じprofileで上のローカルbundleをインストールします。公式にサポートされたbundle installを使い、手動で認証profileを複製しません。
4. 依存拡張`azure.ai.inspector` 1.0.0-beta.3、`azure.ai.projects` 1.0.0-beta.5も含めてversion/互換性を確認します。元PCでは依存を先に用意し、bundleを`--force --no-dependencies`付きでインストールしました。`run\handoff\extension-versions.json`は元PCのversion記録です。profileが異なると拡張の登録も異なります。CLIの互換性警告も確認し、最新版を自動選択しません。
5. 対象tenantへの正規の対話認証を移行先で完了します。ユーザー操作が必要な場合はその時点で待機します。Azure MCPとSDKから対象リソースを参照できることも別に確認します。azdのサインインだけで全ツールの認証完了とみなさないでください。
6. 初期化済みprofileに`config.json`が存在することを確認し、`agent\.azd-client.json`の`config_dir`を移行先の絶対パスへ更新します。profileのパスだけを書き、credentialは書きません。共有されるファイルですが、以後は移行先だけが使用する前提です。
7. azd直接実行と`fixtures\push_prompts.py`の両方で、同じprofileを使います。明示した`AZD_CONFIG_DIR`は`.azd-client.json`より優先されます。

現在の`.azd-client.json`は元PCの`C:\Users\<user>\.copilot\session-state\...`配下を参照しており、そのprofileはOneDrive外です。新PCでパスだけコピーしても使えません。無効なprofileを既定profileへ黙って切り替えないでください。

`agent\.azure`の既存環境名は`demos-tracesdistillation-agent-dev`です。既存project/agentの接続設定を再利用し、移行のために`azd init`、provision、deploy、agent生成を実行しません。`run`や`.azure`が不足する場合は、まず同期・snapshotからの復旧を確認します。

## 5. 再開位置と実行前のゲート

2026-09-06の切替時点では、Step 6の再開後SmokeとPilotが完了しています。最終応答は33会話、承認済み品質除外1会話を差し引いた学習候補は32会話です。Fullは未承認・未送信で、quota調査結果まで保存されています。詳細と最新状態は`EXECUTION_LOG.md`末尾、`run\step6-resume-plan.json`、送信journal、除外記録を突き合わせます。

既存の失敗・成否不明の試行を成功扱いにしたり、未送信とみなして再送しません。承認はbatchごとであり、Smoke/Pilotの過去の承認をFullへ流用しません。追加150/200会話の試算は実行承認ではありません。現在の固定manifestの残り件数とも区別します。

先にoffline testsを実行します。リポジトリルートから次の既存unittest群を一度に実行できます。

```text
Demos\TracesDistillation\.venv\Scripts\python.exe -m unittest Demos.TracesDistillation.fixtures.tests.test_push_prompts Demos.TracesDistillation.fixtures.tests.test_verify_handoff Demos.TracesDistillation.agent.src.zava-traces-demo.tests.test_contract Demos.TracesDistillation.agent.src.zava-traces-demo.tests.test_synthetic_store
```

その後、Azure MCP等による読み取りで接続先と既存agent/versionを確認します。移行確認のためのinvokeもモデル費用が発生し得るため、自動実行しません。追加traffic、fine-tuning、deployment、suite生成は、それぞれ必要性と費用を説明してユーザー承認を得た後に行います。

## 新しいCopilotセッションに貼る指示

```text
このOneDriveフォルダで、別PCから引き継いだ検証を再開します。
Demos\TracesDistillation\MACHINE_HANDOFF.md、
COPILOT_INSTRUCTIONS.md、EXECUTION_LOG.md、agent\AGENTS.mdを読んでください。

まずverify_handoff.pyで切替時点のファイル整合性を確認し、
Gitの状態、runの履歴・除外記録、既存agent\.azure、
移行先のPython環境、カスタムazd拡張、azd profile、MCP/SDK認証を確認してください。
認証情報を表示・コピーしないでください。

不一致や元PCの絶対パスが残っていれば停止し、必要な修正を説明してください。
ローカル環境の準備は手順に従い、一度に1ステップだけ実施してください。
既存Azureリソースは再作成・再デプロイしないでください。
Fullは未承認です。追加traffic、過去の試行の再送、学習、評価suite生成は行わず、
まず再開位置と準備状況を報告して停止してください。
```

## 参考

- [Copilot Appでローカルリポジトリを追加する](https://docs.github.com/en/copilot/get-started/quickstart-copilot-app)
- [Copilot Appのセッション実行場所](https://docs.github.com/en/copilot/how-tos/github-copilot-app/agent-sessions)
- [OneDriveのFiles On-Demand](https://support.microsoft.com/en-us/onedrive/save-disk-space-with-onedrive-files-on-demand-for-windows)

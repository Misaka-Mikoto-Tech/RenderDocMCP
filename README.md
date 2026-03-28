# RenderDoc MCP Server

RenderDoc UI拡張機能として動作するMCPサーバー。AIアシスタントがRenderDocのキャプチャデータにアクセスし、グラフィックスデバッグを支援する。

## アーキテクチャ

```
Claude/AI Client (stdio)
        │
        ▼
MCP Server Process (Python + FastMCP 2.0)
        │ File-based IPC (%TEMP%/renderdoc_mcp/)
        ▼
RenderDoc Process (Extension)
```

RenderDoc内蔵のPythonにはsocketモジュールがないため、ファイルベースのIPCで通信を行う。

## セットアップ

### 1. RenderDoc拡張機能のインストール

```bash
python scripts/install_extension.py
```

拡張機能は `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp_bridge` にインストールされる。

### 2. RenderDocで拡張機能を有効化

1. RenderDocを起動
2. Tools > Manage Extensions
3. "RenderDoc MCP Bridge" を有効化

### 3. MCPサーバーのインストール

```bash
uv tool install .
uv tool update-shell  # PATHに追加
```

シェルを再起動すると `renderdoc-mcp` コマンドが使えるようになる。

> **Note**: 開発時は `uv tool install --editable .` を使うと、ソースコードの変更が即座に反映される。
> 安定版としてインストールする場合は `uv tool install .` を使用。

### 4. MCPクライアントの設定

#### Claude Desktop

`claude_desktop_config.json` に追加:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

#### Claude Code

`.mcp.json` に追加:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

## 使い方

1. RenderDocを起動し、キャプチャファイル (.rdc) を開く
2. MCPクライアント (Claude等) から RenderDoc のデータにアクセス

## MCPツール一覧

| ツール | 説明 |
|--------|------|
| `get_capture_status` | キャプチャの読み込み状態を確認 |
| `get_draw_calls` | ドローコール一覧を階層構造で取得 |
| `get_draw_call_details` | 特定のドローコールの詳細情報を取得 |
| `get_shader_info` | シェーダーのソースコード・定数バッファの値を取得 |
| `get_constant_buffer_data` | 特定ステージ/スロットの定数バッファを個別に取得 |
| `get_buffer_contents` | バッファの内容を取得 (Base64) |
| `get_texture_info` | テクスチャのメタデータを取得 |
| `get_texture_data` | テクスチャのピクセルデータを取得 (Base64) |
| `save_texture` | RenderDoc標準の保存機能でテクスチャを直接ファイル出力 |
| `get_pipeline_state` | パイプライン状態を取得 |

## 使用例

### ドローコール一覧の取得

```
get_draw_calls(include_children=true)
```

### シェーダー情報の取得

```
get_shader_info(event_id=123, stage="pixel")
```

`get_shader_info` の `constant_buffers` には、定数バッファのメタデータに加えて
`variables` が含まれる。各 cbuffer には以下のような情報が入る:

- `slot`: バインドスロット
- `size` / `byte_size`: 定数バッファのサイズ
- `resource_id`: RenderDoc上で解決されたバッファリソース
- `byte_offset` / `byte_size`: 実際に `GetCBufferVariableContents()` に渡したバッファ範囲
- `read_mode`: `explicit_resource` または `pipeline_bound`
- `variables`: 変数名・型・行列/ベクトル形状・値

### 特定の定数バッファを個別に取得

```
get_constant_buffer_data(event_id=123, stage="vertex", slot=1)
```

`get_constant_buffer_data` は 1 つの cbuffer だけを返す軽量なツールで、後段の
アセット書き出しや Unity 向けのマテリアル復元処理に向いている。

返り値には以下が含まれる:

- `name`: cbuffer名
- `slot`: バインドスロット
- `resource_id`: 実際に解決されたリソースID
- `byte_offset`: バッファ読み取り開始位置
- `byte_size`: バッファ読み取りサイズ
- `variables`: デコード済みの変数一覧

例:

```json
{
  "name": "cbuffer1",
  "slot": 1,
  "resource_id": "ResourceId::201",
  "byte_offset": 0,
  "byte_size": 65536,
  "read_mode": "explicit_resource",
  "variables": [
    {
      "name": "cb1_v0",
      "type": "VarType.Float",
      "rows": 1,
      "columns": 4,
      "value": [2.3643281, 47.286564, 94.57313, 141.8597]
    }
  ]
}
```

### パイプライン状態の取得

```
get_pipeline_state(event_id=123)
```

### テクスチャデータの取得

```
# 2Dテクスチャのmip 0を取得
get_texture_data(resource_id="ResourceId::123")

# 特定のmipレベルを取得
get_texture_data(resource_id="ResourceId::123", mip=2)

# キューブマップの特定の面を取得 (0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-)
get_texture_data(resource_id="ResourceId::456", slice=3)

# 3Dテクスチャの特定の深度スライスを取得
get_texture_data(resource_id="ResourceId::789", depth_slice=5)
```

### テクスチャをファイルに保存

```
# ディレクトリを指定すると、RenderDoc上のリソース名を使ってPNG保存
save_texture(resource_id="ResourceId::123", output_path="D:\\exports")

# 出力ファイル名を明示
save_texture(
    resource_id="ResourceId::123",
    output_path="D:\\exports\\albedo.png",
    file_format="png",
)
```

### バッファデータの部分取得

```
# バッファ全体を取得
get_buffer_contents(resource_id="ResourceId::123")

# オフセット256から512バイト取得
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
```

## 要件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- RenderDoc 1.20+

> **Note**: 動作確認はWindows + DirectX 11環境でのみ行っています。
> Linux/macOS + Vulkan/OpenGL環境でも動作する可能性がありますが、未検証です。

## ライセンス

MIT

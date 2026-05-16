# Codex Plugins

个人 Codex 插件集合，用于保存可复用的插件代码、marketplace 元数据、Skill、MCP 服务、脚本和安全的示例配置。

真实环境配置、账号密码、token、缓存内容不提交到 Git，只保留在本机。

## 插件列表

- `elk-log-query`：按服务名、关键字、traceId、时间范围和环境查询 ELK 或 Elasticsearch 日志
- `mysql-query`：按环境执行只读 MySQL 查询

## 仓库结构

```text
.agents/plugins/marketplace.json
plugins/<plugin-name>/.codex-plugin/plugin.json
plugins/<plugin-name>/.mcp.json
plugins/<plugin-name>/configs/*.example.json
plugins/<plugin-name>/skills/
plugins/<plugin-name>/scripts/
```

marketplace 中的插件路径保持相对仓库根目录：

```json
{
  "source": {
    "source": "local",
    "path": "./plugins/<plugin-name>"
  }
}
```

## 本地使用

克隆仓库：

```bash
git clone <repo-url>
cd codex-plugins
```

在 Codex 中添加这组插件时，使用下面的 marketplace 文件：

```text
.agents/plugins/marketplace.json
```

## 环境配置

仓库只提交示例配置。首次使用前复制 example 文件，再填写本机真实配置：

```bash
cp plugins/elk-log-query/configs/environments.example.json \
  plugins/elk-log-query/configs/environments.json

cp plugins/mysql-query/configs/environments.example.json \
  plugins/mysql-query/configs/environments.json
```

以下本地文件已被 `.gitignore` 忽略：

```gitignore
plugins/*/configs/environments.json
plugins/*/cache/
*.local.json
```

## 维护约定

- 只提交 example 配置，不提交真实 host、账号、密码、token 或内部缓存内容
- 插件 manifest 固定放在 `plugins/<plugin-name>/.codex-plugin/plugin.json`
- `.agents/plugins/marketplace.json` 中的 `source.path` 必须相对仓库根目录
- 每个插件的脚本优先放在各自的 `scripts/` 目录下，保持职责清晰
- 推送前校验 JSON 文件，避免 marketplace 或插件 manifest 失效


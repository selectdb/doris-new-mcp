# MCP CLI 使用说明

`mcp-client.sh` 是 Doris MCP Server 自带的命令行客户端，适合在终端、脚本和 CI/CD 中：

- 查看、描述和调用 MCP Tool；
- 检查 MCP Server、Doris 和语义工作区状态；
- 执行单条只读 Doris SQL；
- 查看、下载和暂存修改语义模型文件。

它不是自然语言问数 Agent。调用时需要明确指定 Tool 名称和参数。

## 1. 使用前提

使用 CLI 需要：

1. 一个已经运行的 Doris MCP Server；
2. 一个可以登录目标 Doris 的用户名和密码；
3. 在项目或发行包根目录执行 `./mcp-client.sh`。

认证值的格式为：

```text
<Doris 用户名>:<Doris 密码>
```

CLI 会把它作为以下 HTTP 请求头发送：

```text
Authorization: Bearer <Doris 用户名>:<Doris 密码>
```

> 不要把真实密码提交到 Git，也不要把包含密码的配置文件放进镜像或公开日志。

## 2. 配置 CLI

CLI 支持环境变量和 TOML 文件两种配置方式。两种方式不能混合补全：使用 `--config` 后，CLI 只读取指定文件。

### 2.1 环境变量（推荐）

```bash
export DORIS_MCP_SERVER="https://your-mcp-host.example.com"
export DORIS_MCP_TOKEN="<user>:<password>"
```

`DORIS_MCP_SERVER` 必须是服务根地址，末尾不要写 `/mcp`。CLI 调用标准 MCP Tool 时会自动连接：

```text
https://your-mcp-host.example.com/mcp
```

例如，北京和美国节点可以分别配置为：

```bash
# 北京 SelectDB
export DORIS_MCP_SERVER="https://albjf5vx.cn-beijing.aliyun.selectdb.cloud"

# 美国 VeloDB
export DORIS_MCP_SERVER="https://awvah8gc.us-east-1.aws.velodb.io"
```

切换节点后继续使用相同命令即可。

### 2.2 TOML 配置文件

创建一个不纳入版本控制的 `mcp-client.toml`：

```toml
[server]
DORIS_MCP_SERVER = "https://your-mcp-host.example.com"
DORIS_MCP_TOKEN = "<user>:<password>"
```

指定配置文件运行：

```bash
./mcp-client.sh --config ./mcp-client.toml tool list
```

字段名称区分大小写，必须使用 `DORIS_MCP_SERVER` 和 `DORIS_MCP_TOKEN`。

### 2.3 从 macOS 源码目录运行

发行包通常带有可直接使用的 Python 运行时。当前源码目录中的内置 Python 是 Linux 可执行文件，在 macOS 开发环境中应指定项目虚拟环境：

```bash
export DORIS_MCP_PYTHON="$PWD/.venv/bin/python"
```

然后检查 CLI：

```bash
./mcp-client.sh --help
```

## 3. 快速验证

### 3.1 查看服务端 Tool

```bash
./mcp-client.sh tool list
```

Tool 清单由服务端动态返回。当前版本通常包含：

```text
get_query_guide
list_databases
list_tables
describe_table
execute_query
check_service_health
list_metrics
list_dimensions_for_metric
query_metric
reload_semantic_layer
```

### 3.2 检查服务健康状态

```bash
./mcp-client.sh tool call check_service_health
```

成功响应示例：

```json
{
  "success": true,
  "data": {
    "doris": "connected",
    "workspaces": {
      "example": {
        "status": "healthy",
        "metric_count": 5
      }
    },
    "semantic": {
      "status": "loaded"
    }
  }
}
```

### 3.3 验证只读查询链路

```bash
./mcp-client.sh tool call execute_query \
  --arg "sql=SELECT 1 AS ok"
```

`execute_query` 只允许单条只读 SQL。DML、DDL、多语句以及 `SELECT INTO OUTFILE/DUMPFILE` 会被拦截。

## 4. MCP Tool 命令

### 4.1 查看 Tool 参数

```bash
./mcp-client.sh tool describe execute_query
```

### 4.2 使用 `--arg` 调用

每个参数使用一个 `--arg key=value`：

```bash
./mcp-client.sh tool call describe_table \
  --arg database=dw \
  --arg table=orders
```

CLI 会尝试把值解析成 JSON，因此数字、布尔值、数组和对象能够保留类型：

```bash
./mcp-client.sh tool call execute_query \
  --arg database=dw \
  --arg max_rows=20 \
  --arg "sql=SELECT * FROM orders LIMIT 20"
```

### 4.3 使用 `--json` 调用

复杂参数可以作为完整 JSON 对象传入：

```bash
./mcp-client.sh tool call query_metric \
  --json '{"workspace":"example","metrics":["total_amount"],"group_by":["channel"]}'
```

`--arg` 和 `--json` 不能同时使用。

## 5. 物理表和只读 SQL

典型的物理数据查询流程为：

```bash
./mcp-client.sh tool call list_databases

./mcp-client.sh tool call list_tables \
  --arg database=dw

./mcp-client.sh tool call describe_table \
  --arg database=dw \
  --arg table=orders

./mcp-client.sh tool call execute_query \
  --arg database=dw \
  --arg "sql=SELECT * FROM orders LIMIT 10"
```

目标 Doris 用户仍需具备相应库表的查询权限。

### 5.1 Doris MATCH 全文检索

`MATCH_ANY`、`MATCH_ALL` 等是 Doris 中缀运算符，不是普通函数。

正确：

```sql
SELECT *
FROM articles
WHERE content MATCH_ANY 'error 404'
LIMIT 10
```

CLI 调用：

```bash
./mcp-client.sh tool call execute_query \
  --arg database=search_db \
  --arg "sql=SELECT * FROM articles WHERE content MATCH_ANY 'error 404' LIMIT 10"
```

错误：

```sql
SELECT * FROM articles WHERE MATCH_ANY(content, 'error 404')
```

错误写法会让 Doris 把 `MATCH_ANY` 当成函数，并可能返回：

```text
Can not found function 'MATCH_ANY'
```

## 6. 语义指标查询

健康检查会返回当前已经加载的工作区。需要语义指标时，再调用相应语义 Tool：

```bash
./mcp-client.sh tool call list_metrics \
  --json '{"workspace":"example"}'

./mcp-client.sh tool call list_dimensions_for_metric \
  --json '{"workspace":"example","metric_name":"total_amount"}'

./mcp-client.sh tool call query_metric \
  --json '{
    "workspace":"example",
    "metrics":["total_amount"],
    "group_by":["channel"],
    "order_by":["-total_amount"],
    "limit":10
  }'
```

查询前可以使用 `tool describe <tool-name>` 核对当前服务端的参数定义。

## 7. 语义模型文件命令

| 命令 | 作用 |
|---|---|
| `semantic status` | 查看 Doris 和语义工作区状态 |
| `semantic list` | 列出工作区 YAML 文件 |
| `semantic view` | 查看 YAML 文件内容 |
| `semantic pull` | 下载工作区文件 |
| `semantic push` | 上传文件到 staging |
| `semantic result` | 查询旧版异步 push 任务 |
| `semantic edit` | 编辑 staging 文件 |
| `semantic delete` | 暂存删除文件 |
| `semantic reload` | 重新加载当前 active 语义层 |

### 7.1 读取操作

列出工作区文件：

```bash
./mcp-client.sh semantic list -w example
```

查看文件：

```bash
./mcp-client.sh semantic view orders.yaml -w example
```

下载工作区文件：

```bash
./mcp-client.sh semantic pull \
  --output ./backup \
  --workspace example
```

### 7.2 暂存修改

上传本地文件：

```bash
./mcp-client.sh semantic push ./models -w example
```

从本地文件更新：

```bash
./mcp-client.sh semantic edit orders.yaml \
  --file ./orders.yaml \
  --workspace example
```

使用编辑器修改：

```bash
EDITOR=vim ./mcp-client.sh semantic edit orders.yaml -w example
```

暂存删除：

```bash
./mcp-client.sh semantic delete orders.yaml -w example
```

### 7.3 当前发布限制

当前 CLI 的 `push`、`edit` 和 `delete` 只修改 staging。服务端还要求执行 **Validate** 和 **Commit** 后才会正式生效，但 CLI 暂未提供对应命令。

因此当前完整发布流程是：

1. 使用 CLI 上传或编辑 staging 文件；
2. 打开 `https://your-mcp-host.example.com/mcp/web`；
3. 在 Web UI 中执行 **Validate**；
4. 验证成功后执行 **Commit**。

当前 `semantic push/result` 的异步结果提示也可能与新版服务端响应不一致。自动化发布前应先补齐 CLI 的 `validate`、`commit` 并统一 `push` 响应格式。

当前 `semantic status` 还会向无参数的 `check_service_health` 传入旧版 `detail` 参数，在严格校验参数的服务端上可能失败。此时直接使用：

```bash
./mcp-client.sh tool call check_service_health
```

## 8. 权限说明

| 操作 | 要求 |
|---|---|
| `tool list/describe` | 有效的 Doris 登录凭据 |
| 物理表发现和 `execute_query` | Doris 用户具备目标对象的读取权限 |
| 语义指标查询 | 有效凭据及可用工作区 |
| `semantic list/view/pull` | 已认证用户 |
| `semantic push/edit/delete/reload` | Doris 用户拥有 `admin` 角色 |

MCP 的 `execute_query` 始终保持只读。Admin role 只用于语义层管理，不会给 `execute_query` 增加写 SQL 能力。

## 9. 全局选项和退出码

显示详细错误信息：

```bash
./mcp-client.sh --verbose tool list
```

详细错误会隐藏认证 Token。也可以设置：

```bash
export DORIS_MCP_DEBUG=1
```

默认情况下客户端不使用系统代理。如确实需要使用 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理：

```bash
export DORIS_MCP_USE_SYSTEM_PROXY=1
```

常用退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 调用成功 |
| `1` | 配置、连接、认证、Tool 或服务端调用失败 |
| `2` | `semantic result` 返回异步任务仍在处理中 |

## 10. 常见问题

### `No server configured`

确保两个变量同时存在：

```bash
export DORIS_MCP_SERVER="https://your-mcp-host.example.com"
export DORIS_MCP_TOKEN="<user>:<password>"
```

### URL 最后是否需要 `/mcp`

不需要。`mcp.json` 等通用 MCP 客户端配置使用完整的 `/mcp` URL；本 CLI 配置的是服务根地址。

### `cannot execute binary file`

源码目录中的内置 Python 与当前系统平台不匹配。在 macOS 项目环境中运行：

```bash
export DORIS_MCP_PYTHON="$PWD/.venv/bin/python"
```

### 认证失败

确认 Token 使用 `<Doris 用户名>:<Doris 密码>`，并确认该用户能直接登录目标 Doris。开启 `--verbose` 查看经过脱敏的详细错误。

### `MATCH_ANY` 找不到函数

不要写 `MATCH_ANY(column, 'keywords')`。应写：

```sql
column MATCH_ANY 'keywords'
```

如果中缀语法仍然失败，再检查 Doris 实际版本、查询优化器设置、字段类型以及倒排索引配置。

### 健康检查仍返回 `semantic.mode = preferred`

这通常表示目标节点仍运行旧版服务。CLI 本身不会修改服务端加载策略，需要升级目标 MCP Server 部署。

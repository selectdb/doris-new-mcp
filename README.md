# doris-new-mcp

Apache Doris MCP Server — 基于 MCP 协议的 Doris 查询服务，内置 MetricFlow 语义层。

## 快速开始

```bash
# 1. 下载二进制包
tar xzf doris-mcp-server-0.3.0-linux-x64.tar.gz
cd doris-new-mcp

# 2. 确保同机 Doris FE 运行在 127.0.0.1:9030

# 3. 启动
./start-mcp-server.sh
```

## 编译构建

### 方式一：有网络（下载 Python standalone）

```bash
./build.sh linux-x64
```

自动从 GitHub 下载 Python 3.10 standalone，安装依赖，打包。

### 方式二：无网络 / 下载失败（使用本地 Python）

```bash
DORIS_MCP_SYSTEM_PYTHON=/path/to/python3.10 ./build.sh linux-x64
```

适用于 GitHub 不可达的环境。将本地 Python 3.10 及依赖复制到包内。

### 构建产物

```
dist/
├── doris-mcp-server-0.3.0-linux-x64.tar.gz   # 服务端，自包含 Python + 所有依赖
└── doris-mcp-client-0.3.0-linux-x64.tar.gz   # CLI 客户端
```

### 清理

```bash
./build.sh clean
```

### 运行测试

```bash
# 冒烟测试（快速）
bash test/run_all_tests.sh --smoke

# 全部测试（42 用例）
bash test/run_all_tests.sh
```

## MCP 工具

| Tool | 用途 |
|------|------|
| `get_query_guide` | Agent 工作流指引 |
| `check_service_health` | Doris 连通性 + 工作区状态 |
| `list_metrics` | 列出语义层指标 |
| `list_dimensions_for_metric` | 查看指标可用维度 |
| `query_metric` | 语义查询（MetricFlow 编译） |
| `list_databases` | 列出数据库 |
| `list_tables` | 列出表 |
| `describe_table` | 查看表结构 |
| `execute_query` | 裸 SQL（只读） |
| `reload_semantic_layer` | 手动重载语义层 |

## 依赖修复记录

原始 `requirements.txt` 缺少 4 个 MetricFlow 传递依赖，已补充：

- `jinja2` — 模板引擎
- `rapidfuzz` — 模糊匹配
- `python-dateutil` — 时间计算
- `tabulate` — 表格格式化

## 文档

- [DESIGN.md](DESIGN.md) — 架构设计文档
- [INSTALL.html](INSTALL.html) — 安装指南
- [doris-mcp-docs.html](doris-mcp-docs.html) — 完整文档

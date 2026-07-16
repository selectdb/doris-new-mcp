# Test Cases — doris-new-mcp

基于 [DESIGN.md](../DESIGN.md)、[INSTALL.html](../INSTALL.html)、[doris-mcp-docs.html](../doris-mcp-docs.html) 生成。

## 文件说明

| 文件 | 内容 | 用例数 |
|------|------|--------|
| `test_mcp_tools.py` | 全部 10 个 MCP Tool + 认证 + E2E | **30** |
| `test_web_api.py` | Web UI + REST API + 工作区管理 | **12** |
| `run_all_tests.sh` | 一键运行脚本，支持分模块运行 | — |

## 环境要求

| 条件 | 说明 |
|------|------|
| MCP Server | 运行在 `localhost:3000` |
| Doris FE | 运行在 `127.0.0.1:9030` |
| 认证 | `admin:admin` |
| Python | 3.10+ (仅运行测试) |

可通过环境变量覆盖默认配置:
```bash
export MCP_URL=http://192.168.1.100:3000/mcp
export MCP_BASE_URL=http://192.168.1.100:3000
export MCP_TOKEN=admin:admin
export MCP_WORKSPACE=example
```

## 运行方式

```bash
# 全部测试
bash test/run_all_tests.sh

# 仅冒烟测试 (快速, ~5秒)
bash test/run_all_tests.sh --smoke

# 仅 MCP Tool 测试
bash test/run_all_tests.sh --tools

# 仅 Web/API 测试
bash test/run_all_tests.sh --web

# 或直接运行 Python
python test/test_mcp_tools.py
python test/test_web_api.py
```

## 测试覆盖矩阵

### MCP Tool 测试 (10 个 Tool)

| Tool | 测试场景 | 验证点 |
|------|----------|--------|
| `get_query_guide` | 获取工作流指引 | 返回文本 >100字，含关键词 |
| `check_service_health` | 基础/详细 | Doris=connected, workspaces 存在 |
| `list_metrics` | 列表/分页 | data 数组, meta.total_count |
| `list_dimensions_for_metric` | 按指标查维度 | data 包含维度 |
| `query_metric` | 基础/group_by/where/order+limit | 4 种查询模式 |
| `list_databases` | 列表/分页 | dw,mysql,system_mcp,information_schema |
| `list_tables` | mysql库/dw种子上表/like模糊 | 4 张种子表验证 |
| `describe_table` | summary/full/names | 3 级详细程度 |
| `execute_query` | SELECT/VERSION/SHOW/EXPLAIN/max_rows/写拦截 | 7 种场景 |
| `reload_semantic_layer` | 手动重载 | 返回成功 |

### Web UI & API 测试

| 分类 | 测试点 |
|------|--------|
| **Web UI** | 登录页面、登录提交、未认证拦截、模型管理页 |
| **REST API** | 语义文件列表、pull 下载、reload 重载、staging validate/discard |
| **工作区** | 创建 → 验证存在 → 删除 (完整生命周期) |
| **认证** | admin 权限控制、Bearer token 格式验证 |

### 边界 & 错误测试

| 场景 | 预期 |
|------|------|
| 无 Authorization | 401/403 |
| 无效 Token | 401/403 |
| 写操作 SQL (INSERT) | 被拦截 |
| SQL 语法错误 | 友好错误信息 |
| 非 admin 创建工作区 | 403 |
| 不存在的指标 | 友好错误 |

### 端到端测试 (Agent 工作流)

```
get_query_guide → check_service_health → list_databases
  → list_tables → describe_table → execute_query
```

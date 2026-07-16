# doris-new-mcp

Apache Doris MCP Server — 基于 MCP 协议的 Doris 查询服务，内置 MetricFlow 语义层。

## 目录结构

```
├── build.sh                 # 编译构建脚本
├── requirements.txt         # Python 依赖列表
├── mcp-server.toml          # 服务配置文件
├── start-mcp-server.sh      # 启动脚本
├── src/                     # 源码
├── test/                    # 测试用例
├── mcp-client/              # CLI 客户端源码
└── DESIGN.md                # 架构设计文档
```

## 编译构建

### 前置条件

- Linux x86_64 环境
- curl 或 wget（方式一需要）
- Python 3.10+（方式二需要，已有 conda/miniconda 即可）

### 方式一：在线编译（GitHub 可访问）

```bash
# 自动从 GitHub 下载 Python 3.10 standalone，安装依赖，打包
./build.sh linux-x64
```

构建过程：
1. 下载 `python-build-standalone` (cpython 3.10.16)
2. 解压到 `python/` 目录
3. `pip install -r requirements.txt` 安装全部依赖
4. 打包为自包含 tar.gz（自带 Python，无需系统 Python）

### 方式二：离线编译（GitHub 不可达）

当 `python-build-standalone` 下载失败时，使用本地已有的 Python 3.10：

```bash
# 指向本地的 Python 3.10（conda 安装的或系统安装的均可）
DORIS_MCP_SYSTEM_PYTHON=/opt/miniconda3/bin/python ./build.sh linux-x64
```

构建过程：
1. 复制本地 Python 到 `python/` 目录
2. `pip install -r requirements.txt` 安装依赖
3. 打包（与方式一产出相同）

### 构建产物

```
dist/
├── doris-mcp-server-0.3.0-linux-x64.tar.gz   # 服务端 (~92MB)
└── doris-mcp-client-0.3.0-linux-x64.tar.gz   # CLI 客户端 (~92MB)
```

两个包都是 **完全自包含** 的：自带 Python 解释器 + 所有 pip 依赖，部署无需网络、无需系统 Python。

### 清理

```bash
./build.sh clean      # 删除 dist/ 和 python/ 构建产物
```

## 部署

```bash
# 解压即用
tar xzf dist/doris-mcp-server-0.3.0-linux-x64.tar.gz
cd doris-new-mcp

# 确保同机 Doris FE 运行在 127.0.0.1:9030

# 启动（前台）
./start-mcp-server.sh

# 启动（后台）
nohup ./start-mcp-server.sh > /dev/null 2>&1 &
```

## 运行测试

```bash
# 冒烟测试（快速，约 5 秒）
bash test/run_all_tests.sh --smoke

# 全部测试（42 用例）
bash test/run_all_tests.sh
```

## 依赖修复

原始 `requirements.txt` 缺少 4 个 MetricFlow 传递依赖，已补充：

| 依赖 | 用途 |
|------|------|
| `jinja2` | 模板引擎 |
| `rapidfuzz` | 模糊匹配 |
| `python-dateutil` | 时间计算 |
| `tabulate` | 表格格式化 |

## 文档

- [DESIGN.md](DESIGN.md) — 架构设计文档
- [INSTALL.html](INSTALL.html) — 安装指南
- [doris-mcp-docs.html](doris-mcp-docs.html) — 完整文档

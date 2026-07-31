# doris-new-mcp

Apache Doris MCP Server — 基于 MCP 协议的 Doris 查询服务，内置 MetricFlow 语义层。

## 目录结构

```
├── build.sh                 # 编译构建脚本
├── pyproject.toml           # 项目元数据 + 依赖（版本号单一事实源）
├── uv.lock                  # uv 锁定的依赖版本
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
- Python 3.10.x（方式二需要，必须是 3.10.x，已有 conda/miniconda 即可）

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
└── doris-mcp-server-1.3.1-linux-x64.tar.gz   # 单包 all-in-one（server + client + 文档 + Python 运行时）
```

该包是 **完全自包含** 的：自带 Python 解释器 + 所有 pip 依赖，部署无需网络、无需系统 Python。

### 清理

```bash
./build.sh clean      # 删除 dist/ 和 python/ 构建产物
```

## 部署

```bash
# 解压即用
tar xzf dist/doris-mcp-server-{version}-linux-x64.tar.gz
cd doris-mcp-server

# 确保同机 Doris FE 运行在 127.0.0.1:9030

# 启动（前台）
./start-mcp-server.sh

# 启动（后台）
nohup ./start-mcp-server.sh > /dev/null 2>&1 &
```

### 多机部署（同一域名多节点）

多台 MCP Server 挂在 ALB 后方时，只需在**所有节点**的 `mcp-server.toml` 中配置同一个 `privateIp`：

```toml
[server]
privateIp = "10.0.0.13"   # 所有节点填同一个 IP：指定的 Web UI 节点
```

效果：

- 所有 `/mcp/web` 请求（含登录）自动转发到该节点，session 只存在一台机器
- `/mcp`（MCP 协议）无状态，各节点本地处理
- 三台机器配置文件完全一致，nginx 只做哑代理，无需任何 Cookie 解析
- 不配置 `privateIp` 时退化为按 Cookie 后缀的会话亲和（各节点自动探测本机 IP）

详见 [DESIGN.md](DESIGN.md) §8.3。

## 运行测试

```bash
# 离线单元测试（无需 MCP Server）
bash test/run_all_tests.sh --offline

# 冒烟测试（快速）
bash test/run_all_tests.sh --smoke

# 全部测试（需本地 MCP Server）
bash test/run_all_tests.sh
```

## 文档

- [DESIGN.md](DESIGN.md) — 架构设计文档
- [INSTALL.html](INSTALL.html) — 安装指南
- [doris-mcp-docs.html](doris-mcp-docs.html) — 完整文档

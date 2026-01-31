# webSeach-mcp

基于 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 的网络搜索服务器，使用 Playwright 进行浏览器自动化，支持多个搜索引擎并提供页面内容抓取功能。

## 功能特性

- **多搜索引擎支持**: 同时使用 Bing、DuckDuckGo、百度 3 个搜索引擎进行搜索
- **智能结果过滤**: 自动过滤高质量结果并去重
- **页面内容抓取**: 自动提取搜索结果的页面正文内容
- **状态持久化**: 保存浏览器登录状态，支持验证码处理
- **灵活输出格式**: 支持 JSON 和 Markdown 两种输出格式
- **并发抓取**: 支持高并发页面抓取，提高效率

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/你的用户名/webSeach-mcp.git
cd webSeach-mcp
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 安装 Playwright 浏览器

```bash
playwright install chromium
```

## MCP 服务器配置

### Claude Desktop 配置

在 Claude Desktop 的配置文件中添加此服务器：

**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "webSeach": {
      "command": "python",
      "args": ["E:/WorkSpace/Projects/Mcp/webSeach-mcp/server.py"],
      "env": {
        "HEADLESS": "true"
      }
    }
  }
}
```

### 环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `HEADLESS` | 是否使用无头模式运行浏览器 | `false` |
| `DEBUG` | 是否启用调试模式 | `false` |

> **注意**: 首次使用时建议设置 `HEADLESS: false`，以便手动处理搜索引擎的验证码。验证完成后，浏览器状态会被保存，后续可切换为无头模式。

## 使用方法

### 在 Claude 中使用

配置完成后，重启 Claude Desktop 即可使用 `web_search` 工具：

```
请帮我搜索 "Python 异步编程教程"
```

### 工具参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 搜索查询内容 |
| `top_k` | integer | 否 | 每个引擎返回结果数 (1-20)，默认 10 |
| `format` | string | 否 | 输出格式：`json` 或 `md`，默认 `json` |

## 项目结构

```
webSeach-mcp/
├── server.py              # MCP 服务器入口
├── tools.py               # MCP 工具定义
├── config.py              # 配置文件
├── requirements.txt       # Python 依赖
├── webSeach/              # 搜索引擎核心模块
│   ├── search_engines.py  # 搜索引擎实现
│   ├── page_crawler.py    # 页面内容抓取
│   ├── state_cache.py     # 浏览器状态缓存
│   ├── models.py          # 数据模型
│   ├── utils.py           # 工具函数
│   ├── run.py             # 独立运行脚本
│   └── .cache/            # 状态缓存目录
└── README.md
```

## 独立运行

如需独立测试搜索功能（不通过 MCP）：

```bash
cd webSeach
python run.py
```

## 许可证

MIT License

# MCP 渐进披露设计

## 问题

MCP 工具参数嵌套深时，LLM 直接在原生 `tool_calls` 的 `arguments` 中构造 JSON 容易出错（特别是多层嵌套对象），导致 tool_call 失败，错误内容被当作文本回复发给用户。

全量展开所有 action 的 inputSchema 虽然参数结构清晰，但 schema 体积大、浪费 token。

## 方案：树形导航 + 分层披露

将 MCP 能力组织为**树形结构**，LLM 通过一个统一的导航工具 `explore_mcp` 逐层下钻：

- **根节点 (`/`)**：所有 MCP 服务器列表
- **中间节点**：按服务器/分类组织 action
- **叶子节点**：具体的 action，显示完整 inputSchema 并可直接执行

类似文件系统的 `cd` + `ls`，但一步完成。

```
/
├── capture (服务器，带 description + use_cases)
│   ├── status (叶子，可执行)
│   ├── query (叶子，可执行)
│   └── 运营管理 (子组，非叶子)
│       ├── report (叶子)
│       └── audit (叶子)
├── salesforce
│   ├── records (叶子)
│   └── opportunities (叶子)
```

## 工具设计

只注册一个元工具，替代之前所有 MCP 相关的工具注册：

### `explore_mcp`

```python
registry.register(
    "explore_mcp",
    handler=_handle_explore_mcp,
    description="树形导航 MCP 能力。path 类似文件系统路径：'/' 根节点列出所有服务器，'/server_id' 进入服务器查看 action/子组，'/server_id/action' 为叶子节点显示完整参数 Schema 并可执行。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "树中路径，默认 '/'。如 '/' 返回所有服务器；'/capture' 返回 capture 下的操作；'/capture/status' 为叶子节点，显示参数 Schema 并在有 params 时执行。",
                "default": "/",
            },
            "params": {
                "type": "object",
                "description": "仅叶子节点使用。执行该 action 的参数，按返回的 inputSchema 构造 JSON。不带 params 时只返回 Schema。",
            },
        },
        "required": ["path"],
    },
    category="system",
)
```

## 行为定义

### 根节点 `explore_mcp(path="/")`

返回所有 MCP 服务器列表，每个服务器只暴露：
- `server_id` / 标题
- 一行描述
- 操作数量
- 状态

**不暴露任何 action 名或参数 Schema。**

### 中间节点 `explore_mcp(path="/capture")`

进入服务器，返回该服务器的树状结构：
- 子组（可继续展开）
- 叶子 action（显示名 + 一行描述）
- 每个节点标记 `type: "group" | "action"`

**不暴露参数 Schema。**

### 叶子节点 `explore_mcp(path="/capture/status")`

叶子节点行为分两种情况：

1. **不带 `params`** — 只返回该 action 的完整 inputSchema，**不执行**
   ```json
   {
     "node_type": "action",
     "server_id": "capture",
     "action": "status",
     "description": "...",
     "inputSchema": { "type": "object", "properties": {...} },
     "hint": "再次调用 explore_mcp 并带上 params 即可执行"
   }
   ```

2. **带 `params`** — 直接用 MCP `tools/call` 执行，返回执行结果

## LLM 典型工作流

```
用户: "查一下当前状态"

Round 1: LLM 调 explore_mcp(path="/")
  → 返回: capture 服务器可用

Round 2: LLM 调 explore_mcp(path="/capture")
  → 返回: status(叶子), query(叶子), 运营管理(子组)

Round 3: LLM 调 explore_mcp(path="/capture/status", params={})
  → 返回: 执行结果 "当前状态: 运行中..."

Round 4: LLM 回复用户 "当前状态是运行中"
```

## 树结构数据来源

MCP 服务器的 `manifest.json` 中 `actions` 数组扩展支持层级：

```json
{
  "actions": [
    { "name": "status", "description": "查看系统状态", "inputSchema": {...} },
    { "name": "query", "description": "查询数据", "inputSchema": {...} },
    {
      "name": "运营管理",
      "description": "运营相关操作",
      "children": [
        { "name": "report", "description": "生成报告", "inputSchema": {...} },
        { "name": "audit", "description": "审计日志", "inputSchema": {...} }
      ]
    }
  ]
}
```

没有 `children` 的节点视为叶子。实现时先按平铺 action 列表处理（所有 action 都是叶子），未来可扩展支持显式子组。

## 实现要点

1. **`explore_mcp` 是唯一注册的 MCP 相关工具**，替换之前的所有元工具
2. MCP 所有 action 不直接注册到 ToolRegistry（`_register_server_tools` 只缓存）
3. 叶子节点的执行通过 `_handle_explore_mcp` 内直接调 `call_tool()` 完成
4. `build_awareness_summary` 保持当前风格，只显示服务器概览

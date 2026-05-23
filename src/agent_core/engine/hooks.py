class AgentHooks:
    """Agent 生命周期钩子。

    所有方法默认都是空操作（返回 None），
    只需覆写需要的钩子即可。

    每个方法的语义：
      - 返回 None / 不覆写 → 不做任何修改，继续默认行为
      - 返回修改后的值    → 用返回值替换原始值
    """

    def before_llm_call(self, messages, config):
        """LLM 调用前，可修改 messages。
        Args:
            messages: 当前消息列表（可修改该列表或返回新列表）
            config: AgentConfig
        Returns:
            Optional[list] — 修改后的 messages，或 None 不做变更
        """
        return None

    def after_llm_call(self, response, messages):
        """LLM 调用后，可修改 response。
        Args:
            response: LLMResponse
            messages: 当前消息列表
        Returns:
            Optional[LLMResponse] — 修改后的 response，或 None 不做变更
        """
        return None

    def before_tool_dispatch(self, name, args, ctx):
        """Tool 分发前，可修改参数或阻止调用。
        Args:
            name: 工具名称
            args: 参数字典
            ctx: 工具上下文
        Returns:
            dict  — 修改后的 args（替换原始参数）
            None  — 不做变更，继续默认行为
            False — 阻止此工具调用（ToolResult(error="blocked by hook")）
        """
        return None

    def after_tool_dispatch(self, name, result, ctx):
        """Tool 分发后，可修改结果。
        Args:
            name: 工具名称
            result: ToolResult
            ctx: 工具上下文
        Returns:
            Optional[ToolResult] — 修改后的 result，或 None 不做变更
        """
        return None

    def before_response(self, reply, steps):
        """最终回复前，可修改回复内容。
        Args:
            reply: 最终回复文本
            steps: 执行步骤列表
        Returns:
            Optional[str] — 修改后的 reply，或 None 不做变更
        """
        return None

    def on_error(self, error):
        """发生异常时调用（纯通知，不能阻止错误）。
        Args:
            error: 异常对象
        """
        pass

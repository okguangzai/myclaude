欢迎来到 MyClaude 学习工作区。

你可以在网页 Agent 工作台中让模型读取这份文件，或在这里放置学习代码。
只读模式支持 read_file、list_files、search_files 和 retrieve_context。
选择“允许工作区写入”后，write_file 工具可以创建或覆盖工作区文件。

Agent 循环：用户任务 → 模型选择工具 → 权限检查 → 执行工具 → 返回结果 → 模型继续。
模型负责决策，Python 运行时负责真实操作、状态和停止条件。

# MyClaude：Claude Code 风格的 Python Agent 学习项目

MyClaude 是 [WhosU-Dady](https://github.com/WhosU-Dady) 的个人学习项目，基于开源项目 [ultraworkers/claw-code](https://github.com/ultraworkers/claw-code) 进行 Python 重构与功能复刻，用于学习 AI 应用与 Agent 开发。它以 Python 实现原 Rust 项目的运行时，并增加 Vue Web 工作台，方便配置模型、运行任务、查看工具调用和理解 Agent 架构。

公开仓库：[WhosU-Dady/myclaude](https://github.com/WhosU-Dady/myclaude)。欢迎参考代码、交流学习与提出改进建议。

这里的“复刻 Claude”指 Claude Code 风格的编程助手与 Agent 工作流。项目不是 Anthropic 官方产品，也不是 Claude 模型或 Claude Code 官方源码的移植；实际参考的是上述开源 claw-code 项目。模型通过 API 接入，可以使用兼容的云模型服务，不要求使用 Claude 模型。

后端使用 Python + FastAPI，前端使用 Vue 3 + TypeScript，日常运行不依赖 Rust 可执行文件。

## 主要功能

- **Agent 对话与工具调用**：流式聊天、文件读写、代码搜索、命令执行、权限控制和运行轨迹。
- **多 Agent 与后台任务**：子 Agent、Worker、团队协作、任务状态和定时调度。
- **知识库问答**：文档导入、Embedding、Qdrant 向量检索，为模型提供检索上下文。
- **扩展机制**：MCP 服务、插件与 hooks；第三方平台可通过适配工具接入，当前没有内置飞书机器人接入模块。
- **Web 配置与管理**：模型配置、会话管理、工具实验室、宏、别名和分享管理。
- **多模态与客户端**：图片输入、语音识别与合成、VS Code/Cursor 扩展和 PWA 资源。

## 版本与迁移范围

v0.8 新增原版 25 个占位命令的实际实现和 Vue 管理页，见 [完整功能使用与验收](docs/完整功能使用与验收.md)。此前 v0.7 补齐 CLI 本地命令、工具返回与参数、子 Agent 类型及模型、会话管理、Config 偏好、LSP/Cron 和 PDF/Lane 辅助模块。11 个 crate 职责均有 Python 对应实现；原版功能、Python/Vue 适配和原版占位项见 [迁移收口清单](docs/迁移收口清单.md)。保留原 Rust 实际执行生成的差分夹具，日常运行和 CI 无需 Rust。

## 现在开始

```powershell
cd C:\csg\work\project\myCalude
.\start.ps1
```

打开 [MyClaude 网页](http://127.0.0.1:8000)，从「Agent 工作台」开始，在「设置」中配置模型服务地址和模型名。密钥通过本机私有配置或服务器 Secret 保存，不提交到 Git。新环境需要配置自己的模型凭据，仓库不包含可用 API Key。

现有部署的主模型为百炼 `qwen-flash`，图片模型为 `qwen3-vl-flash`，Embedding 为 `text-embedding-v4`；服务器通过集群内网访问 Qdrant。本地开发需配置本机可达的 Qdrant 地址，或按使用指南配置 SSH 转发。

首次安装或迁移到另一台电脑时先执行 `install.ps1`。停止服务执行 `stop.ps1`。网页生产构建由 FastAPI 同时托管，无需同时启动两个服务。

## 文档

- [使用指南](docs/使用指南.md)：安装、启动、配置、聊天、知识库、常见问题。
- [架构设计](docs/架构设计.md)：架构图、请求时序、模型协议、工具权限、数据结构及学习顺序。
- [完整功能使用与验收](docs/完整功能使用与验收.md)：v0.8 功能操作、架构图、学习路线与验收边界。
- [扩展开发与学习](docs/扩展开发与学习.md)：MCP、插件、hooks、任务调度的实践与源码阅读顺序。
- [逐项迁移矩阵](docs/迁移矩阵.md)：55 个工具和 140 个命令的逐项状态。
- [迁移与验收](docs/迁移与验收.md)：Rust 能力映射、已完成与未迁移内容、验证方式和实际结果。
- [全量迁移进度](docs/全量迁移进度.md)：按运行时模块记录代码、历史验收和最终适配约定。
- [独立程序与模型路由](docs/独立程序与模型路由.md)：Analog、RAG、Mock 的启动方式和备用模型配置。
- [交互式 API 文档](http://127.0.0.1:8000/api/docs)：服务运行时可用。

## 目录

```text
backend/app/       Python API、Agent、模型适配、工具与 RAG
backend/tests/     不依赖真实云服务的自动化测试
frontend/src/      Vue 页面与 SSE 客户端
docs/             中文文档与页面截图
scripts/          初始配置导入与显式真实模型验收
examples/         可运行的 MCP 服务和插件示例
workspace/        默认 Agent 文件操作目录
.local/           本机配置、SQLite、日志，Git 忽略
.env              用户原有配置，保留不改，Git 忽略
```

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check backend scripts
.\.venv\Scripts\ruff.exe format --check backend scripts
cd frontend
npm.cmd run test
npm.cmd run build
```

上述日常测试不需要 Rust。若要重新生成或核验上游差分证据，需要另外构建原项目，再运行 `scripts/verify_rust_interchange.py --rust <claw可执行文件路径>`。真实云服务、浏览器设备授权和第三方账号登录需要单独联调，不能用单元测试替代。

原项目参考基线为 [ultraworkers/claw-code](https://github.com/ultraworkers/claw-code/tree/08106b0c3771ef5b4a5aa176acccd460e88b7325)。保留上游 [MIT License](LICENSE) 与版权声明。

## 服务器与自动发布

私有仓库：<https://gitlab.csghome.fun/csg/myclaude>。服务器入口：<https://myclaude.csghome.fun/>。
`main` 推送触发测试、镜像构建和 Kubernetes 发布，详见 [GitLab 部署与运维](docs/GitLab部署与运维.md)。
Linux 流水线执行后端测试、前端测试和 Vue 构建；历史提交、测试数量及云模型、Embedding、Qdrant 验证见 [部署验收记录](docs/部署验收记录.md)，当前功能边界见 [完整功能使用与验收](docs/完整功能使用与验收.md)。

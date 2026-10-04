# MyClaude Python Agent Studio

这是面向 AI 应用与 Agent 开发学习的 Python 重构版。后端使用 FastAPI，前端使用 Vue 3 和 TypeScript，提供模型配置、流式聊天、55 个上游工具、MCP、插件/hooks、多 Agent、后台任务、知识库检索和运行轨迹。项目不依赖 Rust 可执行文件。

v0.7 补齐 CLI 本地命令、工具返回与参数、子 Agent 类型及模型、会话管理、Config 偏好、LSP/Cron 和 PDF/Lane 辅助模块。11 个 crate 职责均有 Python 对应实现；原版功能、Python/Vue 适配和原版占位项见 [迁移收口清单](docs/迁移收口清单.md)。保留原 Rust 实际执行生成的差分夹具，日常运行和 CI 无需 Rust。

## 现在开始

```powershell
cd C:\csg\work\project\myCalude
.\start.ps1
```

打开 [MyClaude 网页](http://127.0.0.1:8000)，从「Agent 工作台」开始。当前已配置百炼 `qwen-flash`、`text-embedding-v4`，并通过 SSH 接入远程 Qdrant。所有配置均可在网页「设置」中调整。

首次安装或迁移到另一台电脑时先执行 `install.ps1`。停止服务执行 `stop.ps1`。网页生产构建由 FastAPI 同时托管，无需同时启动两个服务。

## 文档

- [使用指南](docs/使用指南.md)：安装、启动、配置、聊天、知识库、常见问题。
- [架构设计](docs/架构设计.md)：架构图、请求时序、模型协议、工具权限、数据结构及学习顺序。
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
.\.venv\Scripts\python.exe scripts/verify_rust_interchange.py --rust C:/csg/work/project/claude/rust/target/debug/claw.exe
cd frontend
npm.cmd run build
```

原项目参考基线为 [ultraworkers/claw-code](https://github.com/ultraworkers/claw-code/tree/08106b0c3771ef5b4a5aa176acccd460e88b7325)。保留上游 [MIT License](LICENSE) 与版权声明。
## 服务器与自动发布

私有仓库：<https://gitlab.csghome.fun/csg/myclaude>。服务器入口：<https://myclaude.csghome.fun/>。
`main` 推送触发测试、镜像构建和 Kubernetes 发布，详见 [GitLab 部署与运维](docs/GitLab部署与运维.md)。
Linux 流水线执行全部后端测试和 Vue 构建；准确提交、测试数量及云模型、Embedding、Qdrant 验证见 [部署验收记录](docs/部署验收记录.md)。



0.5 补齐配置/插件注册表、Markdown 导出和 JSON 契约，新增 Rust 缓存文件互读与 648 组 Bash 差分验收。独立域名部署、使用与兼容边界见上述文档；日常运行只需要 Python/Node，Rust 仅用于源码对照验收。

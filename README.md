# 学习型 Personal Agent

中文学习工作台，首页直接与 Agent 聊天，无目标也能提问；围绕目标、计划、练习、评估证据和主动站内消息持续推进学习。后端使用 FastAPI、Temporal、Pydantic AI、Hindsight 和 LiteLLM；前端使用 React 与 TypeScript。

首版代码已实现，真实模型与生产环境验收仍在进行。基础设施测试、确定性测试和真实供应商验证分别记录；没有真实模型凭据时，不会用预置回答伪装学习结果。功能、测试和剩余门槛见 [实现状态](docs/implementation-status.md)，依赖与部署实测见 [兼容与验证记录](docs/compatibility.md)。

## Docker 部署

安装 Docker Engine / Docker Desktop 与 Docker Compose V2+。首版按 4 vCPU、16 GiB RAM、40 GiB 可用空间规划，尚未构成性能承诺。首次启动会下载 Hindsight 镜像和固定版本的本地多语言模型。

```bash
cp .env.example .env
# 在本地编辑 .env：设置初始账号密码，选择供应商并填写模型 ID、API Key 和输入/输出费率。
./scripts/deploy.sh
```

默认入口为 [http://localhost:8080](http://localhost:8080)。密钥只保存在本地 `.env`、受保护的 `.secrets` 和内部服务密钥卷，不能提交 Git 或粘贴到聊天。

首页聊天框与侧边栏“和 Agent 聊聊”共享个人对话历史；每个目标还有专属辅导对话。消息与执行阶段持久保存，支持取消和失败重试。

支持阿里云百炼、智谱、火山方舟的标准推理 API，通过 `MODEL_PROVIDER=bailian|zhipu|ark` 选择主模型。主别名固定为 `learning-agent`，备用模型默认关闭；开启前需完成结构化输出、工具调用、中文和预算测试。Coding Plan 凭据与端点不适用于本部署。

详细配置、TLS、备份恢复、升级与排障见 [部署说明](docs/deployment.md)。产品行为、数据契约和验收要求见 [开发文档](docs/development-plan.md)。

## 开发与验证

```bash
cd backend
uv sync --frozen
uv run pytest
```

```bash
cd frontend
npm ci
npm test -- --run
npm run build
```

完整基础设施就绪后，`./scripts/smoke-test.sh` 验证数据库、API、Temporal Worker、LiteLLM、Hindsight 与 Web，不额外发起付费模型探测。关闭网页后，消息仍会保存；首版只提供站内消息，不发送邮件或系统推送。

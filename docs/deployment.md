# 部署与运维

本说明对应仓库实际脚本。完整生产验收需要真实供应商模型和 Linux 主机验证；当前逐项结果见 [compatibility.md](compatibility.md)。

## 1. 运行条件

- Docker Engine 与 Compose V2 或以上；macOS Docker Desktop 可用于开发。
- 目标生产平台 Linux amd64；上游镜像清单同时提供 arm64，但清单存在不代表整套系统已在该平台验收。
- 初始规划 4 vCPU、16 GiB RAM、40 GiB 可用 SSD。为备份预留数据库、附件及配置总大小至少两倍的临时空间。
- 能访问 Docker Hub、GHCR、Debian/PGDG 软件源、PyPI/npm、Hugging Face 和选定的模型供应商。
- 本地多语言模型首次下载可能较慢。缓存存入命名卷，失败后重新部署会复用已下载文件。

只有 Docker 是部署脚本运行的额外依赖；配置检查在固定 Python 容器中执行，不要求宿主机安装 Python/Node。脚本使用 POSIX shell、tar、grep 等系统基本工具。

## 2. 首次部署

```bash
cp .env.example .env
# 在本地编辑配置，勿把真实凭据发送到聊天、日志或 Git。
./scripts/deploy.sh
```

配置为逐行 `KEY=value`，可用单引号或双引号包住完整值。不支持变量引用、命令替换、多行或反斜杠转义；不要使用 `$`。初始密码至少 16 个字符；可以由本地密码管理器生成。`.env` 写入权限收紧为 0600，`.secrets` 目录为 0700。单个 secret 文件为只读，以便指定服务的容器用户读取；目录权限阻止其他宿主用户遍历。Docker 守护进程管理员本来就拥有容器密钥访问能力。

必须填写：`BOOTSTRAP_USERNAME`、`BOOTSTRAP_PASSWORD`、`MODEL_PROVIDER`，以及所选供应商的 `*_MODEL`、`*_API_BASE`、`*_API_KEY`。占位符、空密码和不匹配的公开 URL 会使预检失败；错误只显示字段名。

| 供应商配置 | 标准推理入口 | 注意事项 |
| --- | --- | --- |
| `MODEL_PROVIDER=bailian`，`BAILIAN_*` | `https://dashscope.aliyuncs.com/compatible-mode/v1`，也可填写业务空间专属域名 | 业务空间、地域、模型可用性和 Key 必须匹配 |
| `MODEL_PROVIDER=zhipu`，`ZHIPU_*` | `https://open.bigmodel.cn/api/paas/v4` | 不使用 `/api/coding/paas/v4` |
| `MODEL_PROVIDER=ark`，`ARK_*` | `https://ark.cn-beijing.volces.com/api/v3` | 模型或推理接入点 ID 由方舟控制台确认 |

模型 ID 不设推测默认值。三个供应商配置独立，只需启用主供应商；Key 只挂给 LiteLLM。Worker 和 Hindsight 仅持有各自有模型范围、RPM/TPM 和预算限制的虚拟密钥。网关管理密钥不发给应用或浏览器。主模型别名固定为 `learning-agent`。

备用模型需要设置 `MODEL_FALLBACK_PROVIDER`，并在能力验证后显式设置 `MODEL_FALLBACK_VERIFIED=true`；未完成实测不能仅为了让预检通过而修改该标志。网关重试为 0，工作流按业务策略重试，避免重试层数相乘。

配置真实凭据和供应商额度后，可由部署者显式执行 `./scripts/smoke-test.sh --live-model`。该命令会产生模型费用：最多 4 次请求、每次最多 512 输出 token、总 token 上限 6000，验证工具调用、工具结果续接、结构化结果与中文。一次只测当前主别名；先分别选择并验证候选主模型，再配置 fallback，不能用主模型成功替代备用模型验收。不要在没有费用授权时自动运行该命令。

必须为选定模型填写 `*_INPUT_USD_PER_MILLION` 与 `*_OUTPUT_USD_PER_MILLION`，使用供应商当期价格换算的美元每百万 token 费用；脚本不会猜价。费用写入网关模型配置，避免 OpenAI 兼容别名无法识别原厂价格。预算以美元配置，供应商实际计费、缓存/思考 token 和费用上报仍需联调验证，并应同时在供应商控制台设置额度。部署健康检查不代表供应商认证、工具调用或费用计算已验证。

脚本会停止已有应用写入进程，再依次执行数据库初始化、Alembic 迁移、Temporal schema、Hindsight 迁移、命名空间和网关密钥初始化，最后启动服务并验收。初始化可重入；重复部署保留初始账号密码、数据库、附件、服务密钥和模型缓存。迁移失败会返回非零并保持应用停止，不会自行删除卷或退回旧 schema。

部署不会额外运行付费模型测试。但是恢复已有 Workflow、Hindsight 后台任务或用户的新操作可能按原有授权继续使用模型额度。

## 3. 访问和 TLS

默认 `APP_PUBLIC_URL=http://localhost:8080`、`APP_BIND_HOST=127.0.0.1`、`APP_PORT=8080`、`TLS_MODE=local`。只有 Web 映射宿主端口，数据库、Temporal、LiteLLM 和 Hindsight 均在 Compose 内网。不要手动把管理端口暴露到公网。

两种 HTTPS 方式：

1. **已有反向代理**：`TLS_MODE=upstream`，填写真实 `APP_PUBLIC_URL=https://域名`，Web 保持环回端口，由可信反向代理转发。代理需支持 SSE，关闭响应缓冲，读取超时至少一小时。应用根据公开 URL 设置安全 Cookie；确保浏览器请求 Origin 与该 URL 一致。
2. **项目 Nginx TLS**：把证书链和私钥分别放到 `.secrets/tls/fullchain.pem`、`.secrets/tls/privkey.pem`，设置 `TLS_MODE=nginx`、`APP_PUBLIC_URL=https://域名`、`APP_BIND_HOST=0.0.0.0`、`APP_PORT=80`、`APP_TLS_PORT=443`。脚本生成受保护的 Compose override，挂载证书并启用 TLS。证书签发/续期由部署者负责；更新证书后重启 Web。若选非标准 TLS 端口，公开 URL 要包含相同端口，HTTP 自动跳转仅适用于 443。

不能在公网使用 HTTP 登录。本项目不申请域名或自动购买证书。TLS 开关不是已经通过公网安全验收的证明。

## 4. 日常操作与健康

因为 TLS override 由脚本生成，日常命令需包含两个配置文件：

```bash
docker compose --env-file .env -f compose.yaml -f .runtime/compose.override.json ps
./scripts/smoke-test.sh
docker compose --env-file .env -f compose.yaml -f .runtime/compose.override.json stop
./scripts/deploy.sh
```

`ENV_FILE` 可指定仓库内的另一个配置文件；`COMPOSE_PROJECT_NAME` 可区分独立实例。整个生命周期始终使用相同项目名，不要为重复部署随意更改。普通停止不使用删除卷的参数。

`/health/live` 检查 API 进程，`/health/ready` 检查数据库与应用就绪。脚本还运行一个无模型的 Temporal SmokeWorkflow，证明 Worker 能实际消费任务。Hindsight 故障时运行中的应用可以按业务规则降级，但整栈健康检查会失败，不会把降级标为全部正常。

SSE 与普通 HTTP 同源，Nginx 对 API 关闭缓冲和缓存，保留 EventSource 重连头。业务页面与事件可靠性由 E2E 另外验证。容器日志每份 10 MiB、最多 3 份；不要把完整环境列表、展开后的 Compose 配置、完整模型输入或用户提交写入日志。

## 5. 加密备份

使用 age 公钥进行认证加密，收件私钥应另行安全保存。脚本自动构建包含 age 的运维镜像；不自动生成或打印私钥。

```bash
./scripts/backup.sh 'age1...公钥...'
```

脚本进入维护窗口，停止正在运行的 Web、API、Worker、Dispatcher、Temporal、LiteLLM 和 Hindsight，保留数据库服务。备份包含 app、temporal、temporal_visibility、litellm、hindsight 五个数据库、附件、受限服务密钥、内部凭据、环境配置和清单。模型缓存可重新下载，不必进入备份。私有临时目录中的明文在退出时清理，最终 `.backups/*.tar.age` 权限为 0600。此前运行的原容器随后恢复，不通过 Compose 重新执行迁移依赖；如果恢复容器失败，脚本返回非零并提示检查。失败时不会宣称备份成功。

备份期间不要从其他容器或直接数据库连接写入数据。加密备份也不能提交 Git；应另行复制到安全存储并定期演练恢复。

## 6. 恢复到全新部署

先在**同版本代码的新目录**准备恢复，目录中不得已有 `.env` 或 `.secrets`，目标 Compose 项目不得已有容器或卷：

```bash
COMPOSE_PROJECT_NAME=learning-recovery ./scripts/restore.sh \
  /absolute/path/backup.tar.age /absolute/path/age-identity --new-deployment
```

恢复脚本明确拒绝覆盖已有部署。认证解密后按安全 tar 解包规则恢复配置和数据；确认五份数据库 dump、内部凭据和卷归档完整，缺失时拒绝生成替代凭据。仅启动新数据库，保持应用停止。恢复阶段检查配置结构，不要求供应商 Key 可用，也不调用模型；正式部署仍会严格检查凭据和费率。然后检查恢复的公开 URL、端口、TLS、模型预算和原版本兼容性：

```bash
COMPOSE_PROJECT_NAME=learning-recovery ./scripts/deploy.sh
COMPOSE_PROJECT_NAME=learning-recovery ./scripts/smoke-test.sh
```

还需登录确认目标、计划历史、对话、附件与未完成 Workflow 可读且能继续。恢复失败可能留下新创建但未完整恢复的卷；脚本不会删除它们，也不会在第二次运行时覆盖。确认情况后另选全新项目名/目录重试。

2026-10-20 在本机进行了两次独立 Compose 项目和全新卷的恢复演练：原 PostgreSQL 17.6 的五个数据库还原到核心 PostgreSQL 17.11；`0001_initial` 快照中的用户、目标、对话等六表行内容哈希一致，原账号可登录并读取目标，受限服务密钥和内部凭据字节一致；第二轮加入仅存在于测试卷的附件，恢复后字节一致。错误 age 身份和覆盖原目录均被拒绝。演练未运行第二套 Hindsight 模型，也未验证未完成 Workflow 或记忆后台任务的继续执行；这些仍需正式验收。

## 7. 升级与排障

升级先阅读兼容记录，再完成备份，部署新代码和镜像。不可逆数据库迁移之后，切回旧镜像不能代替恢复备份。Dockerfile/Compose 中的固定摘要不应在生产临时改成 latest。

| 现象 | 检查方向 |
| --- | --- |
| 配置预检失败 | 修正提示的变量名；本地检查占位符、密码长度和 TLS/URL，不打印凭据 |
| 镜像或模型下载失败 | 检查网络/仓库可达性、磁盘和代理；保留缓存后重试 |
| 数据库 healthy 但应用未启动 | 查看相应一次性迁移任务的退出码，不能绕过迁移强行上线 |
| Temporal 不消费 | 检查 namespace、task queue、Worker 状态；运行无模型 smoke |
| 模型返回认证/额度错误 | 在本地核对所选供应商 Key、地域和标准推理入口；健康接口不验证供应商 |
| 记忆模型内存不足 | 增加容器内存，检查 OOM；默认单实例记忆 Worker 和低并发 |
| 消息不刷新 | 检查登录会话、Origin、SSE 网络连接和上游反代缓冲 |
| 卷/磁盘接近满 | 检查日志、模型缓存、备份与数据库大小，不直接删除 PostgreSQL 数据目录 |

Hindsight 固定 worker ID 为 `hindsight-main`；恢复记忆后台任务的测试仍属于生产验收，不能通过更改 ID 回避问题。

官方参考：[百炼兼容接口](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)、[智谱接口](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)、[方舟接口](https://www.volcengine.com/docs/82379/1330310)、[Hindsight 配置](https://hindsight.vectorize.io/developer/configuration)、[Temporal 自托管](https://docs.temporal.io/self-hosted-guide/deployment)。

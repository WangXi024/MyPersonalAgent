# 兼容与验证记录

记录日期：2026-10-20。状态分为“代码/清单核验”“本机真实运行”“待验收”；不能将清单存在或 Mock 测试当作真实模型与生产环境验收。

## 锁定的镜像与模型

下列摘要均已通过公开 Registry 清单查询确认，均包含 Linux amd64 与 arm64。Compose/Dockerfile 使用 tag 加多架构 index digest，避免 tag 漂移。自构建镜像还依赖受锁文件约束的应用包；Debian 运维镜像中的 age 包和操作系统包仓库不是完整可复现快照，生产更新应重新做漏洞扫描。

| 组件 | 固定版本 | Index digest |
| --- | --- | --- |
| Python | `3.12.15-slim-bookworm` | `sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258` |
| Node | `22.23.3-bookworm-slim` | `sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392` |
| Nginx | `1.28.3-alpine` | `sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236` |
| 核心 PostgreSQL | `17.11-bookworm` | `sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826` |
| PGroonga 基础镜像 | `4.0.8-debian-17` | `sha256:15ab42f1b92749950da640b6d88f5563498a2ac06b2d0aaee1f9185afe34e376` |
| Temporal Server | `1.32.1` | `sha256:acecbab5b0b8ae9cf4012b508866ca97feda3709577bf157b537239f40029049` |
| Temporal admin-tools | `1.32.1` | `sha256:9ef0dc5b063bf4ffd5c1512de72e0e73e225bd2c7006a18ce69bd592a8862a83` |
| LiteLLM database | `v1.106.0` | `sha256:80a12a210e6626e39daaa31fb6ce70f4c4d97ee10e21d4584a9e87f45bf3a2f1` |
| Hindsight Full | `1.0.0` | `sha256:a88678c038b0e4340822b02a5efc566c56d8176b5bffa731bc433fd37f978e4c` |

记忆数据库按 Hindsight 官方配方构建，在 PGroonga 基础镜像上安装固定 `postgresql-17-pgvector=0.8.6-1.pgdg13+1`。包仓库若移除此版本，构建会失败，不会静默换版。

| 本地模型 | 用途 | 固定 revision |
| --- | --- | --- |
| `intfloat/multilingual-e5-small` | 中文/多语言向量，query/passage 前缀 | `614241f622f53c4eeff9890bdc4f31cfecc418b3` |
| `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | 多语言重排 | `1427fd652930e4ba29e8149678df786c240d8825` |

入口先按 revision 下载到持久缓存，再把本地 snapshot 路径传给 Hindsight，避免仅指定模型名称时随仓库更新。禁用远程自定义模型代码的默认设置保持不变。

开发文档原候选为 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`。本次选择较小的 E5-small 与 mmarco MiniLM，目的是降低单机 CPU、模型下载与常驻内存负担，先完成真实部署兼容验证；没有把这次替换描述为原候选的验收，也没有证据声称中文质量等同或优于 BGE。中文学习记录的检索/排序质量仍需固定评测集验证；若后续换向量模型或维度，须做重新嵌入/索引迁移，不能直接替换已有数据的模型名。

E5 必须区分 `query: ` 与 `passage: `。检查锁定版源码发现：Hindsight `local` provider 忽略通用远程 provider 的前缀环境变量，使用 SentenceTransformers `encode_query` / `encode_document`；本次固定 E5 snapshot 没有自带 prompts。因此入口在独立缓存目录链接原始 snapshot，并仅添加 `config_sentence_transformers.json` 的 query/document prompts，原始下载权重不变。容器中已独立验证：中文测试句的 `encode_query` 与显式 `query: ` 编码数值一致，`encode_documents` 与显式 `passage: ` 一致，维度 384 且输出有限值。这是接入正确性验证，不是完整中文召回质量评测。

## 应用依赖

精确依赖以 `backend/uv.lock`、`frontend/package-lock.json` 为准。当前后端关键组合为 Python 3.12、Pydantic AI 2.57.0、Temporal Python SDK 1.34.0、FastAPI 0.150.0、SQLAlchemy 2.0.55、Alembic 1.20.0、asyncpg 0.32.0。使用 Pydantic AI 的 `TemporalDurability`，不使用已弃用的 `TemporalAgent` 接口。

## 当前本机基础设施验证

测试环境：macOS arm64，Docker Desktop Linux arm64 容器，Docker Engine/CLI 29.6.1，Compose 5.3.0。未使用隐式 amd64 模拟。它不能代替 Linux amd64 生产验收。

| 检查 | 结果 |
| --- | --- |
| Compose 静态结构、所有 shell 脚本语法 | 通过 |
| 配置检查测试 | 6 项通过：占位符拒绝、重复凭据保存及权限、公网 HTTP 拒绝、上游 TLS 明文端口保护、fallback 门槛、显式费率转换 |
| 应用与 Web 构建 | 新锁定 Python/Node/Nginx 补丁版本镜像构建通过 |
| 应用非 root 启动、Alembic、重复账号 bootstrap | 通过；发现并修复降权后的 HOME 指向 root 问题；已有账号密码不重置 |
| PostgreSQL 角色/库隔离、重复初始化 | 通过；app、temporal、temporal_visibility、litellm 分库分角色 |
| PGroonga 与 pgvector 自构建 | 通过，Hindsight 专用库迁移与 PGroonga 索引创建通过 |
| Temporal 正式服务与 schema | 通过；匹配 admin-tools 初始化与重复升级，命名空间创建成功 |
| Temporal 容器 Worker smoke | 通过；实际 Workflow、持久 timer 与 Activity 返回成功，无模型调用 |
| LiteLLM PostgreSQL 存储与 readiness | 通过 |
| LiteLLM 两个受限服务 key bootstrap | 首次和重复运行通过；数据库保留两条服务 key，未执行模型推理 |
| Nginx 静态登录页与 API 同源反向代理 | HTTP 检查通过，API readiness 正常 |
| 默认基础设施 smoke 脚本 | 全项通过：API、实际 Worker Workflow、LiteLLM、Hindsight、Nginx；没有供应商推理调用 |
| Hindsight 本地多语言模型加载与 API health | 两个固定 revision 的本地模型加载完成，API health 返回 200；不等同中文 retain/recall 质量通过 |
| 真实供应商/备用模型/计费 | 待用户在本地配置真实凭据、模型 ID 和费率后验证；未发起收费探测 |
| Docker 全新一键与重复一键完整部署 | 待完整凭据后执行；各阶段独立验证不等同 A17/A18 全通过 |
| 维护窗口加密备份与全新卷恢复 | 两轮隔离项目实测通过：五库还原、核心业务表哈希一致、恢复账号登录/目标读取、凭据/服务 key 和测试附件字节一致 |
| 恢复保护与备份结束恢复容器 | 错误 age 身份、覆盖原部署均拒绝；最终脚本直接恢复原容器，避免 Compose start 重新执行迁移依赖 |
| 升级失败恢复、还原后未完成 Workflow/记忆后台任务续跑 | 待完整场景验收；本次恢复不启动第二套记忆模型或业务 Worker |
| 公网 TLS、Linux amd64 主机、并发性能 | 待验收 |

原开发项目的核心 PostgreSQL 使用 `17.6-bookworm`，为保持并行工作流测试稳定，未重建其容器或卷。隔离恢复项目使用清单中的 `17.11-bookworm`，已真实启动并完成从 17.6 备份的五库恢复、角色读取、应用健康和恢复账号登录验证；这覆盖 17.11 的数据库与基本应用接入，尚不能替代 17.11 上全部 Worker/网关/记忆功能回归。

备份演练冻结在 `0001_initial`：1 个用户、1 个目标、1 个目标对话、0 条消息和 0 个业务附件。第一轮比较这些表及版本表的行内容哈希；第二轮只向隔离附件卷添加测试文件，验证加密备份与再次新卷恢复后的字节一致。全部真实供应商调用关闭，未启动额外 Hindsight 模型；演练创建的临时项目、卷和明文目录已清理，原项目卷未删除。此后新增的 `0002_general_chat` 数据未纳入该快照验收。

部署时采用的修正：Temporal admin-tools 不包含 psql，改用其已核验可重入的 `setup-schema`；显式指定 PostgreSQL 5432 端口；提供正式 server 需要的动态配置文件。初版服务不使用 Temporal dev-server/auto-setup。Hindsight 默认会在启动时验证 LLM，已显式设置 `HINDSIGHT_API_SKIP_LLM_VERIFICATION=true`，保证默认启动不额外发起供应商验证请求；发现该默认行为时本地没有供应商 Key，调用在网关认证阶段失败，没有供应商推理费用。

## 尚未完成的生产门槛

本次模型配置页面增量验证：正式应用迁移至 `0004_model_config`，Alembic check 无模型漂移；后端真实 PostgreSQL/Temporal 79 项、前端 45 项、配置脚本 7 项测试通过，Ruff、ESLint、TypeScript 和 Docker 构建通过。浏览器检查侧栏及对话入口、部署预置配置、复制表单和 390px 窄屏（无横向溢出）。

规划超时修复增量验证：后端真实 PostgreSQL/Temporal 85 项、前端 47 项、配置脚本 8 项通过。模型调用替身模拟持续超时、首次超时后成功和认证失败，分别验证总尝试次数 2/2/1、300 秒客户端参数、360 秒 Activity 超时、最多 2 次尝试、900 秒操作时限及只保存一份计划；页面测试覆盖明确超时标签和旧失败记录不被误标。实际故障规划及一条成功聊天的旧 Temporal 历史均在新代码下回放通过，未调用模型。真实供应商长规划能否在 300 秒内完成仍待用户重试验证。

后续聊天与规划等待时限调整：客户端 600 秒、网关至少 630 秒、模型 Activity 660 秒、失败最多重试 1 次、整次操作 1800 秒。后端真实 PostgreSQL/Temporal 91 项、前端 48 项、配置脚本 8 项通过；聊天和规划均覆盖持续超时、重试恢复及认证失败，真实父工作流历史确认子操作时限为 1800 秒。HTTP 传输替身通过实际 SDK/Pydantic AI 异常转换路径复现并验证包装后的超时分类，连接错误仍归类为普通失败；异常正文不进入持久化错误链。5 条生产历史（含聊天/目标父工作流）在新代码下回放通过，不产生模型调用。前次 300 秒验证记录保留为历史；当前真实供应商能否在 600 秒内完成仍待用户重试验证。

2026-10-21 对话执行修复：后端真实 PostgreSQL/Temporal 100 项、前端 52 项、配置脚本 8 项通过；6 条既有生产历史回放通过，未修改 Workflow 的命令顺序。新增回归验证对话操作的真实持久化、权限与版本拒绝、重复投递、最新目标/源对话传给规划者，以及候选计划确认前不创建任务。修复请求预算仍使用 15 分钟的遗漏，现为 30 分钟。对话操作和回执复用现有 JSON 字段，无新迁移；Docker 应用/Web 镜像构建通过。模型全部使用测试替身，本轮不构成供应商输出质量验收。

LiteLLM 1.106.0 实测通过 `/credentials` 新建命名凭据及复制部署凭据、`/model/new` 动态部署、`learning-configured` 访问组隔离、应用受限密钥推理和网关重启后恢复。推理只发送至 Docker 内临时 HTTP 模拟服务：检查解密后的凭据被正确使用、固定中文工具调用结果和 Token 用量；未调用真实供应商。临时模型与凭据已删除。当前实现保留历史部署版本，尚无自动清理；新配置在网关写成功而应用提交失败时可能留下未引用版本，重试会复用同一版本。新版完整加密备份恢复仍需单独演练。

- 三家所选模型的结构化输出、工具调用、多轮中文、超时和 fallback 行为。
- 显式费率、虚拟 key 预算和供应商账单的一致性；缓存及 reasoning 费用差异。
- 中文记忆 retain/recall、修正删除、恢复后台记忆任务与降级恢复。
- 完整 A01–A27 验收报告、Linux 目标机一键部署/重复部署/备份恢复、TLS 与性能。

官方来源：[Temporal Server 发行版](https://github.com/temporalio/temporal/releases/tag/v1.32.1)、[Hindsight 1.0.0](https://github.com/vectorize-io/hindsight/releases/tag/v1.0.0)、[Hindsight PGroonga 配方](https://github.com/vectorize-io/hindsight/blob/v1.0.0/docker/docker-compose/pgroonga/Dockerfile)、[LiteLLM 1.106.0](https://github.com/BerriAI/litellm/releases/tag/v1.106.0)。

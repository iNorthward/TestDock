# TestDock · 测试平台

供任意业务项目接入的独立测试与诊断平台。Core 提供案例目录、执行互斥、后台任务、结果与历史、通用 HTTP、契约校验、可选资源池和界面主题；业务认证、接口规则、数据、契约和案例由项目 Pack 提供。

## 快速体验

支持 macOS 与 Linux；Windows 当前不支持（文件锁和进程隔离依赖 POSIX）。需要 Python 3.9+；JavaScript 回归需要 Node.js 20+。离线 demo 无需数据库或真实账号。

```sh
test -f .env.local || cp .env.example .env.local
./start.sh start
./start.sh status
./stop.sh
```

默认打开 http://localhost:8801/，选择 DEMO-R01 运行本地 mock。`/insights?tab=history` 查看运行历史。配置在本项目 `.env.local`，示例在 `.env.example`。每个 Pack 使用独立数据目录、环境文件和端口。

## 接入项目

首次使用可先读 [面向维护者的上手说明](docs/onboarding/HUMAN_QUICKSTART.md)。

Agent 从 [快速接入入口](docs/onboarding/QUICKSTART.md) 与 [完整接入手册](docs/onboarding/AGENT_INTEGRATION.md) 开始，执行以下命令生成一个独立的离线项目骨架：

```sh
python3 scripts/create_project_pack.py --id sample_app --name '示例应用' --port 8802
python3 scripts/verify_project_pack.py --pack sample_app --mock-smoke
PLATFORM_ENV_FILE=.env.sample_app ./start.sh start
```

骨架先验收通过，再把项目自己的认证、响应信封、transport、契约和受影响案例接进 Pack。无需修改 Core。生成器不修改真实主业务项目、不复制凭据、不调用网络。

## 维护

```sh
python3 scripts/check_neutral_distribution.py
PLATFORM_ENV_FILE=/dev/null python3 scripts/check_platform_isolation.py --demo-smoke
PLATFORM_ENV_FILE=/dev/null python3 scripts/targeted_regression.py --group onboarding --group isolation
```

所有接入资产先明确归属；不执行全量业务案例。需要数据库能力时在隔离虚拟环境安装 requirements.txt（PyMySQL 与 SQLAlchemy 连接池均为必需依赖；`python3 scripts/check_env.py` 会按包名提示缺项），并安装业务 Pack 自己的依赖。用法、会话清理与池满语义见[数据库连接池指南](docs/guides/数据库连接池.md)。发行不携带仓库元数据；接入项目按自己的版本管理策略维护。

## 显式 HTTP 接入与端到端自检

除了离线骨架，还支持 --http-contract 从已审阅 GET 契约生成认证、HTTP 请求和原始 JSON 断言。详见完整手册第 17 节。可执行 `python3 scripts/accept_project_integration.py --report /tmp/integration.json` 对自有本机模拟业务服务完成双项目端到端验收。

案例结果：passed 为通过，failed 为失败，incomplete 为缺条件未完成；mock 与真实业务验证分开记录。项目历史、任务和资源目录独立。发行只包含平台源码、通用资产、说明和合成样例，不复制环境凭据、运行库或 Git 历史。

## 当前限制

Pack 目前只能从平台仓库内的 `packs/<id>` 装载，尚不支持外部目录直接装载及原子升级。默认时区固定为 `Asia/Shanghai`。Core 内置契约拉取和数据库巡检分别面向 Apifox 与 MySQL；其他来源须由 Pack 自行适配，目前没有统一的多供应商插件接口。

本项目按本机 macOS 定向验收，不配置远端 CI。用例文件变更会在执行空闲时自动重载；长批次期间目录仍可读取，界面显示待重载提示，正在执行的案例保持当前注册表；手动重载忙时返回 409，稍后重试。认证 token 的过期与跨线程登录复用由 Pack 的 TokenProvider 管理，Core 不假设 token 的有效期。

本地一键检查用显式定向组，例如 `bash scripts/check.sh --group execution --group execution-ui`。宽范围 `--changed` 若叠加成全部单测会返回 2，须拆成受影响的定向组；当前检查入口不承诺一次执行全部工作区改动的测试。

## 架构说明

[平台架构白皮书](docs/architecture/TEST_PLATFORM_ARCHITECTURE.md) 说明当前 Core / Pack / Adapter 边界、接入与执行协议、发行和验证范围。数据库访问统一经 `db()` 连接池，见[数据库连接池指南](docs/guides/数据库连接池.md)。

## 接入体检、升级和执行控制

[Agent 操作与交接协议](docs/onboarding/AGENT_OPERATIONS.md) 包含一键体检、证据任务清单、Adapter 配方、Pack 协议版本、执行队列与只读进程隔离、历史对比及真实验收恢复。

[中性发行与升级](docs/guides/中性发行与升级.md) 说明项目启动入口、平台发行来源和独立副本的升级预检。[长任务证据配方](docs/guides/长任务证据配方.md) 提供可运行的合成示例，覆盖取消、回复未知、失败和清理记录。

API 1.1 增加[请求准备与签名](docs/guides/请求准备与签名.md)、[能力采用清单](docs/guides/能力采用清单.md)和[工作台交互组件](docs/guides/工作台交互组件.md)。旧 Pack 与旧 transport 保持兼容；新生成 Pack 显式声明采用方式，验证事实初始为未执行。自定义工作台可从两个合成样例开始，继续沿用会话保护、任务与请求去重机制。

新建页面或扩展现有功能时，从工作台侧栏打开「UI 场景选型」（`/ui-scenes`）。按页面类型选场景，查看组件、Pack 职责和验收步骤。可以打开「入库批次复核」完整示范，操作合成数据并查看对应选型。 [统一 UI 场景规范](docs/guides/统一UI场景规范.md)

```sh
python3 scripts/project_doctor.py --pack demo_pack --mock-case DEMO-R01
python3 scripts/project_tasks.py --pack sample_app init --require-real
```

[本轮交付证据](docs/reports/six-improvements/进度.md) 分开记录离线、模拟服务、浏览器和真实只读验证，避免将 mock 成功当作真实业务已通过。

## 项目名称与版本管理

项目展示名为 TestDock，本地目录名为 testDock；仓库地址为 https://github.com/iNorthward/TestDock。Git 用于源码版本管理，不是平台启动或业务接入的前置依赖。

历史验收报告记录各次验证时的快照，其中“无 Git”证明平台可以脱离仓库元数据运行，不表示当前开发目录禁止初始化 Git。发行工具继续排除 Git 元数据、凭据和运行数据。

## 面板请求保护

面板仅监听回环地址。页面自动获取进程会话 token，API 请求须带 `X-TestDock-Token`；脚本先同源读取 `/api/session`。非同源 Origin/Referer、非法 Host 和非 JSON POST 会被拒绝，请求体上限 1 MiB。保护请求头不得重复；JSON 请求拒绝重复键、非有限数字及不完整正文。LIVE 同时要求启动环境显式设置 `PLATFORM_ALLOW_LIVE=1`，请求 `confirmLive` 等于案例 ID 或 suite；不指定 suite 时等于 `all`。原有 Pack 的逐例写入授权仍然有效。服务重启后刷新页面获取新 token。

执行请求使用 `requestId` 去重。页面会话存储仅保存参数摘要、请求标识与已获知的任务标识，不保存明文凭据；网络中断或结果未知时，即使超过 24 小时，相同参数的手动重试仍沿用原标识，确认终态后才释放。刷新可恢复当前页面会话中的记录，不会自动重发写操作。关闭标签页或窗口、清除会话存储后，这些标识无法保证恢复；会话存储或 SHA-256 不可用时仅保存在页面内存中。遇到这些情况须先核验业务状态，再决定是否使用新标识执行。

运行 `bash scripts/check.sh` 执行静态门禁及变更影响的离线回归；真实 HTTP 安全测试和双项目模拟验收独立执行。选测组叠加覆盖全部 Python 测试模块时，门禁会拒绝执行，应缩小 `--group` / `--file` 范围，不能把自动选择当作全量执行授权。发行 SHA-256 收据只由 `export_platform.py` 在导出目录生成，源码目录不维护过期收据。资源池可用 `password_ref: "env:MY_PASSWORD"`，默认密码可用 `settings.default_password_ref`；引用未配置时明确报未完成，不静默回退明文密码。

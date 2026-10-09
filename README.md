# TestDock · 测试平台

供任意业务项目接入的独立测试与诊断平台。Core 提供案例目录、执行互斥、后台任务、结果与历史、通用 HTTP、契约校验、可选资源池和界面主题；业务认证、接口规则、数据、契约和案例由项目 Pack 提供。

## 快速体验

需要 Python 3.9+；JavaScript 回归需要 Node.js 18+。离线 demo 无需数据库或真实账号。

```sh
test -f .env.local || cp .env.example .env.local
./start.sh start
./start.sh status
./stop.sh
```

默认打开 http://localhost:8801/，选择 DEMO-R01 运行本地 mock。`/insights?tab=history` 查看运行历史。配置在本项目 `.env.local`，示例在 `.env.example`。每个 Pack 使用独立数据目录、环境文件和端口。

## 接入项目

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

所有接入资产先明确归属；不执行全量业务案例。需要数据库能力时在隔离虚拟环境安装 requirements.txt，并安装业务 Pack 自己的依赖。发行不携带仓库元数据；接入项目按自己的版本管理策略维护。

## 显式 HTTP 接入与端到端自检

除了离线骨架，还支持 --http-contract 从已审阅 GET 契约生成认证、HTTP 请求和原始 JSON 断言。详见完整手册第 17 节。可执行 `python3 scripts/accept_project_integration.py --report /tmp/integration.json` 对自有本机模拟业务服务完成双项目端到端验收。

案例结果：passed 为通过，failed 为失败，incomplete 为缺条件未完成；mock 与真实业务验证分开记录。项目历史、任务和资源目录独立。发行只包含平台源码、通用资产、说明和合成样例，不复制环境凭据、运行库或 Git 历史。

## 架构说明

[平台架构白皮书](docs/architecture/TEST_PLATFORM_ARCHITECTURE.md) 说明当前 Core / Pack / Adapter 边界、接入与执行协议、发行和验证范围。

## 接入体检、升级和执行控制

[Agent 操作与交接协议](docs/onboarding/AGENT_OPERATIONS.md) 包含一键体检、证据任务清单、Adapter 配方、Pack 协议版本、执行队列与只读进程隔离、历史对比及真实验收恢复。

```sh
python3 scripts/project_doctor.py --pack demo_pack --mock-case DEMO-R01
python3 scripts/project_tasks.py --pack sample_app init --require-real
```

[本轮交付证据](docs/reports/six-improvements/进度.md) 分开记录离线、模拟服务、浏览器和真实只读验证，避免将 mock 成功当作真实业务已通过。

## 项目名称与版本管理

项目展示名为 TestDock，本地目录名为 testDock；仓库地址为 https://github.com/iNorthward/TestDock。Git 用于源码版本管理，不是平台启动或业务接入的前置依赖。

历史验收报告记录各次验证时的快照，其中“无 Git”证明平台可以脱离仓库元数据运行，不表示当前开发目录禁止初始化 Git。发行工具继续排除 Git 元数据、凭据和运行数据。

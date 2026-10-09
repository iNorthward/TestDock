# Agent 快速接入入口

目标：不修改 Core，在所属 Pack 完成业务接入。所有命令在平台根目录运行。先读 AGENTS.md 与完整接入手册；真实主业务仓库保持独立。

## 路线 A：先体验离线平台

```sh
python3 scripts/create_project_pack.py --id my_app --name '我的应用' --port 8802
python3 scripts/verify_project_pack.py --pack my_app --mock-smoke
PLATFORM_PACKS=my_app PLATFORM_ENV_FILE=/dev/null python3 scripts/targeted_regression.py --group my_app.adapter
PLATFORM_ENV_FILE=.env.my_app ./start.sh start
```

打开 http://localhost:8802，运行 DEMO-R01。目录、进程、数据、测试已独立，接下来替换本 Pack Adapter 与案例。

## 路线 B：已经有只读 HTTP 契约

用项目证据编辑 examples/http_pack_template/contract.example.json 的副本：path、状态、响应类型、header 环境键以及合成 mock。不可填入真实密码或 token。

```sh
python3 scripts/create_project_pack.py --id my_service --name '我的服务' --port 8802 --http-contract /path/to/my-contract.json
python3 scripts/verify_project_pack.py --pack my_service --mock-smoke --case-id HTTP-MOCK-R01
PLATFORM_PACKS=my_service PLATFORM_ENV_FILE=/dev/null python3 scripts/targeted_regression.py --group my_service.adapter
```

在 .env.my_service 补服务地址和认证值，然后启动该项目：

```sh
PLATFORM_ENV_FILE=.env.my_service ./start.sh start
PLATFORM_ENV_FILE=.env.my_service ./start.sh status
# 使用结束后只停止本项目
PLATFORM_ENV_FILE=.env.my_service ./start.sh stop
```

打开 http://localhost:8802，在 UI 单独运行 HTTP-R01。缺配置显示 incomplete，绝不冒充通过。需要复杂认证或写流程时修改所属 Pack，详细协议见 [完整手册](AGENT_INTEGRATION.md)。

## 维护与交付

```sh
python3 scripts/check_neutral_distribution.py
python3 scripts/check_platform_isolation.py
python3 scripts/accept_project_integration.py --report /tmp/integration.json
```

不要启用不需要的资源池、数据库或日志；这些能力按项目声明。不要改 Core 适配字段，不运行全量业务案例，不借用其他项目配置，不把 mock 验收说成真实业务接口通过。

## 接入后的运行与升级

一键体检、证据任务清单、Adapter 配方、兼容门禁、执行队列及历史对比见 [Agent 操作手册](AGENT_OPERATIONS.md)。

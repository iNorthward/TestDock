# 面向 Agent 的完整项目接入手册

## 1. 目标与完成条件

你是接入者，职责是把一个新的主业务项目接入此独立测试平台。先识别真实契约，再写 Pack；不得修改 Core 来适应单个项目，不得读取其他项目账号，不得把 mock 通过说成业务已验证。平台目录名与主业务目录名无须相同。本文命令均在平台根目录运行。

接入完成必须有：唯一 Pack、独立环境/端口/数据、可加载案例目录、注册的项目 Adapter、明确的契约来源、逐项运行状态、定向回归证据和真实业务未验证清单。只做离线接入时可以完成机制验收，但真实认证、DB、写接口必须标注未执行。

## 2. 接入前收集证据

向业务负责人确认并记录：项目 ID/展示名、源码只读路径、测试服务地址与环境、授权接口范围、认证流程、header/tenant要求、响应信封、原始 JSON ID/金额/日期线型、OAS 或接口证据、数据库只读范围（可选）、SAFE 实际副作用、写案例授权/样本/清理、凭据来源、端口和数据归属。缺失信息不猜，记录 incomplete。主业务源码可只读探查；写权限须单独确认。

先读 AGENTS.md、平台与项目隔离规范、平台序列化与反序列化规范、平台执行隔离与任务规范。不要加载其他项目 `.env`。不要执行全量案例。Pack 为受信任的 Python 代码，当前隔离机制不是运行恶意代码的沙箱。

## 3. 首次体验与离线骨架

Python 3.9+；Node 18+ 用于 JavaScript 回归。demo 不需 pip 安装数据库驱动。实际 DB 能力安装 requirements.txt；业务依赖放 packs/<id>/requirements.txt 并在隔离虚拟环境安装。

```sh
./start.sh start
python3 scripts/verify_project_pack.py --pack demo_pack --mock-smoke
python3 scripts/create_project_pack.py --id sample_app --name '示例应用' --port 8802
python3 scripts/verify_project_pack.py --pack sample_app --mock-smoke
PLATFORM_ENV_FILE=.env.sample_app ./start.sh start
PLATFORM_ENV_FILE=.env.sample_app ./start.sh status
```

生成器拒绝覆盖现有 Pack 或环境文件，生成权限 0600 的 `.env.sample_app`。骨架同时生成 tests/test_adapter.py 与 regression.json；可直接运行 PLATFORM_PACKS=sample_app PLATFORM_ENV_FILE=/dev/null python3 scripts/targeted_regression.py --group sample_app.adapter。骨架有离线 TokenProvider、请求头、响应信封、MockTransport 和一条 DEMO-R01。同一 case ID 可以在不同进程的项目中使用；一个项目内部必须唯一。生成器不修改 default demo、不停已有服务、不连接真实业务。

验证器使用干净子进程、/dev/null 环境文件和临时数据，禁止 socket；`--mock-smoke` 只能用于可完全离线的 SAFE 案例。真实案例接入后使用普通 `--pack` 验证装载；若项目 bootstrap 会联网，须先移除 bootstrap 的副作用，将调用放入 runner 或 Adapter 按需方法。

## 4. 文件归属

```text
packs/sample_app/
  pack.json
  __init__.py          # install() 注册 Adapter，初始化不联网
  transport.py        # 初始 mock，真实项目可替换
  cases/__init__.py
  cases/readonly_cases.py
  helpers/            # 项目业务算法
  contracts/          # 项目 OAS / wire / serializer 证据
  requirements.txt    # 可选项目依赖
  regression.json     # 可选定向映射
.env.sample_app       # 忽略的运行配置，无需提交
 data/sample_app/     # 实际池、历史，忽略
 artifacts/sample_app/# 执行证据，忽略
```

全部业务模块以 `packs.sample_app.*` 完整路径导入；不要把业务 helper 放 common。无初始化需求的父命名空间保持空或 docstring。新 Core 文件才登记 platform-core-files；Pack 文件不登记为 Core。

## 5. Manifest 合同

最小已运行骨架由生成器给出。以下字段按需扩展，不要复制其他项目的默认值：

| 字段 | 用途与约束 |
| --- | --- |
| id / display_name / default | id 与目录一致，合法标识符；未选择项目时只有一个 default |
| bootstrap | module:function，通常 packs.sample_app:install，先校验源码归属 |
| case_dirs | 仓内相对路径，归本项目，包含 *_cases.py |
| navigation / module_order_hint | 导航父节点与案例 suite.parent 一致；排序不改变归属 |
| config_defaults / config_aliases | 非秘密默认值与项目兼容键；不能覆盖项目 selector |
| identity_pool_file / identity_requirements / resource_pool | 可选资源，默认关闭；详见资源章节 |
| panel_html | 可选项目自有 HTML；默认共享完整工作台 |
| panel_services / optional_panel_services | 可选项目模块；只允许明确缺失，模块内部异常不隐藏 |
| shared_core_services | 显式允许的通用 alias：run_history、identity_pool、platform_config、timezone_config |
| panel_extension / catalog_extension | 本 Pack 工厂，提供项目 UI 路由或执行上下文 |
| cli_tools | alias 到本 Pack 模块，通用入口只调用当前项目映射 |
| optional_environment_checks | 项目可选配置检查，只展示键的名称，避免输出值 |
| execution_contract_sources | 证据标签到配置键映射，执行来源核验 |
| regression_manifest | 本 Pack 内定向回归清单 |
| diagnostic_capabilities | 可选 UI 入口：contract、database、error_log，默认空列表 |
| catalog_policy | 可选 group_tokens 与 require_readonly_baseline，不强制业务命名 |
| case_doc_exceptions | 明确 case ID 到保留差异原因；不是跳过错误的通用通道 |
| adapter_files / compatibility_files | 显式文件归属；新接入优先将实现放 Pack 内，不加旧裸导入入口 |

路径必须真实存在、不得跨 Pack 或利用 symlink 逃出归属范围。registry 会先验证，再 bootstrap。失败后修复并重启。

## 6. 配置与启动

统一使用 PLATFORM_*。环境文件是纯文本，已导出的进程变量优先，/dev/null 只禁用文件、不清空外部环境。建议从干净 shell 启动，避免继承其他环境的服务地址或凭据。

```dotenv
PLATFORM_PACKS=sample_app
PLATFORM_PANEL_PORT=8802
PLATFORM_PACK_DATA_DIR=data/sample_app
PLATFORM_ARTIFACT_ROOT=artifacts/sample_app
TZ=Asia/Shanghai
```

业务键在自己的 Adapter 中读取；`.env.example` 只提供平台启动键。HTTP 会话优先通过 ApiSession(base=...) 显式设置地址，契约生成器使用 recipe 的 base_env。通用连接辅助配置实现在 `test-platform/common/env_config.py`：PLATFORM_API_BASE、PLATFORM_AUTH_BASE、PLATFORM_NODE_API_BASE，均无默认业务地址。只有使用 cli_session/mgr_session 便利函数时才配置其 tenant、category、role。项目可使用自己的业务环境键并显式映射，不需要采用 cli/mgr 语义。不要在 manifest 写真实 token/密码/DSN。

```sh
PLATFORM_ENV_FILE=.env.sample_app ./start.sh restart
PLATFORM_ENV_FILE=.env.sample_app ./start.sh stop
```

每个服务以 Pack、port、仓库路径识别。不能用 killall/python 或认领其他仓库同端口进程。端口冲突改本项目端口；首次启动不迁移真实历史库。

## 7. Adapter 实现顺序

先看 examples/demo_pack/__init__.py 与 transport.py，它们是完整可运行协议例子，不是真实认证方案。项目 install() 只注册，不登录、不查 DB。

```python
from api_client import register_token_provider, register_request_auth
from response_envelope import register_response_envelope

def install():
    register_token_provider(ProjectTokens())
    register_request_auth(ProjectHeaders())
    register_response_envelope(ProjectEnvelope())
```

TokenProvider 实现 get_token(username, tenant_id)。ProjectHeaders 实现 build_headers(token=None, **options) 和 validate_headers(headers)，负责全部项目 header。ResponseEnvelope 实现 is_ok、is_reject、business_code、message、data、normalize_code、validate_envelope；不要猜 success/code/data。精度敏感项目可以注册 ResponseDecoder.decode_json(text, method, url)，先明确解码后的原始数字策略。

案例构造 ApiSession(tenant=..., base=..., default_user=..., role=..., transport=...)；base 明确，role 只是会话上下文，项目不必使用默认 cli/mgr helpers。mock transport.request(method,url,headers,data,timeout) 返回 (http_status, decoded_body)，真实网络默认 transport 由统一 HTTP 入口提供。认证缓存、刷新和凭据访问放项目 TokenProvider。禁止在 Core 写 OAuth、签名、业务路径或默认用户。

## 8. 案例编写与结果

骨架 readonly_cases.py 有 SUITES、CATALOG、CAT、RUNNERS、RUNNER_API、exec_case。按自身项目改 case ID、suite、group、desc、mode、expect、api、expected；runner 映射须存在，ID 唯一，suite.parent 必须对应导航。先保留离线验收案例，再新增一条真实 SAFE 案例，不要直接批量生成业务写操作。

runner 先验证 HTTP 状态、信封、字段是否存在、原始 JSON 类型、null/required、值域，再转换做对账。bool 不是整数；字符串 ID 不转 Number；金额不先 float。业务 wire 策略由 Pack 注册，未确认就 incomplete。

缺配置、未执行或跳过使用 ok=None；ok=False 表示真实失败，会优先归类为 failed。不要把 status=incomplete 与 ok=False 混用。

返回例子：
```python
return {'ok': True, 'status': 'passed',
        'subs': [{'label': 'health', 'ok': True, 'detail': '原始结构与预期一致'}]}
```

缺少身份/契约/授权返回 incomplete 并说明缺项，不填伪造默认值；失败返回 failed。status 与 ok 不矛盾。任何写案例必须独立控制开关、清理、恢复失败断言和资源互斥；没有当次授权不要运行。

## 9. 可选资源与可选诊断

不需要资源池时保持 resource_pool.enabled=false；UI 不显示该入口。需要设备、服务账号、租户样本时，分类 id/label/storage/display_fields 由 Pack 自己定义。storage 支持 identities/scenarios/fixtures；这是通用存储结构，不是必须启用的业务能力。参考平台资源能力接入文档。

默认登录类别和角色分别通过 PLATFORM_DEFAULT_CLI_CATEGORY/ROLE、PLATFORM_DEFAULT_MGR_CATEGORY/ROLE 声明，仅使用对应 helpers 时需要。关系、登记限制、密码策略、别名和夹具工厂全部显式注册；不按 category 名推断语义。池和凭据忽略提交，UI 脱敏。

Apifox、数据库、错误日志和 wire 同步等工具在缺少当前项目输入或 cli_tools 映射时返回 incomplete。初始接入可忽略它们，不需伪造数据库或 SSH。逐个启用：提供证据 → 配置本 Pack input_keys/cli_tools → 给出定向测试 → 验证缺项与失败分支 → 执行授权范围。工具不得自动借用其他项目 MCP token 或路径。

## 10. 扩展与执行上下文

panel_extension 工厂接收 {pack_id,pack_root,services,core}，返回 dispatch(method,handler,path,query,body)->bool；只有处理自己的路由才返回 True，可实现 startup 和 execution_options(data)。响应须脱敏，执行任务使用已有 job/互斥机制，避免绕过平台入口。

catalog_extension 可提供 before_suite、before_case、after_reload、enrich_catalog、catalog_section，以及 case_context(case,module,execution_options)。项目上下文用 context manager/ContextVar，在成功和异常都恢复。示例默认不启用扩展，接入初期不必实现它们。

## 11. 验收命令与输出

```sh
python3 scripts/check_neutral_distribution.py
python3 scripts/check_platform_isolation.py --json
python3 scripts/verify_project_pack.py --pack sample_app --mock-smoke
PLATFORM_ENV_FILE=/dev/null python3 scripts/targeted_regression.py --group onboarding --group isolation --group transport --group identity --group execution-ui --group distribution
```

新增业务回归放 Pack 的 regression_manifest：version=1、groups，每组声明仓内 sources 模式、Python unittest 模块和可选 JavaScript 文件。测试使用临时合成数据，不能连真实 DB；运行只选择改动组。真实 SAFE 验收须另获测试环境授权，并记录 case ID、环境、数据副作用与结果。不要把离线 unittest 当成接口通过。

UI 验收：所选 Pack 名称正确、case 列表正确、未声明资源入口不显示、搜索可用、单条 mock 执行 passed、历史归本项目、主题/手机布局可用、项目特有端点无映射时明确缺项。历史保存失败和 runner 失败分别报告。

平台运行和接入不需要 Git。没有 `.git` 时使用显式 `--group` 或 `--file` 定向回归；`--changed` / `--staged` 仅供自行启用 Git 的项目使用，不要为运行平台自动初始化仓库。

## 12. 排障与回退

| 症状 | 处理 |
| --- | --- |
| 多个 default / 多项目选择 | 显式 PLATFORM_PACKS 指定一个，重启 |
| 未注册认证或信封 | 检查 bootstrap 路径、安装顺序、方法完整性 |
| module ownership / symlink 错误 | 移回本 Pack，检查父包初始化，不放宽 Core 校验 |
| case ID / suite 重复 | 本项目内唯一，检查 manifest case_dirs 没重复 |
| pool 缺失或 category 不可登记 | 确认当前 Pack、resource_pool 分类和实际数据根，不用其他项目池 |
| tools incomplete | 补本 Pack 明确输入或保留未启用状态 |
| 端口被占用 | 更换本项目端口，勿停止未知进程 |
| mock smoke 试图联网 | 移除 bootstrap I/O，提供 MockTransport |
| 历史保存失败 | 核实本项目目录权限，勿重复执行写 runner 补历史 |
| JSON 类型不匹配 | 检查原始响应与项目契约，不通过强转放宽断言 |

回退只停止自己项目的服务，恢复本 Pack/config 备份，保留数据与历史，不 reset 主业务项目，不覆盖其他 Pack。保持原 default demo 可用于诊断平台机制。

## 13. Agent 交付模板

报告整体状态；需关注列出真实业务未执行项、缺失证据和待授权操作；已完成列 manifest/Adapter/案例与路径；验证区分通过/失败/未执行，给出精确命令与报告；数据变更列新建文件、mock 历史、真实写入和清理结果。报告中不得输出密码、token 或完整 DSN。未经用户要求不提交推送、不部署真实环境。

本手册与生成器、验收工具和回归测试共同维护。接入成功后，更新 Pack 自身 README，记录实际能力、契约来源、运行限制和下一条可执行命令，让下一位 Agent 无须猜测。

## 14. 逐条执行与写开关

单条案例使用界面或平台 API，不用 run_all 验收所有业务：

```sh
curl -s http://localhost:8802/api/platform
curl -s http://localhost:8802/api/catalog
curl -s -H 'Content-Type: application/json' -d '{"id":"DEMO-R01","requestId":"sample-app-offline-001"}' http://localhost:8802/api/run-case
```

同一个 requestId 用于同一次操作重试；换 payload 不复用该 ID。案例重试任务按实际返回的 executionId 查询 /api/execution/job?id=<executionId>，取消通过 POST /api/execution/cancel，body 为 {"id":"<id>"}。不把 unknown/incomplete 当成功。上述 API 只演示离线 mock。

需要真实写入时，逐例设置 PLATFORM_ENABLE_WRITE_CASE=<精确 case ID>；无资源标识操作还受 PLATFORM_ENABLE_UNSCOPED_WRITE_CASE=<精确 case ID> 控制。开关不代表业务授权，也不替代 Pack 钩子、恢复和范围限制。默认不设置；必须由负责人明确授权后才使用。

Pack 回归清单示例（测试模块须能按完整包名导入）：

```json
{"version":1,"groups":{"sample_app.adapter":{"sources":["packs/sample_app/__init__.py","packs/sample_app/helpers/*.py"],"python":["packs.sample_app.tests.test_adapter"],"javascript":[]}}}
```

在 pack.json 配置 regression_manifest="packs/sample_app/regression.json"，用 PLATFORM_PACKS=sample_app PLATFORM_ENV_FILE=/dev/null python3 scripts/targeted_regression.py --group sample_app.adapter 运行。未登记来源、错误组名或失败都会阻断；不得通过跳过测试绕开。

## 15. 主业务仓库保持独立

业务应用仍在原仓库开发、构建和部署，不需搬入测试平台。接入 Pack 只保存测试案例、Adapter 与已确认的接口/序列化证据。源码路径只作为只读证据输入；构建、格式化、数据迁移与提交主业务仓库须另行授权。不要对主业务仓库创建共享 Git worktree 来运行接入验收。

推荐把已经审阅的测试契约放 packs/<id>/contracts，使发行可复现；仓外契约路径可显式配置，但报告必须注明外部输入可能漂移。PLATFORM_BIZ_KNOWLEDGE_ROOT 指知识目录根，PLATFORM_PACK_DATA_DIR 指本项目池/历史，PLATFORM_ARTIFACT_ROOT 指证据根。所有子路径不得逃出配置根。业务 API、认证、数据库与私密键只由本 Pack 读取当前配置。

现有项目如果配置使用别名，迁移时在 config_aliases 显式声明本项目别名；新接入统一采用 PLATFORM_*，不依赖其他项目遗留环境键。

## 16. 可选诊断入口

新项目默认只显示环境预检、结构体检、历史与稳定性；不强制启用接口平台、数据库或日志服务。需要相应入口时在 pack.json 的 diagnostic_capabilities 数组显式声明 contract、database、error_log，例如 {"diagnostic_capabilities":["contract"]}。名称、类型和重复值在 bootstrap 前校验。声明只表示项目启用该 UI 能力，实际工具仍须提供对应输入与权限，缺少输入返回 incomplete。

身份池只有 id/category/role/roles/status/stages 等通用模型约束；扩展字段保持原始 JSON 值，业务线型与关联由项目验证。旧业务的阶段投影、层级身份合并和默认用户/管理员猜测不属于 Core，新项目如需工作流请在 Pack helpers 中实现。

## 17. 从显式契约快速接入 HTTP 服务

默认生成器提供离线骨架。已经掌握 GET 路径、HTTP 状态、认证 header 与 JSON 字段证据时，用 --http-contract 直接生成能调用真实 HTTP 的 Pack，避免从零写会话与断言。示例文件为 examples/http_pack_template/contract.example.json，只有合成数据。

```sh
python3 scripts/create_project_pack.py --id service_app --name '服务应用' --port 8802 --http-contract /path/to/reviewed-contract.json
python3 scripts/verify_project_pack.py --pack service_app --mock-smoke --case-id HTTP-MOCK-R01
PLATFORM_PACKS=service_app PLATFORM_ENV_FILE=/dev/null python3 scripts/targeted_regression.py --group service_app.adapter
# 在忽略的 .env.service_app 配置自己的服务地址与认证值后启动
PLATFORM_ENV_FILE=.env.service_app ./start.sh start
```

生成器还会为所属 Pack 写入 README，包含正确 mock case ID、定向测试、配置键、独立启动/停止和后续实现位置；接入者可以直接照做。业务 base_env 和 header env 禁止复用 PLATFORM_PACKS、PLATFORM_ENV_FILE、PLATFORM_PANEL_PORT、PLATFORM_PACK_DATA_DIR、PLATFORM_ARTIFACT_ROOT 或 TZ，以免覆盖平台启动配置。

契约必须是严格 JSON，禁止重复键和非有限数值。method 为 GET；path 为当前服务的绝对路径，不能使用另一个主机的 URL。base_env 指服务地址环境键；headers 只声明 env/prefix，不接收真实凭据字面量；expected_status 为明确 HTTP 状态；checks 显式声明字段路径、JSON 类型和可选 equals；mock_response 是人工合成的协议样本。生成器不根据字段名字选择 ID、金额、用户或框架策略。

模板生成 HTTP-MOCK-R01（离线）与 HTTP-R01（真实 HTTP）。没有配置服务地址或认证时，真实案例返回 incomplete，且不发请求；认证拒绝、HTTP 错误、字段缺失、原始类型不符都失败。结果不输出认证值、原始敏感响应或全量环境。

模板适用于明确 GET 与 env-header 认证。需要 OAuth 登录、签名、刷新、复杂信封、写流程或精度解码时，在所属 Pack 的 integration.py / Adapter 实现，保持 Core 不变。HTTP 模板不是自动证明 SAFE 的工具：仍需负责人确认此 GET 无业务写副作用、允许调用的环境和频率。

## 18. 可重复端到端自检

```sh
python3 scripts/accept_project_integration.py --report /tmp/project-integration-report.json
```

该工具导出干净平台，在临时目录生成两个新 Pack，启动自有 loopback 模拟业务与面板，验证真实 HTTP transport、认证、错误类型、缺配置、401、请求去重、案例和历史隔离，再停止自有进程并清理临时数据。它不接触真实业务服务、主业务仓库、账号或数据库。运行它需要允许本机回环端口；它独立于禁止网络的定向 unittest。真实环境验收仍需另行授权。

交付验收矩阵见 [验收矩阵](../reports/mature-foundation/验收矩阵.md)。最新的绿色回归记录不替代新业务自身的契约证据。

目录分组不强制采用特定业务端、中文标签或 R/F/D/E 命名。需要分组标签约束时在 Pack 的 catalog_policy.group_tokens 声明；需要 R 只读基线建议时显式声明 require_readonly_baseline=true。缺省关闭这些项目规范，Core 仍校验唯一 ID、基本字段、mode、runner 与 suite 归属。

## 升级、任务交接与受控执行

完整操作协议见 [AGENT_OPERATIONS.md](AGENT_OPERATIONS.md)。`pack.json` 可声明以下平台能力字段：

| 字段 | 类型与用途 |
| --- | --- |
| platform_api | 对象：min、max_exclusive 为 major.minor；features 为唯一能力字符串数组。不兼容时在 bootstrap 前拒绝装载。 |
| required_environment | 大写环境键名数组；只登记本项目必需配置名称，值留在私有环境文件。体检不调用真实业务。 |
| execution_policy.isolated_case_ids | 已确认无业务写副作用的 SAFE case ID 数组。禁止包含写案例或未声明资源范围的写流程。 |
| execution_policy.case_timeout_seconds | 有限正数，不超过 3600 秒。隔离只读进程超过时限会被终止；默认 30 秒。 |

`project_doctor.py`、`project_tasks.py` 的明确命令及证据格式见操作协议。任务需要保留指定 mock，并用本 Pack 定向测试证据交接；真实验收独立记录。不要因 GET 方法或名称包含 readonly 就默认允许强制终止，仍须阅读业务实现。

从生成骨架改写案例时保留全部目录接口：SUITES、CATALOG、CAT、RUNNERS、RUNNER_API、exec_case。CAT 必须由当前 CATALOG 建立，不能只修改 CATALOG 而留下旧映射。先运行体检，装载失败时先修复本 Pack，禁止跳过门禁启动真实验收。

浏览器以 background=true 提交，用执行 ID 查询终态；查询失败仍保留 ID，刷新后恢复查询。取消按钮只请求取消；实际取消完成前不宣布已停止。显式隔离的只读子进程支持强制停止，其余案例依赖协作检查点，不能保证终止任意阻塞第三方代码。

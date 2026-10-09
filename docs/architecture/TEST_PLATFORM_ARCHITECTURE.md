# TestDock 独立测试平台架构白皮书

> 文档版本：v2.0 · 核对日期：2026-10-09
> 适用范围：当前独立平台源码与已交付验收证据。文档版本与平台 API 版本独立；当前 API 为 1.0。

## 1. 定位与验收目标

这是供不同主业务项目接入的测试平台基座。Core 提供通用装载、请求、执行、任务、资源互斥、结果和历史；业务项目通过自己的 Pack 与 Adapter 接入。平台默认提供纯离线 demo，不需要真实账号、数据库或业务网络。

接入目标是新业务项目无需修改 Core，即可生成骨架、明确配置、注册自身协议、完成 mock 与定向回归、运行已授权的真实案例，并交接可核验的证据。具体业务认证、角色、字段、成功与拒绝码、数据库口径及恢复策略均由业务所有者确认。

平台无需 Git 才能生成、启动或验收 Pack。独立发行不包含仓库元数据、私有环境、账号池、历史库或真实业务资产。没有 Git 时执行证据的 codeRevision 可为空，代码指纹仍有效。

## 2. 结构与归属

| 层 | 源码与资源位置 | 责任 |
| --- | --- | --- |
| Core | resources/platform-core-files.json 登记的 Python、test-platform 通用运行时、通用 scripts、platform_config.py、timezone_config.py | 协议、装载、执行、结果、历史与平台工具 |
| 通用组件 | test-platform/common | HTTP、token/header/decoder/envelope 注册点、类型校验等已登记组件 |
| Pack | packs/<id> | 案例、helpers、Adapter、契约、Schema、业务工具、环境默认值及资源分类 |
| 通用界面 | test-platform/panel.html、insights.html、assets | 工作台、诊断、历史和共享主题 |
| 离线与接入样例 | examples/demo_pack、http_pack_template、adapter_recipes | 合成 transport、显式 HTTP 契约及按需复制到 Pack 的配方 |

Core 不直接导入业务实现、不提供业务账号或专属字段回退。所有者以源码归属清单、manifest 和路径校验为准；放入 common 目录不能自动变成通用能力。

## 3. 项目选择与装载顺序

```mermaid
flowchart LR
    Env[独立环境文件] --> Pack[单个 Pack manifest]
    Pack --> Version[归属与协议兼容校验]
    Version --> Bootstrap[bootstrap 注册 Adapter]
    Bootstrap --> Catalog[案例索引]
    Catalog --> Admission[执行容量与资源门禁]
    Admission --> Runner[当前 Pack runner]
    Runner --> Result[案例四态与历史证据]
```

PLATFORM_ENV_FILE 选择环境文件，PLATFORM_PACKS 选择单个 Pack。每个进程只装载一个项目；不同项目用独立环境文件、端口、数据根和证据目录。默认离线面板端口为 8801，生成新项目时显式指定其端口。

案例模块必须导出 SUITES、CATALOG、CAT、RUNNERS、RUNNER_API、exec_case。CAT 由当前 CATALOG 构建；唯一 ID、suite 归属、expect runner、mode 和 API 映射须一致。manifest 或注册机制变更后重启，不能把案例热重载当完整运行时升级。

## 4. manifest 与扩展协议

| 字段或能力 | 职责与约束 |
| --- | --- |
| case_dirs / bootstrap | 声明所属案例目录与启动注册函数，先校验归属再装载 |
| platform_api | min、max_exclusive 使用 major.minor；features 为唯一能力名称数组，不兼容时在 bootstrap 前拒绝 |
| required_environment | 只保存必需配置键名，配置值留在所属项目私有环境文件 |
| config_defaults / environment_aliases | 项目默认值及显式兼容；Core 不猜业务变量 |
| resource_pool | enabled、标题、说明、分类、显示字段及维护策略；未启用时隐藏资源入口 |
| panel_services / panel_extension | 当前 Pack 服务与可选 dispatch 路由，startup、execution_options 等 hook |
| catalog_extension | 目录增强、分区、执行前 hook、case_context 和 reload 清理 |
| cli_tools | 兼容命令对应的所属实现；缺能力报告 incomplete |
| execution_contract_sources | 契约名称到项目路径配置键，用于执行版本指纹 |
| execution_policy | 明确允许隔离的只读案例 ID 与执行时限，不是写操作回滚策略 |

当前平台 API 1.0 声明 request_id、resource_pool、diagnostics、response_decoder、bounded_queue、history_compare、isolated_readonly。GET /api/platform 返回版本、能力、当前 Pack 与兼容结果。未声明 platform_api 的旧 Pack 暂按现有协议兼容，体检提示补显式范围；兼容并不证明业务正确。

资源池没有必选用户、代理、管理用户、场景或夹具模型。接入者可以定义适合自身项目的分类，也可以完全关闭资源能力。分类标签不能替代业务角色授权，维护建议不能代替真实账号有效性测试。

## 5. 请求、认证与响应契约

ApiSession 使用当前项目的 TokenProvider、RequestAuthProvider、HttpTransport、可选 ResponseDecoder；ResponseEnvelope 提供成功、拒绝、业务码、消息、payload 与信封校验。注册入口位于通用组件，具体 OAuth、签名、请求头、租户、字段与拒绝码实现放在 Pack。

配方覆盖显式登录回调、token 缓存、过期与并发复用、配置化响应信封、分页路径和无损解码。复制到所属 Pack helpers 后按完整包路径导入；不把业务实现追加进 Core。登录失败不回退过期 token，401 不自动重放业务写操作。

先验证原始 JSON 类型，再做数值转换或业务计算。请求、响应 wire 和 DB 分别核实；bool 不能冒充 integer，大整数和金额按项目策略解码与校验。HTTP 200 不证明业务成功，权限拒绝也可能通过自定义业务码表达；须依据明确的业务契约断言，不在 Core 固定码或默认信封。

协议细节见[序列化与反序列化规范](平台序列化与反序列化规范.md)与[Adapter 配方](../../examples/adapter_recipes/README.md)。

## 6. 接入与 Agent 证据闭环

1. 从完整接入手册确认范围、角色、接口、字段与只读证据。
2. create_project_pack.py 生成所属骨架、独立环境模板和测试清单；已确认 GET 契约可使用 --http-contract。
3. project_doctor.py 核对 manifest、版本、环境、目录、端口、必需配置、Adapter 和目录；指定 --mock-case 才执行该离线 smoke。
4. targeted_regression.py 运行本 Pack 的受影响离线组，使用清理后的环境与临时数据。
5. project_tasks.py 以 scope、environment、adapter、mock、regression、real_readonly 记录证据路径和 SHA-256。
6. 独立实例逐例真实验收，生成脱敏报告，再核对恢复与清理。

证据缺失不能完成任务，文件改变或删除会变成 stale。--require-real 将真实验证设为必需；mock 成功不能关闭真实任务。scope 约束真实 case ID，报告不能越界。证据来自可信接入者，平台不将其冒充远端服务的签名证明。

体检不登录、不请求真实业务；ready 仅说明所查接入条件成立。它不代替真实字段、认证、数据有效性或数据库正确性验收。下一步具体命令见[快速接入](../onboarding/QUICKSTART.md)、[完整 Agent 手册](../onboarding/AGENT_INTEGRATION.md)与[操作及证据手册](../onboarding/AGENT_OPERATIONS.md)。

## 7. 执行容量、去重与停止

进程默认最多同时执行 2 项，后台排队容量 32。PLATFORM_EXECUTION_WORKERS 可配置 1–16，PLATFORM_EXECUTION_QUEUE_LIMIT 可配置 1–1000；修改后重启。后台 FIFO，同步兼容入口共享执行上限，满额时返回 incomplete 且操作未开始。此限制是进程内的，不是跨进程或跨主机全局调度。

工作台以 background=true 提交持久任务，用 executionId 查询终态；查询失败保留标识，刷新后可以恢复查询。requestId 对相同操作和参数去重，冲突拒绝；重试同一标识不会重新调用业务。确认终态后浏览器释放旧标识，下次明确执行生成新标识。

排队取消释放容量；运行中取消先请求停止并等待结果。协作检查点防止新增业务操作，恢复由 Pack 承担。只有 execution_policy 明确列入的 SAFE、无写入开关案例才可独立进程执行；父进程监控超时和取消，必要时终止自有进程组。GET 或 SAFE 标签本身不能证明无副作用，接入者须先核对实现。

已验证的进程组终止环境是 macOS/POSIX；Windows 后代进程树终止未验证。任意未声明隔离的阻塞 runner 不承诺强制终止。写案例不能用强杀冒充回滚，进程隔离也不是运行不受信任代码的安全沙箱。

## 8. 案例状态与任务状态

| 维度 | 状态 | 判断 |
| --- | --- | --- |
| 案例 | passed / failed / skipped / incomplete | passed 对应 ok=True，failed 对应 ok=False，skipped/incomplete 对应 ok=None |
| 任务活动态 | queued / running / cancel_requested | 未到终态，不能宣布通过或已经取消 |
| 任务终态 | done / failed / incomplete / skipped / interrupted / cancelled / timed_out | 调度、执行或停止结论，须同时读取 result 中的案例状态 |

cancelled 和 timed_out 是任务生命周期终态。停止后的未完整案例保持 incomplete，而不是把 cancelled 当第五种案例状态。缺配置、未执行或不完整子断言不能算 passed；进程中断不自动重放可能已触发的业务操作。

## 9. 历史、差异与平台 API

历史保存结果、子断言、来源、耗时、Pack、代码指纹、已声明契约指纹和脱敏选项。落盘失败须报告 historySaved=False；代码版本缺失、契约指纹缺失需要明确标记。

GET /api/history/compare?before=1&after=2 比较案例增删、状态、子断言、代码与契约指纹；重复断言标签按出现次序比较，不重新执行。明确属于不同 Pack 的历史拒绝比较；旧历史缺身份或指纹时不推断缺失证据。契约漂移是关联证据，不证明它必然导致某个失败。

| API | 用途 |
| --- | --- |
| GET /api/platform、/api/catalog | 项目、能力、兼容性与目录 |
| POST /api/run-case、/api/run | 单例或套件执行；后台请求返回任务标识 |
| GET /api/execution/queue、/api/execution/job?id=… | 进程容量和持久任务查询 |
| POST /api/execution/cancel | 请求取消所属任务 |
| GET /api/history、/api/history/compare | 历史和两次执行的差异 |
| GET /api/validate | 目录结构诊断 |

这是主要接口索引，不表示所有业务专项路由已启用。可选诊断和资源路由按当前 Pack 能力声明展示，未提供业务能力时不自动加载其他项目实现。

## 10. 发行与已验证范围

export_platform.py 生成独立平台与可选业务包，并记录逐文件 SHA-256。只复制声明的源码、通用测试、主题、说明与合成样例；过滤私有环境、真实账号池、运行库、缓存状态和仓库元数据。交付后运行数据与源代码清单分开，修改发行文件后需刷新清单并核验链接。

2026-10-09 的验收快照：147 项 Python、9 项 JavaScript 定向检查；独立双项目模拟业务 24 项检查；最后发行修复后 9 项发行检查通过。临时真实业务覆盖三类角色、五项只读案例，正向查询、未登录拒绝和角色越权拒绝最终通过；请求去重、隔离执行和历史对比已验证。

首轮临时权限断言不匹配已正确记录为失败，核对业务错误定义后在所属临时 Pack 修正，再重测通过。平台没有加入该业务错误码或真实业务资产。临时 Pack、私有凭据、历史与进程已清理；所检源文件和 Git 状态未改变。认证产生的远端会话与审计按服务策略保留，不宣称删除本地文件等于撤销 token。

这些是有限样本结论，不是整个平台可靠性百分比。真实写入、DB 对账、生产、全量业务案例及其他操作系统未验证；浏览器渐变背景对比度仍有一类自动判定缺口。完整证据见[六项优化及真实只读验收](../reports/six-improvements/进度.md)，不得把 mock 或历史报告中的通过外推到下一业务项目。

## 11. 维护规则

架构以当前源码、归属清单和 manifest 为准；完整流程维护在 onboarding，细节规则维护在架构专题，验收报告保留日期与覆盖范围。平台扩展、版本门禁、状态、执行生命周期、接入工具或发行边界变化时同步更新本白皮书和受影响入口。

文档版本升级不自动升级平台 API，不代表重新完成真实验收。纯文档修订只做链接、代码与声明对照、发行指纹和残留检查，不触发真实接口、全量案例或服务重启。

# Agent 接入、升级与运行操作手册

当前支持 macOS/Linux（POSIX）；Windows 不支持，不能以“未验证”推断可启动。新生成离线 Pack 的案例 ID 为 `<PACK_ID大写>-R01`；内置 demo 仍为 `DEMO-R01`，HTTP starter 仍使用显式 HTTP case ID。

与完整接入手册共同使用；命令从平台根目录执行。生成新 Pack 后先运行自己的离线验证，真实业务验收按授权范围逐例执行。没有 Git 不影响以下命令。

## 一键体检

```sh
python3 scripts/project_doctor.py --pack my_app --mock-case MY_APP-R01 --report /tmp/my-app-doctor.json
# HTTP starter 改用 --mock-case HTTP-MOCK-R01
```

体检核对当前 Pack、API 兼容范围、独立环境、端口格式、数据和证据目录、声明的业务配置键、bootstrap、目录及指定 mock。报告逐项给出状态与下一步操作，隐藏配置值。`--probe-port` 可额外探测本机端口可用性，不会停止占用端口的进程。ready 只说明这些接入条件，不证明真实业务已经通过。

新认证或数据库 Adapter 在 pack.json 声明 `required_environment` 键名数组；不要写值。已有 HTTP starter 自动登记地址与鉴权键。体检不登录、不调用业务接口。

## 可接续的证据任务清单

```sh
python3 scripts/project_tasks.py --pack my_app init --require-real
python3 scripts/project_tasks.py --pack my_app status
python3 scripts/project_tasks.py --pack my_app record --task adapter --evidence /tmp/my-app-doctor.json
```

状态保存在 `data/<pack>/onboarding-tasks.json`，不保存凭据或报告正文。scope、environment、adapter、mock、regression、real_readonly 分开验证。mock 与 adapter 证据来自体检，regression 必须是本 Pack 的定向测试组并通过。scope 的人工授权证据格式为 `{"pack":"my_app","authorizedReadOnly":true,"caseIds":["REAL-R01"]}`。真实报告必须标明同一 Pack、scope=real_readonly、realBusinessVerified=true、ok=true 和实际 caseIds；不允许超出 scope 的案例。

记录保存报告 SHA-256；证据改变或删除后，status 会显示 stale，须重新验收。真实验收同时绑定本次授权范围证据的摘要；范围被替换、失效或不再覆盖已执行案例时，真实验收也标记 stale，不能保留旧成功。报告内容与摘要来自同一次文件读取；损坏或缺项的任务清单拒绝装载。真实报告是可信接入者提供的证据，不是远端服务的加密证明；应保留执行环境、断言和限制。启用 --require-real 后，mock 成功不能关闭真实验收任务。执行时保留必要的证据文件，临时 Pack 完成后一起清理。

## Adapter 配方

`examples/adapter_recipes/README.md` 提供显式登录回调、过期缓存与并发复用、配置化响应信封、分页路径校验以及无损解码说明。将选中的代码复制到本 Pack helpers，以完整包路径导入；不在 Core 写项目 OAuth、签名、业务字段或默认角色。登录失败不回退到旧 token；收到 401 不自动重放业务写操作。

## 协议版本与升级门禁

生成器声明 `platform_api`：`{"min":"1.1","max_exclusive":"2.0","features":["request_id","capability_adoption"]}`。Core 在 bootstrap 前验证范围、字段类型、重复及所需能力；不兼容时拒绝装载。旧 Pack 未声明时按现有协议兼容，但体检会提示补显式约束。GET /api/platform 返回当前版本、支持能力与当前 Pack 兼容结果。升级先体检与定向回归，再启动独立验收进程，不直接替换运行中的业务配置。

生成 Pack 后可以用其独立启动入口操作当前项目。独立副本升级前可以比较原始发行、当前副本和目标发行，再审核文件冲突和必需能力。具体命令和中性发行来源见[中性发行与升级](../guides/中性发行与升级.md)。

## 执行队列和卡死隔离

进程内默认最多 2 个执行，后台最多排队 32 个。可在该项目环境配置 PLATFORM_EXECUTION_WORKERS=1–16 与 PLATFORM_EXECUTION_QUEUE_LIMIT=1–1000，修改后重启。后台任务 FIFO，同步兼容入口也受相同执行上限控制；满额时明确 incomplete，操作尚未开始，不自动重放。GET /api/execution/queue 查询进程内运行及排队数；不宣称跨主机全局并发控制。

工作台使用 background=true 提交持久任务并轮询。API 接入者可用相同参数；返回 pending 时按 executionId 查询任务，不重复提交。排队取消释放队列容量，运行中取消先让 Pack 清理。保留 requestId 的去重规则；失败或满额任务核验后使用新的标识重试。

首个提交响应丢失时，可凭原请求号查询 `GET /api/execution/receipt?requestId=…&kind=catalog-case`。用例批次的 kind 为 `catalog-batch`。该接口使用当前 Pack 的面板会话，仅返回任务身份、状态和过期信息。查到执行 ID 后按原任务继续查询。没有查到时保留请求号和待核验状态。需要约束新平台能力的 Pack 可声明 `request_receipt_lookup`。

取消或超时后的批次仍保存已完成案例与未完成范围，历史不会把部分通过当作整次通过。SAFE 批次含写入标记时也需面板写入确认，不能仅按 SAFE 标签判断没有副作用。结果正文归档后返回 `resultExpired=true` 与核验提示，同 requestId 仍不会重放；不要删除任务数据库来清理历史，否则去重摘要也会丢失。

停止/重启先协作取消并等待已有任务和恢复结束。脚本等待窗口耗尽时返回非零并保留原服务，不自动强杀；待清理完成后重试。临时清理故障修复后可再次请求停止。没有控制检查或恢复契约的业务写操作仍不能保证在有限时间内结束。

要验收轮次证据、回复未知后的接续决策及清理凭据，可先运行[长任务证据配方](../guides/长任务证据配方.md)。

只读 Pack 可在 `execution_policy` 声明 `isolated_case_ids` 和 `case_timeout_seconds`（有限正数，最多 3600）。只有显式列入的 SAFE、无写入开关案例才能在独立进程执行；父进程监控时限和取消并终止其进程组。当前进程组终止验收环境为 macOS/POSIX；Windows 的后代进程树终止尚未验证，接入者不得沿用本轮结论。示例：`{"isolated_case_ids":["OFFLINE-R01"],"case_timeout_seconds":30}`。先验证该案例实际只读；GET 标签本身不能证明无副作用。写操作保持协作取消与项目恢复，不能通过强杀冒充回滚。进程隔离不等于运行不受信任 Pack 的安全沙箱。

## 历史结果与契约比较

历史页在至少两条记录时提供执行对比。也可 GET /api/history/compare?before=1&after=2。它比较增删案例、状态、子断言差异、代码与契约指纹，不重新执行。历史无指纹时明确标注证据缺失；跨 Pack 版本证据不允许混比。契约漂移提示同期变化，不证明它就是失败原因。

用例的主要接口沿用 `case.api` 或 `RUNNER_API`，可用 `fixtureApis` 声明前置接口。多个契约来源时，可按 `{"api":"GET /items/{id}","source":"service"}` 标明归属。在当前 Pack 下用两份已审阅快照生成复验清单：

```sh
PLATFORM_ENV_FILE=.env.my_app python3 scripts/contract_case_impact.py \
  --before service=old.json --after service=new.json
```

需要对照一条已有运行时增加 `--history-run 42`。输出列出关联角色、版本摘要、受影响案例及待核对项。原运行结果保留在对应版本，新契约下的受影响案例标为待复验。来源或关联无法确认时先核对 Pack 声明。

## 真实验收与恢复

使用临时独立平台/Pack、独立端口和数据根；只装载已批准的必要角色，证据与业务判断放临时 Pack。少量正向、认证拒绝、角色权限、类型及分页断言分别报告。记录每项实际结果、耗时、覆盖面、缺口与平台执行是否正确，不能把少量通过率外推为整个平台可靠性百分比。

清理自有进程、临时凭据、Pack、配置、池和历史；核对主平台原有配置及业务源指纹。认证产生的远端会话与审计记录按服务契约处理，不能宣称删除本地凭据就已撤销远端 token。对真实写操作没有恢复契约时不执行。

## 请求、采用清单与工作台扩展（API 1.1）

- 请求签名通过可选请求准备协议复用 HTTP 管线；旧 transport 不改变入参。算法、查询规范与精确编码由 Adapter 显式选择，见[请求准备与签名](../guides/请求准备与签名.md)。
- doctor、任务状态、GET /api/platform 和默认工作台展示同一能力采用清单。采用声明、证据状态和验证范围分开；必需能力缺证据为 incomplete。旧 Pack 显示未声明，可按[能力采用清单](../guides/能力采用清单.md)迁移。
- 能力证据在执行前通过 capability-context 取得当前绑定，完成后用 record-capability 登记。改声明、Adapter 或适用版本后重新验收，不为旧报告直接贴新标签；real_readonly 不代表真实写入。
- 自定义 panel_html 可组合使用[工作台交互组件](../guides/工作台交互组件.md)，沿用安全客户端与执行客户端。刷新只恢复查询，任务终态和业务状态由 Pack 分别解释。

扩展机制的自有回环验收：`python3 scripts/accept_foundation_extensions.py --report /tmp/foundation-extensions.json`。浏览器样例使用 `--serve --state /tmp/foundation-panels.json` 临时生成两个隔离 Pack；结束后 Ctrl-C 清理自有进程与数据。全部为合成机制验收，不代表真实业务已验证。

# Agent 接入、升级与运行操作手册

与完整接入手册共同使用；命令从平台根目录执行。生成新 Pack 后先运行自己的离线验证，真实业务验收按授权范围逐例执行。没有 Git 不影响以下命令。

## 一键体检

```sh
python3 scripts/project_doctor.py --pack my_app --mock-case DEMO-R01 --report /tmp/my-app-doctor.json
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

记录保存报告 SHA-256；证据改变或删除后，status 会显示 stale，须重新验收。真实报告是可信接入者提供的证据，不是远端服务的加密证明；应保留执行环境、断言和限制。启用 --require-real 后，mock 成功不能关闭真实验收任务。执行时保留必要的证据文件，临时 Pack 完成后一起清理。

## Adapter 配方

`examples/adapter_recipes/README.md` 提供显式登录回调、过期缓存与并发复用、配置化响应信封、分页路径校验以及无损解码说明。将选中的代码复制到本 Pack helpers，以完整包路径导入；不在 Core 写项目 OAuth、签名、业务字段或默认角色。登录失败不回退到旧 token；收到 401 不自动重放业务写操作。

## 协议版本与升级门禁

生成器声明 `platform_api`：`{"min":"1.0","max_exclusive":"2.0","features":["request_id"]}`。Core 在 bootstrap 前验证范围、字段类型、重复及所需能力；不兼容时拒绝装载。旧 Pack 未声明时按现有协议兼容，但体检会提示补显式约束。GET /api/platform 返回当前版本、支持能力与当前 Pack 兼容结果。升级先体检与定向回归，再启动独立验收进程，不直接替换运行中的业务配置。

## 执行队列和卡死隔离

进程内默认最多 2 个执行，后台最多排队 32 个。可在该项目环境配置 PLATFORM_EXECUTION_WORKERS=1–16 与 PLATFORM_EXECUTION_QUEUE_LIMIT=1–1000，修改后重启。后台任务 FIFO，同步兼容入口也受相同执行上限控制；满额时明确 incomplete，操作尚未开始，不自动重放。GET /api/execution/queue 查询进程内运行及排队数；不宣称跨主机全局并发控制。

工作台使用 background=true 提交持久任务并轮询。API 接入者可用相同参数；返回 pending 时按 executionId 查询任务，不重复提交。排队取消释放队列容量，运行中取消先让 Pack 清理。保留 requestId 的去重规则；失败或满额任务核验后使用新的标识重试。

只读 Pack 可在 `execution_policy` 声明 `isolated_case_ids` 和 `case_timeout_seconds`（有限正数，最多 3600）。只有显式列入的 SAFE、无写入开关案例才能在独立进程执行；父进程监控时限和取消并终止其进程组。当前进程组终止验收环境为 macOS/POSIX；Windows 的后代进程树终止尚未验证，接入者不得沿用本轮结论。示例：`{"isolated_case_ids":["OFFLINE-R01"],"case_timeout_seconds":30}`。先验证该案例实际只读；GET 标签本身不能证明无副作用。写操作保持协作取消与项目恢复，不能通过强杀冒充回滚。进程隔离不等于运行不受信任 Pack 的安全沙箱。

## 历史结果与契约比较

历史页在至少两条记录时提供执行对比。也可 GET /api/history/compare?before=1&after=2。它比较增删案例、状态、子断言差异、代码与契约指纹，不重新执行。历史无指纹时明确标注证据缺失；跨 Pack 版本证据不允许混比。契约漂移提示同期变化，不证明它就是失败原因。

## 真实验收与恢复

使用临时独立平台/Pack、独立端口和数据根；只装载已批准的必要角色，证据与业务判断放临时 Pack。少量正向、认证拒绝、角色权限、类型及分页断言分别报告。记录每项实际结果、耗时、覆盖面、缺口与平台执行是否正确，不能把少量通过率外推为整个平台可靠性百分比。

清理自有进程、临时凭据、Pack、配置、池和历史；核对主平台原有配置及业务源指纹。认证产生的远端会话与审计记录按服务契约处理，不能宣称删除本地凭据就已撤销远端 token。对真实写操作没有恢复契约时不执行。

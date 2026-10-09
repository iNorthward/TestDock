# 任务完成汇报

汇报任务结果时，先给出整体状态，并让成功与异常一眼可分：

- `✅ 完成`：已完成且没有需要用户处理的异常。
- `⚠️ 部分完成`：明确列出未完成项、异常和需要用户处理的事项。
- `❌ 未完成`：明确说明阻塞原因和当前无法完成的部分。

整体状态后单独列出 **需关注**；有失败、异常、未验证、待确认或未解决缺口时置顶列出，没有时明确写“无”。随后再列 **已完成**、**验证**（区分通过、失败、未执行）和相关**数据变更**。不得把未执行/未验证写成通过，也不得把待确认事项混在成功项中。


# 接口案例序列化规则

新增或修改接口案例、请求构造、响应/对账校验、OAS 或 wire 清单前，必须阅读并遵守 `docs/architecture/平台序列化与反序列化规范.md`。先校验原始 JSON 类型再转换计算；请求/响应/DB 表示分别核实；项目策略归 Pack / Adapter，不按 OAS、Java 类型或字段名猜 ID/金额线型。只运行受影响的定向测试，遵守用户禁止全量案例执行的要求。

# 平台项目隔离规则

修改 Core、Pack、Adapter、启动配置或项目资源路径前阅读 `docs/architecture/平台与项目隔离规范.md`。每进程只装载一个项目 Pack；配置、案例来源、数据目录与服务进程归属须核实。业务规则与运维配置归 Pack，Core 不固定项目资产或直接导入业务实现。`common/` 只放已登记的通用组件；业务案例与 helper 放入所属 Pack，使用包路径导入，旧导入兼容入口只由所属 Pack 注册。修改隔离机制后运行 `scripts/check_platform_isolation.py` 与受影响的定向测试，不执行全量案例。

# 项目接入工作流

接入新业务先读 docs/onboarding/AGENT_INTEGRATION.md，使用 create_project_pack.py 与 verify_project_pack.py。业务认证、契约、案例、helpers、数据与写入策略归所属 Pack，不修改 Core 迁就业务。运行配置统一使用 PLATFORM_*；不同项目独立环境文件、数据根与端口。维护平台发行时运行 check_neutral_distribution.py，禁止泄漏源项目名称、个人路径、业务资产或凭据。新增接入工具同时登记 Core 归属和定向回归。真实业务验收与 mock 验收分开报告。

接入入口为 docs/onboarding/QUICKSTART.md。已确认 HTTP GET 契约时使用 create_project_pack.py --http-contract；不猜真实字段。案例注册须包含 CAT 字典。未完成/未执行结果统一 ok=None 与 status=incomplete；真实失败用 ok=False。交付前在允许本机回环服务的环境执行 accept_project_integration.py，并保留报告。

接入、升级与任务接续同时阅读 docs/onboarding/AGENT_OPERATIONS.md。真实验收和 mock 分开，证据变化后重验；平台版本门禁不得绕过。只读进程隔离须由 Pack 明确列出 case ID，写操作不得强杀冒充恢复。

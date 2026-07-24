# Claim-to-Evidence Scientific Workflow

一个面向长期科学计算任务的、仓库原生的 Codex 工作流。人可以直接用自然语言提出科学 idea；Agent 负责把它整理成可持续研究的内部状态，并在必要边界前渐进式形式化。

```text
Human authority:
自然语言 idea -> Brief / approval ---------------------------> Verdict

Research Workspace（图外、可变）:
猜想 / 临时代码 / 失败探索 / draft

认知图（为什么、知道了什么）:
EvidenceGap -> ExperimentIntent -> Observation -> Evidence -> Claim

控制图（如何执行）:
ExperimentIntent 精确引用 -> ControlGraphSpec -> active PLAN -> Run / Artifact

跨图桥接:
Run / Artifact -> provenance-bound interpretation -> Observation
```

本框架的目标不是让流程替代科学判断，而是让代码、实验和结论之间保持可追溯、可复现、可审查。`studyctl` 只记录和验证事实，不自动发明科学结论。

## 核心原则

- **Idea-first**：人直接描述想法，不需要先填写工作流表格。
- **Just-in-time alignment**：Agent 先检查仓库并起草；只有歧义会改变研究目标、受保护条件、硬预算或立即执行的高成本操作，且没有安全可逆默认值时才询问人。
- **Default informal**：低成本、可逆探索默认放在 Study 的 `work/active/`，不预先形式化整个研究过程。
- **Workspace 位于双图之外**：原始猜想、失败尝试、自由笔记和临时代码留在可变 Research Workspace；认知图只承载 EvidenceGap、ExperimentIntent、Observation、Evidence 和 Claim。
- **Intent / Plan 分离**：`ExperimentIntent` 是认知对象，说明为什么执行、要填补什么证据缺口以及怎样解释未来观察；`ControlGraphSpec` 是控制对象，说明调用哪些节点、依赖、执行器和资源。Plan 必须精确绑定一个 finalized Intent 版本。
- **Progressive formalization**：只在科学语义、共享依赖、计算成本、可复现性或审查要求上升时创建最小必要的 `METHOD`、`PROTOCOL`、`EVALUATOR` 或 `PLAN`。
- **Claim-to-Evidence**：简单观察直接内嵌在单 Claim Evidence 中；只有版本化 Registry 中的提升条件适用时才创建不可变 Observation Record。新条件须经独立审查、明确的人类采纳和受保护的追加式 Registry 更新；Observation 绑定该 Registry、其精确版本与哈希。Evidence 再说明观察如何影响 Claim、依赖哪些辅助假设、有哪些竞争解释以及什么结果会推翻当前判断。
- **Default exploratory, confirm on promotion**：所有普通 Run 默认是探索性的；只有准备把结果提升为高强度 Claim 时，才冻结一个很小的确认记录并运行新的确认性 Runs。
- **Finite active context**：历史可以持续增长，但默认只加载有界的 `ACTIVE_CONTEXT.json` selector；Evidence、Frontier、Checkpoint 和 Compaction 负责保持当前工作集有限。
- **Human authority**：人批准 Brief，并分别裁决实现是否可接受、证据支持什么科学结论。

完整协议见[工作流指南](docs/scientific-agent-workflow.md)。

## 接入一个现有科学计算仓库

本框架应当**适配进宿主仓库**，而不是在旁边建立第二套源码、测试和实验目录。接入由显式调用的 [`bootstrap-scientific-workflow`](.agents/skills/bootstrap-scientific-workflow/SKILL.md) Skill 完成。

### 1. 准备安全的接入环境

需要 Python 3.11 或更新版本和一个 Git 仓库。建议从目标仓库的干净分支或独立 worktree 开始，并确保 Codex 可以同时读取：

- 目标科学计算仓库；
- 本框架源码及 Bootstrap Skill。

当前发布形态是“源码仓库 + 显式 Bootstrap Skill”，尚未提供一行命令安装器。尚未接入的目标仓库也无法发现它未来才会拥有的 repo-local Skill。因此可以将本仓库作为相邻的只读协议源，或者先把 Bootstrap Skill 以个人 Skill / Plugin 的方式提供给 Codex；无论采用哪种方式，都应把本框架源码路径和目标仓库路径写清楚。

V2 必须从目标 Git worktree 根目录运行。科学源码、测试和实验目录可以通过 profile 适配，但运行时锚点目前固定为 `scientific-workflow/`、`tools/studyctl/` 及其仓库级配置；若这些路径与宿主现有结构冲突，应在 Bootstrap 阶段合并或请求明确迁移，不要静默改名或嵌套安装。

### 2. 直接给 Codex 接入 Prompt

如果 Bootstrap Skill 已经可见，可以在目标仓库中直接使用：

```text
$bootstrap-scientific-workflow

把 Claim-to-Evidence Scientific Workflow 接入当前科学计算仓库。

先检查现有源码、测试、实验配置、验证命令、Git 约定、对象存储、
AGENTS.md、Codex 配置和已有实验跟踪机制，再提出最小适配方案。

复用现有机制，不要创建平行源码树，不要改变科学程序行为，
不要启动真实 Study，也不要覆盖现有配置。完成后运行宿主仓库验证、
工作流测试和幂等性检查，并报告所有路径映射与剩余人工事项。
```

如果 Skill 尚未安装，则在 Prompt 中明确给出协议源：

```text
框架源码：/ABSOLUTE/PATH/TO/agent-workflow
目标仓库：当前工作区

请完整遵循框架源码中的
.agents/skills/bootstrap-scientific-workflow/SKILL.md，
并以同一框架仓库中的指南、schemas、templates、studyctl 和 tests
为协议源，将工作流最小化地适配进当前仓库。

只修改目标仓库；先检查和映射，再安装和验证；不要开始科学研究任务。
```

Bootstrap 只负责搭建或升级工作环境。建立第一个 Study 是后续独立操作。

### 3. 审查仓库适配契约

接入的核心不是复制目录，而是正确配置 [`scientific-workflow/repository-profile.json`](scientific-workflow/repository-profile.json)：

| 配置 | 在宿主仓库中的含义 | 常见映射 |
|---|---|---|
| `study_root` | Brief、Claims、Runs、可选 Observations、Evidence、Checkpoint 等研究状态 | `studies/` 或 `research/studies/` |
| `object_root` | 大型 Run 输出；必须位于仓库内且被 Git 忽略 | `.objects/`、`artifacts/` |
| `source_roots` | 被采用的生产科学代码 | `src/`、`packages/solver/` |
| `test_roots` | 宿主仓库原生测试 | `tests/`、`test/` |
| `experiment_roots` | 可复用实验配置和启动代码 | `experiments/`、`configs/` |
| `run_cwd` | 注册计算实际执行时的工作目录 | `.` 或某个 package 根目录 |
| `commands` | 宿主原生验证命令，按 argv 数组保存，不使用 shell 字符串 | pytest、构建、类型检查、科学验证命令 |
| `git` | Study 分支格式、基准分支和 worktree 要求 | `main`、`study/{study_id}/{slug}` |

本仓库当前 profile 描述的是**框架源码仓库自身**：它把 `tools/studyctl` 视为源码，把 `tests` 视为测试。接入实际科学软件时不能原样照搬；应把生产代码、测试、实验配置和验证命令映射到宿主仓库的原生位置。

一旦 `study_root` 或 `object_root` 中已经存在研究记录或输出，改变它们就是数据迁移。V2 没有自动 root migrator；应保留原路径，或使用经过审查、能维持 manifest 路径、哈希和外部指针的显式迁移方案。

### 4. 确认产出存放规则

| 产出 | 应放在哪里 |
|---|---|
| 临时推导、候选想法、一次性脚本、原型代码 | `<study_root>/SC-NNNN/work/active/` |
| finalized ExperimentIntent（认知层：为什么执行） | `<study_root>/SC-NNNN/intents/` |
| finalized ControlGraphSpec（控制层：如何执行） | `<study_root>/SC-NNNN/control-plans/` |
| 被采用的生产实现 | profile 声明的 `source_roots` |
| 单元、集成、回归、收敛或科学验证测试 | profile 声明的 `test_roots` |
| 可复用实验配置与启动代码 | profile 声明的 `experiment_roots` |
| Brief、Claims、正式制品、Run manifests、可选 Observations、Evidence、Checkpoints | 对应 Study 目录 |
| checkpoint、数组、轨迹、profiler trace 等大型输出 | Git 忽略的 `object_root` 或由其中的哈希指针引用外部对象存储 |
| `STATUS.md`、`REVIEW_PACKET.json` 等生成视图 | Study 的 `generated/`；它们不是事实源 |

原则是：候选仍可丢弃时留在 `work/`；一旦其他代码、实验或研究者需要依赖它，就将实现、测试或配置提升到宿主仓库的原生目录，并纳入正常 Git 审查和验证。

### 5. 验证接入结果

至少运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m tools.studyctl --help
PYTHONDONTWRITEBYTECODE=1 python -m tools.studyctl profile-validate
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
git diff --check
git status --short
```

第三条命令在本框架仓库中是完整测试入口。目标仓库应以 profile 中配置的宿主原生验证命令为准，并额外验证：

- 原有测试和构建仍通过；
- `object_root` 确实被 Git 忽略；
- source、test、experiment 根目录真实存在，并且与 workflow/protected 路径没有重叠或遗漏；
- Bootstrap 的第二次执行没有产生实质变更；
- 没有真实 Study、Brief 批准或 Verdict 被 Bootstrap 伪造。

`profile-validate` 当前以错误为失败条件；成功输出不能代替对缺失 source、test 或 experiment 根目录等非致命适配警告的人工核对。

## 认知图与控制图

Research Workspace 位于两张权威图之外。认知图包含
`EvidenceGap -> ExperimentIntent -> Observation -> Evidence -> Claim`；控制图
包含 `ExperimentIntent` 精确引用、`ControlGraphSpec`、活动 PLAN、Run 与
Artifact。Run/Artifact 是执行和 provenance 事实，只有经过解释的 Observation
和 Claim-specific Evidence 才进入科学认知。

`ExperimentIntent` 说明为什么执行、请求哪些观察及如何评估它们；
`ControlGraphSpec` 说明节点、依赖、执行器、资源估计和完成条件。Agent 可以自由
修改 Workspace 中的草稿和控制拓扑，但 finalized Intent/Plan 只能追加新版本。
每个 Plan 必须精确绑定一个 finalized Intent，`formal/PLAN.json` 必须是通过
`plan-activate` 选中的 finalized ControlGraphSpec 的逐字节物化。

`GRAPH_RECORDS.sequence.json` 绑定所有 finalized Intent/Plan。Run 会快照活动
PLAN，但当前运行时尚不自动调度整张图，也不会把每个 Run 确定性地绑定到
ControlGraph 节点。ExperimentIntent 的判据已冻结用于审计，当前仍须由
Claim-specific Evidence 明示推理，不能从 Plan 或 Run 成功直接更新 Claim。

最小命令入口为 `intent-new`、`intent-finalize`、`plan-new`、`plan-finalize`
和 `plan-activate`；完整字段、示例与崩溃恢复见[工作流指南](docs/scientific-agent-workflow.md#progressive-formalization)。

## 接入后的请求路由

科学内容本身不等于“创建持久 Study”的授权。Codex 先按用户动作路由：

| 用户意图 | 行为 |
|---|---|
| 一次性讨论、解释、推导、批判或头脑风暴 | 直接回答，不创建或修改 Study |
| 明确要求开始、创建或持续调查一个新问题 | 使用 `start-scientific-study` 创建一个新草稿 |
| 继续一个已命名的 Study | 先运行 `python -m tools.studyctl resolve-study SC-NNNN` |
| 未给 ID，但明确要求继续之前或当前研究 | 先运行只读的 `python -m tools.studyctl resolve-study` |

无 ID 只表示“尚未选定对象”，绝不表示“自动新建”。只存在一个有效候选时，
resolver 会选择它；零个、多个或无效候选时，Codex 只询问一次必要的选择，
不会以 `init` 作为回退。未批准草稿继续由 `start-scientific-study` 修改同一个
Study；已批准 Study 由 `scientific-study` 续研。`VERDICT.json` 记录人的裁决，
当前协议不把它解释成自动关闭 Study 的状态。

## 接入后：直接从一个 idea 开始

用户不需要手工创建 Study 或填写 Brief。可以直接告诉 Codex：

```text
研究在现有 VMC 模型中加入等变 attention，目标是在保持精度的同时降低
Laplacian 计算成本。请直接建立研究任务并准备后续研究。
```

Codex 会调用 `start-scientific-study`，检查必要的仓库上下文，创建一个 `DRAFT` Study，起草 Brief 和 proposed Claims，并只在真正阻塞授权时询问最多三个对齐问题。它会停在人工批准之前，不会立即改生产代码或消耗昂贵计算。

审查 Brief 后，由人在交互式终端执行 Agent 返回的命令，例如：

```bash
python -m tools.studyctl approve-brief SC-0001
```

Brief 批准仍是 human-only gate，要求真实的 stdin/stdout TTY；不要让 Agent
代为调用，也不要通过管道、heredoc 或输出重定向伪造确认。

最终审查后，人不需要再复制或运行 Verdict 命令。写权限 Agent 先在对话中给出
一份精确的裁决摘要：Study 与 Claim、实现裁决及理由、科学裁决及适用范围、理由
和条件。只有当人的当前指令明确给出这些决定，或明确采纳这份刚展示的完整摘要
时，Agent 才把 version-2 decision-only 输入写到 profile 声明且 Git 忽略的
`object_root`，并发起：

```bash
python -m tools.studyctl verdict SC-0001 \
  --agent-initiated \
  --file <decision-input.json>
```

若决定或范围存在实质歧义，Agent 只询问一次必要问题；“继续”“完成任务”、审查
通过、沉默或 Agent 自己的建议都不构成接受。该入口记录用户指令、指令哈希和
`cooperative` assurance；最终 Verdict 自身的摘要绑定所有决定与机械 scope。它是可审计的协作来源记录，不是
密码学签名，也不声称 Hook 能验证聊天中的真实意图。需要时仍可由人运行
`python -m tools.studyctl verdict SC-0001` 使用交互式兼容入口。

Verdict 的 ID、时间、身份、commit 以及 Brief、Checkpoint、Claim、Evidence
哈希均由 `studyctl` 自动绑定；无需手写带哈希的 JSON。记录前应先提交已审查
状态，并为当前 Brief、审批和 Claims 生成最新 Checkpoint。

日常研究不需要预注册。Agent 可以自由运行 exploratory Runs，用它们发现假设、
筛选候选，并形成 `under_test` 或范围受限的 `partially_supported` Claim。只有准备
提升到高强度的 `numerically_supported` 状态时，才执行一次最小确认流程：

```text
自由探索
  -> 选择候选并明确待确认 Claim
  -> 为该精确 Claim 版本建立 confirmation campaign
  -> 冻结候选、协议、评价器、held-out 条件和分析规则
  -> 运行新的 confirmatory Runs
  -> 生成覆盖整个 campaign 的 confirmatory Evidence
  -> 提升 Claim
```

对应命令入口是：

```bash
python -m tools.studyctl confirmation-new SC-0001 \
  --id CONF-0001 --claim CLAIM-0001

# 编辑命令返回的最小草稿后，在任何确认性运行之前冻结：
python -m tools.studyctl confirmation-finalize SC-0001 \
  --file <confirmation-draft>

python -m tools.studyctl run SC-0001 \
  --mode confirmatory --confirmation CONF-0001 --slot SLOT-001 \
  --purpose "Confirm CLAIM-0001" \
  --input <input-path> --output <object-root/output-path> \
  -- <program> <arguments>
```

确认记录不是新的人工审批，只是一个运行前不可变的时间与哈希边界。每个
confirmatory Run 必须完全匹配冻结的 Claim、候选、协议、评价器、输入和 slot；
失败、中断或 incomplete 的尝试也会消耗 slot。Evidence 必须交代全部预定 slot
和全部可见尝试，混合 exploratory/confirmatory 结果时必须分别列出。同一精确
Claim 版本的后续 Confirmation 会自动串入同一 campaign；前序 slot 未全部终止时
不能定稿下一份。后续记录必须声明 `replication` 或
`corrective_supersession`，写明理由和差异；纠错性取代还必须绑定前一份记录并说明
其无效原因。Evidence 自动重建整个 campaign，遗漏旧失败或中断尝试会失败关闭。
旧 Evidence 保持不可变，但 campaign 扩展后不能单独维持
`numerically_supported`，直到新 Evidence 覆盖当前完整历史。旧 Run 或 exploratory
Run 永远不能通过改标签升级为 confirmatory。Run 子进程通过可插拔的密封执行
后端运行：macOS 使用 Seatbelt (`sandbox-exec`)，Linux/集群节点使用
Bubblewrap (`bwrap`)。环境变量使用白名单重新构造，仓库写入被拒绝，只允许保留
声明的输出，并禁止网络访问；读白名单之外的隐藏路径不可见。Linux 后端把
`object_root` 映射到私有 staging，命令结束后只复制声明的常规文件，因此程序即使
创建未声明的临时结果也不能把它们写入宿主仓库。Manifest 的
`execution_boundary` 记录后端、版本、策略格式与哈希、环境哈希及访问声明；缺少
这一强制边界的 Run 不能进入 Evidence。后端会在 Run ID 与预算分配前进行能力
探测；工具缺失、Linux 所需 namespace 能力不可用（非 setuid 安装通常需要
unprivileged user namespace）或 macOS 嵌套 Seatbelt 不可用时均失败关闭，不会
退化为仅比较运行前后哈希。

仓库 profile 的 `execution.backend_preference` 决定自动探测顺序；也可在单次运行
中传入 `--execution-backend linux-bubblewrap` 或
`--execution-backend macos-seatbelt`。Linux GPU 节点只在标准的
CUDA/NVIDIA、HIP/ROCm 或 oneAPI 可见性变量声明设备选择时，才把相应
NVIDIA、DRI 或 KFD 设备加入边界；宿主调度器的 device cgroup/权限仍是分配
边界。额外的编译器、MPI、CUDA 或 module
运行时目录必须由受保护 profile 的 `execution.trusted_read_only_paths` 明确
列出。它仍不是完整的内容寻址 capsule：profile `source_roots`、Python/runtime、
受信任系统路径和显式 runtime 路径仍是可读环境，依赖与外部程序尚未逐项内容
固定。Slurm/PBS 用户应在已分配的计算节点内运行
`python -m tools.studyctl run`；当前后端不负责
提交异步调度任务。

Brief 中可见的 `STUDYCTL-HARD-BUDGET` JSON 块是唯一的数值预算权威。
`null` 与数值 `0` 都不授权任何正用量；GPU-hour、CPU-hour 和存储预算
由 `studyctl run` 在启动子进程前累计检查并预留。失败、中断、未完整封存
和仍在运行的 Run 也占用预留量，避免通过失败重试绕过人的授权边界。
每个 Study 的 `RUNS.ledger.json` 保存连续、只增不减的 Run 编号高水位与
预算承诺；它位于 `runs/` 外，因此移动或重建整个 `runs/` 也不会重置历史。
ledger 缺失、损坏或引用的 Run 消失时，验证和新 Run 都会失败关闭。
同一 Study 内，声明的输出路径会在上述串行注册事务中随 `running`
Manifest 一起被预留；即使文件最终没有产生，后续 Run 也不能认领同一路径。
验证器还会拒绝任何伪造或历史损坏造成的重复输出所有权。
`EVIDENCE.sequence.json` 以同样的只增高水位记录 Evidence draft 的创建次数；
发布失败会烧掉一次计数，删除文件不会降低压缩压力；sequence 缺失时失败关闭，
当前运行时不从可见 Evidence 历史补建它。
`CHECKPOINTS.sequence.json` 则绑定连续 Checkpoint 高水位和最新 Checkpoint 哈希；
缺失、删除、改名或回滚 Checkpoint 尾部都会使验证、context 和下一次压缩失败
关闭，当前运行时不提供补建该 sequence 的迁移入口。
`GRAPH_RECORDS.sequence.json` 绑定全部 finalized ExperimentIntent 与
ControlGraphSpec 的记录摘要和文件摘要；其高水位必须与可见记录数严格相等。
它在 Study 初始化时建立；缺失时验证失败关闭，不允许从可见历史补建。
记录已经落盘但 sequence 尚未推进的单记录崩溃窗口，只能显式运行
`recover-graph-record-sequence` 向前恢复，绝不会自动降低高水位。

然后只需告诉 Codex：

```text
继续研究 SC-0001。
```

后续 `scientific-study` Skill 会先运行 `validate` 和 `context`，从有界的
`generated/ACTIVE_CONTEXT.json` 恢复。该 selector 对当前 Frontier 和 active
Claims 只保存 ID、短预览、计数与哈希，对 Brief、正式制品和最新 Checkpoint
只保存路径、哈希、大小与计数摘要；另有有界 Confirmation 索引暴露可恢复的
草稿、待运行/运行中 slot 和等待 Evidence 的记录，避免重复创建。Agent 再按当前问题或 ID 下钻，不会默认
重读全部历史或把完整 Claims 复制进启动上下文。

终态 Claim 不会因压缩而只剩不可逆哈希：Checkpoint 首次封存时会在
`checkpoints/claim-records/` 写入内容寻址的完整 Claim，并验证其路径与哈希；
记录必须满足 Claim Schema、使用规范内容寻址路径、只读且无硬链接。终态 Claim
一经封存，其完整内容和状态都不可在原 ID 下改写；历史 superseded 链必须始终
终止于 active Claim。只有此后才允许从当前 `CLAIMS.json` 移出对应终态记录，
这些归档内容中的输出引用也继续受到 GC 保留规则保护。

研究压缩同样只生成有界索引：`COMPACTION_INPUT.json` 中的 Claims、Frontier、
ExperimentIntent/ControlGraphSpec、Run/Observation/Evidence/Cohort、Confirmation 工作、work、正式制品、失败方向和宿主变更路径只保留有限 locator
批次，并用全量计数与 `inventory_sha256` 绑定完整历史。压缩 plan 只携带常数大小
的 Evidence inventory binding，并只携带一次完整 Frontier，不复制全部 Evidence
路径表或另建问题/行动列表；finalize 会重算全量哈希，因此未出现在 locator
批次中的变化仍会使计划失效。Formalization debt 是由当前 policy、范围和活动制品
计算的治理状态，不是 `CLAIMS.json` 中可由作者修改的字段。

当前运行时只接受其随附的当前 Claims、ExperimentIntent、ControlGraphSpec、
Observation、Evidence 和 Checkpoint schema；
任何不支持的 schema 版本都会 fail closed。当前 CLI 不会把旧格式作为
历史 schema 变体继续验证，不会推断缺失的 Evidence 语义，也不会就地重写。
旧安装应继续使用与其记录格式匹配且由 Git 固定的旧版工作流，或者先在
隔离副本中完成显式、经审查的离线迁移，验证迁移后的记录、引用、哈希和
不可变历史后再采用当前运行时；不得通过新建替代 Study 绕过不兼容。

## 修改宿主代码和测试时

原型可以留在 Study 的 `work/`，但正式源码、测试和实验资产必须进入 profile 声明的宿主原生目录。修改前先在 Study 分支或所需 worktree 中创建最小变更合同：

```bash
python -m tools.studyctl changeset-new SC-0001 \
  --allow 'src/solver/**' \
  --allow 'tests/solver/**'
```

实际 Git diff 而不是 Agent 自述决定允许范围。修改完成并提交后，运行：

```bash
python -m tools.studyctl validate-changes SC-0001
python -m tools.studyctl check-changes SC-0001
```

只有范围、宿主验证和 provenance 合格的 Run 才能进入正式 Evidence。完整执行、Evidence、Compaction 和 Review 命令见[工作流指南](docs/scientific-agent-workflow.md)。

每次重要计算都会先登记 `running` Manifest，再启动程序，最后原子封存为
`succeeded`、`failed`、`interrupted` 或 `incomplete`。后处理失败不会留下
不可见的执行；`validate` 还会交叉检查 ledger、所有 `RUN-*` 目录和
Manifest。注册在启动程序前依次持久化预算/ID、完整 `running` 目录和启动
授权；任何中途失败都会保留可诊断、占预算且不可复用的记录。
同一 Study 的 Run 登记、Brief 审批/修订、Verdict 与压缩落盘共享一个锁，
不会在并发操作中混用不同版本的权威状态。已声明但未产出的输出路径也会被
保留；若文件稍后出现，或已有输出无法建立稳定哈希，后续 Run 将 fail-closed，
防止存储预算被绕过。

升级前已经存在的连续 V1/V2 Run 历史不会被普通 `run` 自动重建 ledger。
先用外部记录确认历史完整，再显式执行
`python -m tools.studyctl ledger-migrate SC-NNNN`；迁移拒绝空历史、ID 缺口、
V3/V4 Run 或已有 ledger。V4 是首个显式记录 exploratory/confirmatory 角色的
Manifest 版本；V1–V3 永久按 exploratory 解释。

## 仓库结构

```text
AGENTS.md                         始终生效的最小不变量
.agents/skills/                   Bootstrap 与四个运行期 Skills
.codex/                           Codex 配置、只读 Reviewer 和小型 Hook
scientific-workflow/              profile、policy、schemas 与 templates
tools/studyctl/                   确定性 CLI 和协议门禁
studies/                          长期 Study 状态
.objects/                         Git 忽略的大型 Run 输出
tests/                            工作流回归与压力场景契约测试
docs/scientific-agent-workflow.md 完整协议指南
```

## 本仓库开发与验证

要求 Python 3.11 或更新版本。框架本身不需要数据库、后台服务或 Web UI。

```bash
python -m tools.studyctl profile-validate
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

已知边界和威胁模型记录在[工作流指南末尾](docs/scientific-agent-workflow.md#recover-or-reproduce-a-run)。Hook 只是早期 guardrail；真正的约束来自 profile、Git 实际状态、哈希与不可变快照、确定性验证、独立 Reviewer 和人的最终审查。

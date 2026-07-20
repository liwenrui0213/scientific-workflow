# Claim-to-Evidence Scientific Workflow

一个面向长期科学计算任务的、仓库原生的 Codex 工作流。人可以直接用自然语言提出科学 idea；Agent 负责把它整理成可持续研究的内部状态，并在必要边界前渐进式形式化。

```text
自然语言 idea
  -> Agent 起草 Brief 与 Claims
  -> 人确认研究边界
  -> 自主探索与代码实现
  -> 不可变 Runs
  -> Evidence
  -> Claims
  -> 独立审查
  -> 人的最终 Verdict
```

本框架的目标不是让流程替代科学判断，而是让代码、实验和结论之间保持可追溯、可复现、可审查。`studyctl` 只记录和验证事实，不自动发明科学结论。

## 核心原则

- **Idea-first**：人直接描述想法，不需要先填写工作流表格。
- **Just-in-time alignment**：Agent 先检查仓库并起草；只有歧义会改变研究目标、受保护条件、硬预算或立即执行的高成本操作，且没有安全可逆默认值时才询问人。
- **Default informal**：低成本、可逆探索默认放在 Study 的 `work/active/`，不预先形式化整个研究过程。
- **Progressive formalization**：只在科学语义、共享依赖、计算成本、可复现性或审查要求上升时创建最小必要的 `METHOD`、`PROTOCOL`、`EVALUATOR` 或 `PLAN`。
- **Claim-to-Evidence**：Claim 引用已定稿 Evidence；Evidence 引用可复现、不可变的 Runs。
- **Finite active context**：历史可以持续增长，但通过 Evidence、Frontier、Checkpoint 和 Compaction 保持当前上下文有限。
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
| `study_root` | Brief、Claims、Runs、Evidence、Checkpoint 等研究状态 | `studies/` 或 `research/studies/` |
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
| 被采用的生产实现 | profile 声明的 `source_roots` |
| 单元、集成、回归、收敛或科学验证测试 | profile 声明的 `test_roots` |
| 可复用实验配置与启动代码 | profile 声明的 `experiment_roots` |
| Brief、Claims、正式制品、Run manifests、Evidence、Checkpoints | 对应 Study 目录 |
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

第二条命令在本框架仓库中是完整测试入口。目标仓库应以 profile 中配置的宿主原生验证命令为准，并额外验证：

- 原有测试和构建仍通过；
- `object_root` 确实被 Git 忽略；
- source、test、experiment 根目录真实存在，并且与 workflow/protected 路径没有重叠或遗漏；
- Bootstrap 的第二次执行没有产生实质变更；
- 没有真实 Study、Brief 批准或 Verdict 被 Bootstrap 伪造。

`profile-validate` 当前以错误为失败条件；成功输出不能代替对缺失 source、test 或 experiment 根目录等非致命适配警告的人工核对。

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

该命令和最终 `verdict` 命令都是 human-only gate，要求真实的 stdin/stdout TTY；不要让 Agent 代为调用，也不要通过管道、heredoc 或输出重定向伪造确认。

然后只需告诉 Codex：

```text
继续研究 SC-0001。
```

后续 `scientific-study` Skill 会从已批准 Brief、active Claims、正式制品、最新 Checkpoint 和当前 Frontier 恢复，而不是默认重读全部历史。

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

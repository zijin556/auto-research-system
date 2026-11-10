# 科研文件管理系统 · 本次实现约定

## 2026-11-10 v2：分层讨论、三家模型、台账与版本

产品说明见 `工作流说明.md`；完整技术规格（文件格式、接口、规则）见 `.runtime/v2_spec.md` 的副本 `docs/v2_spec.md`。新增模块：

- `notebook/agents.py`：Codex / Antigravity / Claude Code 三个命令行的无界面调用（提示词走 stdin，取消时结束整个进程树，不自动重试）、错误分类、用量记录 `.runtime/usage.jsonl`、额度状态（Codex 周额度从 `~/.codex/sessions` 的 rollout 文件读取，不发请求）、角色分配与家族约束，以及设置文件 `00_模型与额度.json`。
- `notebook/ledger.py`：锚点 `锚点.md`、台账 `台账.md`、台账日志的读写；模型回复中各种块（正文修订、台账变更、验证计划、小问题清单、候选框架、复推）的解析；台账规则（新条目一律待定；确定或否定必须有绑定的运行或不同家族的独立复推；“人工”只能来自页面操作；反例先记为待核并自动加一条待验证；否定条目进淘汰清单）；正文按节修订（第 1 节由锚点生成，第 5 节只在采纳时写入）；结果占位符 `{{R01.键}}` 替换为指向 summary.json 的链接。
- `notebook/round_prompts.py`：各层各步骤的中文提示词模板，开头一行 `【模板】<key>`。
- `notebook/rounds.py`：讨论会话。一次“开始”是 1–5 轮；同一时间只运行一个会话；每轮在条目的 `讨论/第NN轮_时间/` 下保存任务、每一步的提示词和原始回复、`简报.md`、`round.json`。模型调用不持有存储锁。工程运行在 `%LOCALAPPDATA%/ResearchNotebook/work/` 的隔离目录中进行，结束后由系统导入代码与输出、按 summary.json 复核判据并判定结果（数值与 passed 不符记为出错）。停止条件：轮数用完、需要你决定、漂移、调用失败、没有进展、待验证项都有结果。
- `notebook/v2api.py`：`/api/v2/tree|item|session|settings|usage|git|history|version` 与 `v2_*` 动作。只有当研究根目录本身是 git 仓库顶层时才启用自动提交（示例目录位于主仓库内，不会单独提交）。
- `notebook/vcs.py`：git 提交与推送（3 秒内的多次提交合并为一次；单独线程推送；不强推；超过 50 MB 的文件不进仓库）。所有 git 调用都带 `core.autocrlf=false`，仓库含 `.gitattributes`（`* -text`），因为记录版本号是原始字节的 SHA-256，换行转换会让已判断的问题变成“需复查”。
- `static/v2/`：前端页面入口（默认地址 `/`），已移除旧经典版页面。

存储层改动：`_records` 从字面目录 glob（标题里的 `[ ]` 不再被当作通配符，修复了讨论记录“丢失”和编号重复）；研究内容保存章节角色 `role`；验证计划可记录 `target_entries`（台账条目及其内容哈希）与 `direction`（支持 / 否定）；文件接口开放框架层的锚点、台账和讨论目录。经典页面的每次保存也会触发自动提交。

测试：`py -3.12 -X utf8 -m unittest discover -s tests`。v2 测试用 `tests/fake_cli.py` 模拟三个命令行（环境变量 `RESEARCH_NOTEBOOK_FAKE_AGENTS=1`），不消耗额度。

## 2026-11-09 页内编辑与分层研究会话

问题正文和章节草稿使用 `inlineManuscript` 页内编辑，沿用 `save_body` / `save_draft` 的版本校验与历史备份。阅读区点击或 Enter 进入编辑，Ctrl+S 保存；当前标签页的 sessionStorage 按资料根、文稿类型、内部 ID 隔离未提交草稿。导航/刷新保留草稿，冲突和失败不丢文本，不进行无提示覆盖。框架元信息仍使用原编辑窗口。

问题页连续显示正文、折叠的问题定义与验证约定、阶段判断、原始讨论链接、代码与结果链接。正文路由从页面标题开始；其余锚点定位相应材料段。提示词操作保留在工具菜单。KaTeX 和现有文件服务继续使用。

`tools/prepare_research_session.py --root <资料目录> --scope framework|chapter|question --id <内部ID> --task <本轮目标> [--framework <框架ID>]` 只准备本地任务，不调用模型。自动读取上下文，保存到 `04_交接包/研究会话/<时间_随机标识>/`：任务.md、背景快照.md、来源清单.json，以及讨论/、候选正文/、审查/。在线优先读取匹配本机服务；离线取得工作区锁，并禁止 Store 初始化写入。来源记录包括路径、版本与 SHA256。此工具不构成多模型调度器，新的会话目录也尚未纳入网页讨论索引。

`tools/mini_paper_demo.py` 为示例根目录追加六章框架和三步相移小论文；`tools/verify_mini_paper_demo.py` 检查实际 CSV、解析特例、代码快照哈希、16 个正文链接及重复运行不改文件。示例没有人工结论，也没有真实仪器精度主张。完整协作约定见 `论文协作工作流.md` 和 `AI协作入口.md`。

正式根目录为本文件所在目录。不得擅自导入旧研究资料。示例与验收仅放在 `示例研究/` 或临时测试根目录。使用 Python 3.11+ 标准库后端和本地前端；日常阅读与编辑使用本地文件，用户发起 AI 整理时通过已登录的 Codex 联网调用模型。文件为真源，不需要独立数据库服务。

## 文件职责

- `notebook/store.py`：资料读写、版本、关系、编号、路径校验；由后端开发负责。
- `notebook/prompts.py`：P0–P10 提示词与交接包；由提示词开发负责。
- `notebook/semantic_jobs.py`：本机 Codex 登录检查、异步模型请求、取消、恢复与归档任务；`notebook/semantic_schema.json` 限定模型方案结构。
- `notebook/semantic.py`：原文与材料准备、模型方案校验、多问题原子归档与来源保存。
- `notebook/capture.py`：保留为明确标签的单问题本地规则入口，不充当 AI 请求失败后的替代。
- `notebook/server.py`、`nb.py`、启动停止脚本：主开发负责。
- `static/index.html`、`static/app.js`、`static/style.css`：前端开发负责。
- `tests/`：各自添加独立命名的测试，主开发完成联调验收。

## 数据与文件

`00_项目卡.md`、`01_论文框架/F01_标题.md`、`02_研究内容/C01_标题/研究内容.md`、`草稿.md`、`C01-Q01_标题/问题.md`、`推导笔记.md`、`对话/`、`代码/`、`验证计划/`、`结果/R01_标题/运行记录.md`、`结论/J01.md`、`03_提示词/提示词.md`、`总览.md`。

Markdown 正文为真源，结构字段可用可读的 Markdown 元数据/JSON sidecar（若用 sidecar，只存结构索引/稳定关系，不可藏正文），由后端选择可靠方案。所有编辑留历史；对话、提示词快照、判断追加保存。外部修改可刷新读取，不静默覆盖。重复编号、损坏文件可见报错，不忽略资料。研究内容独立于框架，框架 `content_ids` 为有序引用。问题固定属于一个研究内容。

## Python 接口

`Store(root: Path)` 自动初始化空白项目。

- `state()` → `{root, project, frameworks, contents, questions, warnings}`
- `detail(qid)` → 问题对象，加 `discussions, plans, runs, judgments, files, body, body_revision, path, latest_judgment`。
- `content_detail(cid)` → 研究内容对象，加 `draft, draft_revision, questions, placements, path`。
- `perform(action, data)` → 操作返回对象；失败抛 ValueError / FileNotFoundError / PermissionError / ConflictError。
- `read_file(relative)` → 安全根目录内 Path；仅允许已登记研究资料，拒绝程序目录、隐藏目录、越界、symlink/junction 外跳。服务器自行决定下载或文本预览。

所有实体带 `id,title,revision,path`。revision 为文件内容 SHA256，写入已有实体时必须携带匹配 revision；新建实体不传 id。字段缺省保持原值。

项目：`title,brief,background,progress,configuration,symbols,data_sources,rules,revision,path`。
框架：`id,title,mainline,status(active/candidate/parked),content_ids,chapters,risks,revision,path`。chapters 为自由 Markdown 文本；content_ids 数组保持顺序。最多一个 active。
研究内容：`id,title,objective,revision,path`。
问题：`id,content_id,title,question,why,criterion,outline,status,revision,path`；status 派生，不从用户输入任意改。
状态键：`discussing,pending,running,needs_judgment,review,supported,refuted,inconclusive,not_required`，分别讨论中、待验证、验证中、待判断、需复查、验证支持、验证不支持、证据不足、不需要验证。一次运行成功不自动成为 supported；review/inconclusive 不计入已有科学结论。

## HTTP

GET `/api/health` → `{app:'research-file-system', root, ...}`
GET `/api/state`、`/api/question?id=...`、`/api/content?id=...`
GET `/api/file?path=...` 安全读取；文本源码/图片可预览，其余下载。
POST `/api/action` JSON `{action,...data}`，需请求头 `X-Research-Notebook: 1`。返回操作结果。前端成功后重新读取 state/detail。
POST `/api/stop` 仅相同请求头、loopback。

## perform 的动作和字段

- `save_project`：项目各字段、revision。
- `save_framework`：id 可选、框架各字段、revision。
- `save_content`：id 可选、研究内容各字段、revision。
- `create_question`：content_id,title,question,why,criterion,outline。
- `update_question`：id,revision,问题可编辑字段。
- `save_body`：question_id,text,revision（body_revision）。
- `save_discussion`：question_id,ai,title,raw,summary,understanding,unresolved,parent_id,prompt_id。raw 与带标签保存块至少一个；如果只给摘要不可假称原始对话。P6 可解析【主题】【要点】【遗留问题】，原始粘贴不丢。
- `save_plan`：question_id,title,method(theory/simulation/measurement),target,criterion,inputs,expected,source_ids 数组。追加不可变 P01 等；冻结本轮判据。
- `create_run`：question_id,title,plan_id,target_step,command,parameters,code_paths 数组,mapping（公式代码对照 Markdown）,expected。创建 R01 目录与记录，不运行任何 shell。
- `update_run`：question_id,id,revision,actual,analysis,outcome(pending/pass/fail/partial/error),mapping,parameters,command,code_paths。更新运行记录留旧版本；code_paths 对应本问题下代码；保存快照与哈希防止以后改代码混淆证据。
- `add_code`：question_id,filename,purpose,text 或 base64。拒绝覆盖同名，自动生成版本名。保存用途和归属。
- `attach_file`：question_id,run_id 可选,filename,base64；无 run_id 存资料/，有则存相应结果目录。拒绝覆盖/越界。
- `save_judgment`：question_id,decision(supported/refuted/review/inconclusive/not_required),text,evidence_ids 数组,scope,meaning。supported/refuted 需引用存在的 D/P/R/正文证据；理论允许 D/正文，无需强迫数值运行。not_required 仍需说明理由。对话存在不等于科学正确，由人确认。
- `append_draft`：content_id,question_id,text,revision(draft_revision)；追加人工选择的段落及来源，保留条件与证据，不后台调用 AI。
- `save_draft`：content_id,text,revision。
- `generate_prompt`：question_id 可选,template(P0–P10),focus,source_ids 可选；返回 `{text,id,path}` 并保存不可变上下文快照；生成不等于已发送。
- `sync`：重新扫描文件生成总览，返回 state。

讨论/计划/运行/判断列表元素至少包含 `id,title,path` 及上面动作字段，运行包含 revision；files 项为 `{name,path,size,kind,purpose?}`；路径一律根目录相对 POSIX 路径。

## 提示词接口

`notebook.prompts.TEMPLATES` 为 `{P0:{title,text},...}`。
`build_prompt(template, project, framework, content, question, focus='', sources=None)` 返回字符串；question 是 detail。
`parse_capture(raw)` 返回 `{title,summary,unresolved}`。解析用于预填，保存原文。
`library_markdown()` 返回完整提示词库。

## 界面

正文优先，左树项目/总览与研究路径/框架/研究内容→问题（状态文字）。默认入口为整段粘贴讨论，由 AI 自动理解多问题关系、复用或新建层级；用户一次查看预览并整批归档，无需逐题选归属。项目背景和手动新建入口独立保留。正文读写、讨论原文、计划与运行、代码/文件、判断与草稿，按当前材料给一个主要下一步动作。无需强填“我的理解”。理论/仿真/实测可选，不把失败运行直接判成理论被反驳。框架浏览与当前采用严格分离。文本渲染必须防 XSS，外部源文件不作为可执行 HTML 加载。


## 批量建立与材料摘要（2026-11-08）

state/detail 每个问题提供 artifacts.codes/results/runs/judgments，并在 counts 中添加 codes/results。结果仅计实际输出文件，排除运行记录、计划快照、代码/证据快照；代码不计缓存。所有文件仍属于具体问题，结果提供 run_id，空值表示待关联。新增/变化/移除结果会让旧判断需复查；新的人工判断记录 observed_result_files（路径与 SHA），从而明确它看过的材料版本。

新增 action：
- preview_outline：text、可选 framework_id；只读，返回 outline、counts、warnings、token、framework_revision。Markdown # 为框架，## 为研究内容，### 为问题；说明保留。
- import_outline：上述输入加 token、request_id；仅建立新条目或给所选框架追加内容，保留原文于 04_交接包/目录导入/I_<request_id>.md。返回 framework_ids/content_ids/question_ids/source_path/counts。
- import_materials：question_id、可选 run_id/purpose、files（filename/kind/base64）、request_id；kind 为 code/result/attachment。代码进本题代码/，结果进所选运行目录或结果/待关联/，资料进资料/。不自动创建运行或给出验证结论。

批次先完整校验，逐文件事务备份；失败只恢复本批修改路径，不递归删除任何资料树。同一个 request_id 与同一负载重试返回原收据；不同负载不可复用。界面批次上限 24 MB，服务端总文件上限 32 MB。


## 可读框架目录（2026-11-08）

_refresh_record_indexes 为每个框架追加 generated 附录：研究目录 H2 → 内容 H3 → 问题 H4 → 正文/讨论/代码/结果/结论链接，结果再按运行分组。链接保持相对文件路径、URL编码，原始作者字段不变。返回 save_framework 的结果时，重新读取附录更新后的revision，避免立即再次编辑产生虚假的过期冲突。框架有序引用、问题唯一归属、共享内容来源路径不变。

## 保留的单问题规则归档（2026-11-08，次要入口）

`notebook/capture.py` 提供本地规则识别，不调用模型。`preview_capture` 输入 `text`、可选的 `framework_id/content_id/question_id` 或同级 `*_title`、`files:[{filename,kind?,base64}]`，返回 `plan/files/counts/warnings/needs_choice/can_import/token`。`plan` 包括自然名称归属、正文摘录、候选结论与适用范围。`import_capture` 使用同一输入并增加 `token/request_id`，原子写入，支持同批次重试，返回实体和文件来源信息。

明确名称优先识别，已有子项可推断真实上层；与当前上下文冲突时阻止直接导入并集中确认。未闭合或不识别的代码围栏不参与目录识别。多问题文本只选择本轮主要归属，完整原文保留，不宣称语义自动拆分。

原始输入 UTF-8 字节另存于本题资料目录；代码块提取为独立文件；结果与附件按本题归档，不自动执行，不自动建立运行记录或科学判断。新问题生成讨论摘录正文，已有正文保持不变。候选结论及范围保存在讨论中，前端预填人工确认。网页输入可能将 CRLF 规范为 LF；实际上传附件按原字节保存。

前端目录、标题和选项显示自然名称，实体内部标识及文件路径保持稳定。生成的 Markdown 目录链接标签也使用自然名称。正文、最近讨论、材料按连续文稿显示，单独的讨论/代码/结果页面作为筛选入口保留。

## AI 理解多问题归档（2026-11-08，默认入口）

流程为 `粘贴讨论 → AI 理解并整理 → 预览所有问题 → 全部归档`。不要求用户填写框架、研究内容或问题标签；当前浏览位置仅作为 context 线索。模型根据已有项目背景、层级和问题说明判断续题、新题与材料关联，已有问题即使表述不同也可以复用。

### 模型与输入

`semantic_jobs.py` 使用本机 Codex CLI，通过 ChatGPT 登录调用模型，消耗该账号额度；未登录时不可启动，不回退到 API 密钥付费调用。每次整理由用户明确触发，不定时调用，不自动重发失败请求。

输入包含本轮原文分段、项目背景与配置、已有框架/研究内容/问题说明、附件内容和当前浏览位置。可解码文本附件提供前 18,000 字符并标明是否截断；当前支持最多 4 张 PNG/JPG/JPEG/WebP 图片，每张不超过 8 MB。不可读取内容的文件仅提供元数据，模型不得编造文件内容。上传文件原字节在本地留存，网页文本可能将 CRLF 规范为 LF。

模型只提出受 JSON Schema 约束的方案；关闭 shell、网络检索和其他工具，不运行原文中的代码或指令。原文视为待整理资料。后端对方案中的已有实体、上下层关系、原文段落和材料引用逐项校验；格式有效不等于科学结论正确。

### 异步接口

- GET `/api/semantic-status`：返回 `available/provider/auth/message` 等连接信息。
- POST action `start_semantic`：`text/files/context/request_id`，返回 `job_id/status`。相同请求标识的重复提交不新增模型调用。
- GET `/api/semantic-job?id=...`：读取 `queued/running/ready/failed/cancelled/imported` 状态及消息、预览或结果；恢复信息含原文、上下文与附件元数据。
- POST action `cancel_semantic`：`job_id`，停止该任务，保留输入。
- 主动重试可调用 `start_semantic` 并传 `resume_job_id` 与新的 `request_id`，使用已保存的原文和附件。前端轮询只读取状态，不重新调用模型。
- POST action `import_semantic_job`：`job_id/token/request_id`，一次提交全部问题及材料。重复提交相同批次返回原回执。

登录、额度、超时或模型响应不完整时，任务失败且不生成研究条目；前端保留原文和附件，允许用户明确重试。预览之后目录或项目背景变化时，修订校验阻止以过期方案归档。

### 方案、预览与文件

模型方案为 `frameworks/contents/questions/unassigned_source_ids/unassigned_material_ids/notes`。每个问题包含 `content_key/title/summary/criterion/candidate_conclusion/scope/source_ids/material_ids`，可通过 `existing_id` 复用已有实体。共用原文与材料允许被多个问题引用；每份原文段落和材料必须归入问题或列为未确定归属，不能静默遗漏。

预览返回所有问题的完整路径、摘要、原文依据、关联文件和待归属材料。用户无需逐一选择归属，点击“全部归档”即可。归档先完整校验，再事务写入，失败只回滚本批变化。

本次资料包保存在 `04_交接包/AI整理/<request_id>/`：`讨论原文.md`、`原文分段.json`、`模型整理方案.json`、`原始材料/`、`本轮整理.md` 与 `归档索引.json`。原始材料按原字节保存，保留 SHA256；共用文件在多个问题目录中有对应副本或来源记录，能追溯到同一个批次原件。未知归属的部分保留在资料包中，回执可打开清单，不要求立即选题。

各问题保存本题讨论摘要、相关原文的来源文件、代码及结果；已有正文保持不变，新题正文明确标为 AI 整理。结果未指定具体运行时进入 `结果/待关联/`。归档不运行程序、不自动创建成功运行，也不生成“验证支持”或“验证不支持”的人工判断。候选结论仅保存在讨论中，仍由用户核对后决定。

`P6` 收尾稿属于可选阅读整理，可包含多个问题与共用材料，不要求框架标签。保留三个旧摘要标签只用于兼容手动记录预填，不作为默认 AI 归档的必要格式。既有 `03_提示词/提示词.md` 不会随模板代码自动覆盖，需要单独保留历史后同步。


## 笔记本阅读界面（2026-11-08）

当前页面样式入口为 `static/notebook.css`；旧 `style.css` 不再加载。导航与文件使用真实链接，操作集中在全局收录按钮和页面顶部的编辑/工具菜单。正文区域不渲染编辑、提示词或创建按钮。侧栏只显示框架、内容、问题三级。问题页面连续展示正文、判据、判断、讨论、完整材料，章节链接兼容既有四段 hash 路由。结果按运行自然名称分组，未知运行和未关联材料仍显示。页面标题层级调整只作用于渲染，不修改 Markdown 文件。

主要新函数：`notebookToolbar`、`notebookTools`、`navigationLink`、`documentSection`、`scrollToDocumentSection`。普通文件链接仍由受限文件接口打开；状态标签保持 span，不作为操作入口。后端与语义拆分协议未改动。

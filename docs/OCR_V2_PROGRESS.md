# 第二轮 OCR 重构：进度与审查记录

更新时间：2026-10-06。

当前状态：**OCR V2 实现与静态交付完成，D1—D7 和 A1—A16 已由主代理阅读实际代码后静态接受**。最终多目标恢复的重试绑定返工已落代码并复审。本轮接续同名 active Goal，工作区开始时无未提交改动。主代理仅协调、静态审查和维护记录，所有业务编码及返工由指定子代理完成。交付见 [OCR_V2_DELIVERY.md](OCR_V2_DELIVERY.md)。**未测试、未运行验证**；静态接受不表示真实模型、界面、迁移或编译运行通过。

入口：[Goal](OCR_V2_GOAL.md)。规格：[问题与设计](OCR_V2_PLAN.md)。实现：[编码指南](OCR_V2_IMPLEMENTATION.md)。旧 `WORKFLOW_REDESIGN_*` 为上一轮历史，旧完成状态不延续到本轮。

2026-10-05 补充：按用户要求取消实验性上下文复用，Goal 和编码计划已明确每次识别、复核、修复及重试使用独立上下文，不得重新引入跨页请求历史。来源几何和有依据的书级样式统计保留。本补充不表示 OCR V2 已开发完成，以下工作包及 A 条件状态不变。

2026-10-05 识别优化补充（实施前记录）：粗版面前置、整页与局部高清输入、按类型识别、块级保存/恢复、从源页出发的覆盖判断、PDF 辅助证据、按原因恢复及共享预算写入 Goal、流程和编码指南；取消旧固定调用槽位及失败后才裁切的限制，补充 A13—A16。当时全部 pending；本轮实际接受结果见下表。

## 本次已完成的分析

- 结合截图定位主流程无明确顺序、已处理/成功混淆、技术信息占据主界面等问题。
- 阅读实际上传跳转、任务面板、模型提示词和响应契约、流水线、布局/质量层及导出代码。
- 发现旧提示词与自动路径约束冲突、封面内容分支与全页覆盖要求冲突、内容与精确布局紧耦合、解析错误泛化及原图保留绕经重排文档构造等问题。
- 精确定位截图错误文本在 `latex_export._fidelity_document()` 的缺 layout 分支；没有运行现场响应链，不能断言本次两页最初失败原因。
- 制定文件→选页→识别的新主流程和内容先保存、布局派生、输出独立的后端方案。

## 工作包

| 工作包 | 状态 | 交付/审查备注 |
| --- | --- | --- |
| W00 实施接续与归属 | static_accepted | 已读取规格、当前工作区状态与入口；未发现适用 AGENTS.md；保持文档规定单写者 |
| W01 契约冻结 | static_accepted | 已复读选择/追加来源、响应结算与截断、预算预约、候选/CAS、补做及精确快照；Python/TypeScript 和实际调用一致 |
| W02 内容请求链 | static_accepted | 已阅读 V2 提示/schema、正文先保存、严格解析、协议异常和 D4 新鲜/恢复请求接线 |
| W03 来源与输出边界 | static_accepted | 已阅读来源/裁切/合并/派生、全页内容生成、直接原页 PDF、精确候选缓存和逐格式失败分支 |
| W04 主协调闭环 | static_accepted | 已阅读完整闭环；多目标恢复重试显式绑定具体失败请求，未发送子预约在中断后复用，最后返工已复审 |
| W05 前端与 API | static_accepted | 已阅读上传→选择确认→启动、分页来源、轻量摘要、候选内容、同快照预览下载、完整缺页详情及旧版控制 |
| W06 旧路径收口 | static_accepted | 新旧运行显式分派；增量备份/迁移只编码；旧人工稿和历史记录保留，未执行迁移 |
| W07 逐条件静态审查 | static_accepted | 主代理已阅读实际入口、调用和失败分支，A1—A16 全部静态接受，必要返工结束 |
| W08 文档交付 | static_accepted | 完成最终联接返工及静态接受后创建 OCR_V2_DELIVERY.md，同步本轮状态；明确未测试、未运行验证 |

## 代理与文件归属

| 模块 | 指定模型/推理 | 代理 ID | 状态 |
| --- | --- | --- | --- |
| D1 contracts_storage_v2 | gpt-6.1-sol / xhigh | /root/contracts_storage_v2 | 初轮及最终重试绑定返工已静态审读，静态接受；文件已释放 |
| D2 recognition_v2 | gpt-6.1-sol / xhigh | /root/recognition_v2 | 初轮交付已静态审读；已释放文件所有权 |
| D3 source_layout_v2 | gpt-6.1-sol / xhigh | /root/source_layout_v2 | 初轮交付已静态审读；已释放文件所有权 |
| D4 coordinator_v2 | gpt-6.1-sol / xhigh | /root/source_layout_v2（接续模块） | 实现及最后重试/未发送预约复用已实际审读，静态接受；无活动写者 |
| D5 output_v2 | gpt-6.1-sol / xhigh | /root/recognition_v2（接续模块） | 实现及缓存隔离返工已实际审读，静态接受，文件已释放 |
| D6 frontend_v2 | gpt-6.1-sol / xhigh | /root/contracts_storage_v2（接续模块） | 实现及选择/暂停/人工稿返工已实际审读，静态接受，文件已释放 |
| D7 integration_v2 | gpt-6.1-sol / xhigh | /root/recognition_v2（接续模块） | API/兼容/使用说明已实际审读，静态接受，文件已释放 |
| D5 交付后 Windows 路径修正 | gpt-6.1-sol / xhigh | /root/windows_render_paths | 短目录、短资源名及旧失败缓存处理已实际审读，静态接受；文件已释放 |
| D2/D4 交付后余额错误修正 | gpt-6.1-sol / xhigh | /root/service_balance_stop | HTTP 402 分类、停止派发、已保存错误恢复和提示语义已实际审读，静态接受；文件已释放 |

所有代理不测试、不运行验证、不做 SHA256 等无意义校验；编码代理无 computer use、无下级代理。主代理不参与基础编码。并行上限三个子代理，公共文件保持单写者。

## 静态接受记录

| 轮次/模块 | 实际文件和函数 | 接受/退回 | 原因及具体修改要求 |
| --- | --- | --- | --- |
| D1/D2 接口初审 | `content_contract.parse_content_response`、`CropMapping`、`responses_client._save_v2_body` | 退回后已修正 | 统一 crop 归一化→canonical 归一化；响应身份改用 attempt_id；存储截断状态须向解析传播 |
| D1 存储初审 | `finish_run_attempt`、`save_recognition_response`、`create_output_snapshot` | 退回后已修正并复审 | 分离收到/最终结算；存储截断不能标完整；快照绑定精确修订及已准备来源几何；内容/布局 ID 不可变 |
| D2 模块初轮 | `content_response_schema`、`ContentClientV2`、`_v2_request`、两协议 `_v2_text` | 模块方向接受，整链待审 | 已实际阅读独立提示、分类型 schema、source-only 重读、完整源页复核及异常保存；D4 尚未接线，不能据此接受整体 A 条件 |
| D3 模块初轮 | `prepare_analyzed_page`、`_coarse_regions`、`uncovered_content`、分析资产缓存、`prepare_recognition_inputs`、`merge_region_content`、`derive_layout`、`to_source_fidelity_layout` | 模块方向接受，整链待审 | 已阅读真实来源裁切与可逆坐标、完整源页覆盖线索、同位置内容去重及几何派生；表格不假造网格，内容/编号从内容权威回填；D4/D5 仍待接线 |
| D1 模块初轮复审 | `append_files`、`save_selection`、`_reserve_v2_attempts`、`finish_run_attempt`、`save_recognition_response`、候选保存、`save_page_outcome`、摘要、快照/精确修订读取 | 模块方向接受，整链待审 | 已确认收到/结算分离、截断传播、追加草稿、人工稿 CAS 与本轮候选分离、输入引用不可变、逐格式状态；D4/D6/D7 尚待联接 |
| D5 模块初轮 | `build_v2_latex_document`、`source_preserved_line_ids`、候选缓存/渲染、`_write_source_page_pdf`、`preserve_source_page`、`generate_snapshot_outputs` 与逐格式实现 | 模块方向接受，整链待审 | 已阅读 V2 全页内容统一生成、整组保留及不伪造编号框、直接原页输出、JSON 优先、精确候选引用和完整缺页清单；导出不增加编译，尚待 D4/D7 接入 |
| D1 跨模块返工 | `create_output_snapshot`、`_basic_v2_needs` | 已修正，整链待审 | 跨快照仅允许输入和设置不变的 PDF 复用；含快照身份的 JSON/LaTeX 重建。明确已结算失败的基础职责释放预算保护，不计成功 |
| D4 部分恢复初审 | `reviewed_content`、`content_improves`、继承候选与导出路径 | 退回后已修正并复审 | 独立全页复核可解决已检查目标，保留其他未解决问题；新候选不能新增问题或修改无关可靠块。继承内容的新修订显式转接旧缓存，不重编译已完成页 |
| D7 接线初轮 | 上传逐文件保存、overview/selection/source-pages、摘要/结果/候选、快照下载/errors、旧版分派与清单边界 | 模块方向接受，整链待审 | 已实际阅读路由及失败分支，摘要不传模型正文，下载只读精确快照资产；历史源预览绑定冻结运行及来源版本。并发说明、V2 备份说明返工已交付 |
| D4/D6 整链初审 | `_recover_page_v2`、`_finish_page_v2`、App 导航/保存选择/高级工具 | 退回后已修正并复审 | 持久目标/原因/来源/输入策略字段防止跨轮重复同一失败方案；严重渲染诊断经有限调整后转必要源区域/原页保留。确认选择后才启动，暂停身份保留，运行中新人工稿仍受 CAS 保护 |
| D1/D4 补做复审 | `create_run`、`_inherit_v2_candidate`、`_reuse_inherited_render_v2` | 静态接受 | 输出设置变化使所有继承布局失效，保留内容/复核；设置相同才绑定旧 PDF。旧消费未知保留旧用量，同运行不重发；用户显式建立关联续做运行才授予新预算 |
| D4 输出与源失败复审 | `_output_source_regions_v2`、`_source_read_failure_v2`、`_finish_page_v2` | 静态接受 | 缺字/碰撞等不能因有 PDF 而通过；源替代不增加编译，仍有严重问题则必要原页保留。原始源读取失败与分析缓存/输出写入错误分离，内容引用由 `save_page_outcome` 保留并校验 |
| D5 缓存隔离复审 | `_cached_candidate`、`cache_candidate_render` | 静态接受 | 旧 PDF 绑定不依赖 LaTeX 资源复制或 `.tex` 写入；可选 PNG 缺失降为无预览，PDF 复制/身份保存失败仍明确传播 |
| D6/D7 最终整链 | App、ProjectOrganizer、WorkflowDashboard、api、main 路由、README/usage | 静态接受 | 默认屏仅轻量数据；24 页缩略图与按需内容/详情；幂等启动身份跨刷新保留；模型等待按实际 sent_at，断线保留后台状态；结果引用同一快照，V1 保持显式控制 |
| D1/D4 最后重试返工 | `reserve_attempt`、`_reserve_v2_attempts`、`cancel_unsent_attempt`、`Attempt`、`_request_v2` | 退回后已修正并复审 | 多目标预约后不再取同阶段最后预约：`retry_of_attempt_id` 绑定同运行/页/职责的明确失败请求，继承 blocks/round；同 key 父身份须一致，旧失败不能重复派发子重试；共享额度/未知禁发保持。未发送子预约在操作身份写入中断后复用，取消重试不误退恢复轮；Python/TypeScript 字段已实际核对 |

## A 条件状态

| 条件 | 状态 | 实际阅读位置与接受理由 |
| --- | --- | --- |
| A1 顺序明确 | static_accepted | App `upload`/`savedSelection`、ProjectOrganizer `next`、Dashboard `start`、main `create_run`：上传进入选页；下一步只保存；选择确认后启动才创建任务 |
| A2 一键无人识别 | static_accepted | pipeline `_process_run_v2`→基础识别/复核→有界恢复→布局/渲染→输出，全链无人工批准、坐标或逐页校对等待状态 |
| A3 内容独立 | static_accepted | storage `save_content_candidate`/`save_page_outcome`、main `get_run_content`、Dashboard `ContentView`、compile_service `_snapshot_content_payload`：内容先持久化；无布局仍可读取/导出 JSON；输出错误不清除内容 |
| A4 契约一致 | static_accepted | content_contract 传输 schema/`parse_content_response`、prompts 四种职责、responses_client `ContentClientV2`、layout_solver `to_source_fidelity_layout`、前端 types：按类型严格校验，程序生成 ID，全页类型统一，渲染文字来自内容权威 |
| A5 响应可恢复 | static_accepted | responses_client `_v2_request`、storage `save_recognition_response`/`finish_run_attempt`、pipeline `_saved_result_v2`/`_diagnostics_v2`：有界正文先保存，截断不猜补；新鲜及恢复都本地严格解析，保留阶段/字段/请求诊断 |
| A6 恢复有界 | static_accepted | storage `_reserve_v2_attempts`/`reserve_recovery_round`/`mark_attempt_sent`、pipeline `_request_v2`/`_compile_v2`：统一整轮/单页/共享重试/恢复轮/编译上限，恢复与复核原子预约；多目标重试绑定具体失败 attempt，配置停止，未知消费同运行不重发 |
| A7 输出隔离 | static_accepted | compile_service `_write_source_page_pdf`/`preserve_source_page`/`generate_snapshot_outputs`/缓存绑定：原页 PDF 独立于布局与 TeX，JSON 优先、各格式分别结算；缺失重排候选明确失败，不静默改为源页 |
| A8 状态真实 | static_accepted | storage `_outcome_category`/摘要、pipeline `_finish_page_v2`、Dashboard 四类数量与 `no-text` CSS：处理数独立；保留区域/原页有明确处置；旧稿保护另计；严重诊断影响布局结论，初始/恢复/输出错误并存 |
| A9 简洁可知 | static_accepted | App/Organizer/Dashboard/api 与 overview/source-pages/summary：三步导航，技术与人工工具折叠，默认轻量分页；持久任务/启动身份恢复，轮询断线保留原状态与重连入口，已发送请求等待计时 |
| A10 一致与保护 | static_accepted | storage 选择 CAS、`create_run`、候选保存、`save_page_outcome`、`create_output_snapshot`、main 版本分派：来源/选择/内容/布局/快照引用冻结；人工稿 CAS 与幂等保护；V1 不当作 V2 成功 |
| A11 通用且可维护 | static_accepted | source_analysis、layout_solver、pipeline、main 的实际调用及修改差异：无固定书页特判；分析传给识别/覆盖/派生，源贴图限必要区域；沿用现有 SQLite、PyMuPDF/Pillow 与客户端，无新框架 |
| A12 完整接线与交付 | static_accepted | App→api→main `dispatch_run`→`process_run` V2 分派→D1/D2/D3/D5 实际函数；D1—D7 已实际阅读，最后重试签名/调用/持久字段已复审；进度与交付说明同步，未测试、未运行验证 |
| A13 粗分析与高清输入 | static_accepted | importers `prepare_analyzed_page`、source_analysis 区域/阅读顺序/`prepare_recognition_inputs`/`merge_region_content`、pipeline `_basic_page_v2`：粗分析前置，普通页概览，按需原始高清裁切含完整组与周边，映射可逆，同位置及相同内容才去重 |
| A14 分类型与块级恢复 | static_accepted | `parse_content_response`、quality_service `recovery_targets`/`local_content_candidate`/`content_improves`、pipeline 内容及源保留分支：完整 JSON 有效块独立保存；结构/截断按源区域恢复，旧值绑定且无关块保留；布局错误只处理输出 |
| A15 覆盖与证据 | static_accepted | SourceAnalysis `uncovered_content`、`source_coverage_content`、CONTENT_REVIEW/REREAD 提示及调用：完整源页独立复核允许无候选 ID 遗漏，原生 PDF 文本仅辅助；局部请求不带旧答案；墨迹/一致不独自判通过；Responses store:false/Gemini 当前 contents |
| A16 预算分配与原因路由 | static_accepted | `_basic_v2_needs`/`_reserve_v2_attempts`、`_request_v2`/`_recover_page_v2`/`_render_v2`：基础额度按冻结页序保护；恢复/复核原子预约；暂时重试继承具体目标，不新占恢复轮；错序本地调整，结构/截断/小字/公式/表格局部重读，渲染走本地输出，持久策略防相同失败方案重复 |

没有准确率、实际速度、首次成功率或运行通过结论。未测试、未运行验证。

## 交付后修正：Windows 渲染路径过长

2026-10-06，用户提供渲染阶段 `[WinError 206]`。主代理只读本次既有 `content.json`：两页分别保存了 22 个和 18 个内容块，输出阶段都记录了路径过长；内容恢复阶段另有独立复核未确认改善的回退记录。这些用户已有运行记录是故障定位资料，不是代理运行验证，也不能据此确认真实识别准确率。

指定子代理仅修改 `backend/compile_service.py`。主代理已实际阅读 `_candidate_directory`、`_legacy_candidate_directory`、`_read_cached_candidate`、`_cached_candidate`、`_render_source`、候选绑定/渲染、源区域/原页保留、V1 清单和 V2 快照输出的相关调用，静态接受以下修正：

- 新候选目录使用 `render/<完整修订 ID>/s<设置版本>`，不重复嵌入来源、页面和生成器标识；完整来源、修订、设置与生成器身份仍由 `result.json` 校验。
- 编译使用的声明资源复制为 `a/<序号><后缀>`，只替换派生编译源码的 `includegraphics` 资源参数；权威源码、内容、资源引用和导出包保持原记录。
- 原页保留使用短 `src` 分支，导出原页使用短 `p` 分支；直接原页输出的临时 PDF 名随目标文件确定。
- 旧 `workflow-render` 路径保留只读兼容；旧路径读取抛出 Windows 206 时按缓存未命中处理。新旧缓存中只有无 PDF 且错误明确包含 `[WinError 206]` 的失败记录被跳过，避免旧失败阻止新渲染；有效 PDF 和其他失败缓存保持原语义。

未删除、移动或重写用户旧缓存及既有结果，未放宽局部候选的独立复核采用条件，未增加预算。修正需由后续使用新代码的运行生效，既有冻结结果不自动改写；任意超长工作目录、实际 Windows 文件写入和 XeLaTeX 效果尚未确认。**未测试、未运行验证。**

## 交付后修正：HTTP 402 余额不足

2026-10-06，用户再次提供长路径记录及 `HTTP 402：Insufficient Balance`。主代理只读既有快照 `dea4a98f-05f0-44b7-8a83-face438436c6`：本次两页为 `content=uncertain`、`layout=approximate`、`source_disposition=regions_preserved`，导出目录已有 `document.pdf` 和 `latex.zip`；对应短目录候选记录为 `pdf=true`、`error=null`。报出的两个旧长路径错误在前一快照已存在，由 `inherited_errors` 继承，并非这份记录新增的路径失败。读取用户产物仅用于定位，不表示代理执行或验证了修复。

服务返回 402 后，同一个 `4212…` 请求同时记录为结构错误和缺失内容，另有独立 `c645…` 复核请求；另一页也有多个不同的 402 请求。实际代码的配置错误 HTTP 集合遗漏 402，因此不可重试的当前请求结束后，运行仍能派发后续目标。指定子代理仅修改 `backend/responses_client.py`、`backend/pipeline.py`，主代理已实际阅读并静态接受：

- `CONFIGURATION_STATUS` 与 `_send_json` 把 402 归为不可重试的 `service_configuration`；原始状态及请求身份保留。错误正文保存失败也不丢失已知配置停止原因。
- V1/V2 启动恢复调用 `_restore_configuration_error`，从本运行明确的持久 HTTP 错误恢复停止状态；不因新旧正文可用性不同把已知 402 改称未知消费。
- `_saved_result_v2` 在正文解析前处理已保存服务错误，不把 HTTP 错误信封解析成 OCR 内容。没有明确 HTTP 状态的错误信封按未知消费停止该页新调用，不猜测请求成功。
- `_Unavailable` 的已记录标记与原因类别接入请求、恢复、首次内容和复核分支；底层服务失败不再重复包装成 `missing_content` 或 `invalid_structure`。未发送的阻断步骤明确提示“当前请求未发送”，不复制原始 HTTP 请求身份充当新响应。
- 模型配置停止检查阻止尚未发送的后续请求；恢复环退出后继续本地 `_finish_page_v2` 与格式生成，未发送预约沿用取消机制，已发送/未知消费不退还，不扩预算。独立复核不能完成时仍保留旧候选，不能改成质量通过。

未重写历史快照及旧错误。当前服务的实际余额仍须由用户处理，或改用可用接入；代码不能补足余额。已发送的并发请求可以继续结算，不能由此撤回。**未测试、未运行验证。**

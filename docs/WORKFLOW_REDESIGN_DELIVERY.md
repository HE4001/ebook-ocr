# 无人值守电子书 OCR 重构：交付说明

日期：2026-10-04。交付状态：实现及静态审查完成，C1—C10 均为 `static_accepted`。

**未测试、未运行验证。** 本说明中的接受结论来自主代理对实际代码和差异的阅读，不表示真实模型、界面、数据库迁移或 PDF 排版效果已经确认。

依据：[Goal](WORKFLOW_REDESIGN_GOAL.md)、[流程规格](WORKFLOW_REDESIGN_PLAN.md)、[编码指南](WORKFLOW_REDESIGN_IMPLEMENTATION.md)、[交付条件](WORKFLOW_REDESIGN_ACCEPTANCE.md)、[进度与退回记录](WORKFLOW_REDESIGN_PROGRESS.md)。

## 实现范围

默认流程已改为上传后进入任务总览，一次启动冻结范围、页序、设置、保护策略和调用额度。后台自动推进源页准备、全页识别、本地几何与字体恢复、候选生成、独立全页内容复核、有限修复、自动终态及导出，不依赖用户打开页面或逐项处理疑点。

新增运行、页面任务、不可变修订、评估、问题、物理请求和导出清单的持久化契约。执行状态与内容、布局、覆盖结论分开；历史 `ready`、合法 JSON 和编译成功均不直接转换为自动通过。Responses 与 Gemini 保留显式协议适配，识别、复核及修复使用独立职责与上下文。

无法可靠转录的内容自动保留最佳候选和对应源区域；无法安全形成重排输出时保留必要源页。源图保留标为带问题结果，不计作可靠文字化。源也无法读取或输出缺失时记录失败、缺页和不完整状态，保留已有结果。

源文件、已有人工稿及历史修订受到保护。后台采用使用事务与基准修订比较，不能覆盖运行中新保存的稿件。用户可在启动前明确选择替换人工稿范围；成功采用仍归档旧修订。高级自由 LaTeX、校准与草稿编译保留为可选工具。

终态自动生成冻结导出清单。PDF、LaTeX/资源包和 JSON 使用同一页序、最终采用修订和输出设置，记录原图处置、问题、输出质量及页映射。旧直接源码导出也已补齐引用资源；缺失或越界资源明确报错。

## C1—C10 静态结论

| 条件 | 状态 | 已阅读的代码落点与结论 |
| --- | --- | --- |
| C1 无人工识别介入 | static_accepted | `pipeline.process_run/_process_page/_finalize`、`main` 运行路由与 `App/WorkflowDashboard`：正常、修复、保留及失败分支均自动终结，无人工批准或坐标填写关卡。 |
| C2 自动处理闭环 | static_accepted | `main.dispatch_run` → `Storage.create_run` → `process_run` → 准备/识别/布局/渲染/复核/修复/完成 → `_export`；调用、阶段保存、采用和下载接线完整。 |
| C3 有界资源 | static_accepted | `Storage.reserve_attempt/reserve_compile`、`pipeline._call_model/_render_page/_repair_page` 和两适配器：请求前原子计数，每页最多六个物理请求、共享两次暂时错误重试、一轮模型修复及一次复审、四次候选编译；运行默认上限 3N。未知消费不自动重发，恢复复用已保存阶段。 |
| C4 状态真实 | static_accepted | `quality_service.assess/passed`、`PageReview.require_complete_review`、修订评估与工作台：执行、内容、布局、覆盖、输出完整性和质量分别表达；缺完整源页复核、自然尺寸或诊断覆盖不能凭 PDF 存在标通过。 |
| C5 自动保留不确定内容 | static_accepted | `preservation_regions`、`pipeline._finalize/_source_only_page/_fail_page`、`preserve_source_regions/preserve_source_page/generate_manifest_outputs`：局部保留优先，必要整页保留；保留输出缓存失效可重建；原图不可读保留具体失败原因与缺页，不伪装转录成功。 |
| C6 数据保护 | static_accepted | `Storage` 增量迁移、手工保存、`create_run/adopt_candidate/create_export_manifest` 及高级保存路由：不可变候选、事务 CAS、保护范围和最终指针明确；准备失败仍选择当前稿；旧运行评估不能冒充本轮复核；密钥不进入运行快照，恢复核对冻结地址及协议。 |
| C7 自动工作台 | static_accepted | `WorkflowDashboard`、`App`、`api`、`ProjectOrganizer`：上传后进入总览，任务主动轮询并刷新终态，启动未知时复用幂等身份；结果/问题分页、只读源位置和统一下载可用；高级编辑不是推进条件。 |
| C8 输出一致 | static_accepted | `create_export_manifest/get_manifest_book/get_manifest_pages`、`generate_manifest_outputs`、清单下载路由和 `export_latex`：冻结页序、修订、设置及页映射；资源随源码打包；未采用候选不进入最终稿；子范围导出不替换完整运行入口；降级与部分 PDF 明确呈现。 |
| C9 实现简洁 | static_accepted | 沿用 FastAPI、SQLite、asyncio、React、PyMuPDF/Pillow 和 XeLaTeX；结构化原行/区域/公式组是新流程内容权威，LaTeX 为生成物；图像锁归导入模块，无新增服务、工作流框架、哈希体系、评估工具或依赖。旧启动器及兼容编译缓存机制保留。 |
| C10 按规定交付 | static_accepted | D1—D7 及返工由指定 `gpt-6.1-sol / xhigh` 子代理编码，主代理只协调、阅读实际代码、接受/退回及维护交付文档；具体退回意见与接受记录见进度文件。 |

以上表格记录静态判断，不是运行验收报告。

## 模块交付与归属

所有编码代理均为 `gpt-6.1-sol / xhigh`，未使用 computer use，未派发下级代理。共享文件按所有权交接。

| 模块 | 代理 | 主要文件 |
| --- | --- | --- |
| D1 契约与存储 | `/root/contracts_storage`；最终返工 `/root/contracts_storage_final` | `backend/models.py`、`layout_contract.py`、`storage.py`、`frontend/src/types.ts` |
| D2 来源分析与布局 | `/root/source_analysis` | `backend/importers.py`、`source_analysis.py`、`layout_solver.py` |
| D3 模型三职责 | `/root/recognition_clients` | `backend/prompts.py`、`responses_client.py`、`gemini_client.py`、`model_client.py`、`workflow_model_contract.py` |
| D4 自动流水线与质量 | `/root/workflow_quality_resume`（接续原代理） | `backend/pipeline.py`、`quality_service.py` |
| D5 渲染与保留输出 | `/root/rendering_resume`（接续原代理） | `backend/compile_service.py`、`fidelity_rendering.py`、`latex_export.py`、`latex_diagnostics.py`、`latex_content.py` |
| D6 自动工作台 | `/root/workflow_frontend_resume`（接续原代理） | `frontend/src/App.tsx`、`WorkflowDashboard.tsx`、`ProjectOrganizer.tsx`、`api.ts`、`styles.css`、`workflow.css` |
| D7 接线与兼容 | `/root/integration` | `backend/main.py`、`README.md`、`docs/usage.md` |
| 协调及静态交付 | 主代理 `/root` | 流程状态、进度与本说明；未代写业务代码 |

主代理已处理并阅读返工，包括基础请求低额度分配、字体/公式依据、布局检查不足、派生输出缓存与失败说明、完整性与质量展示、启动幂等、终态更新、旧运行恢复接入、保留资源校准、源保留缓存恢复、失败时新稿保护及兼容源码资源包。

## 设计取舍与使用限制

- 有限处理优先于反复重写：修复没有改善或产生新问题时继续采用较好候选，再自动保留未解决的源内容。额度不足也结束为明确带问题结果，不要求逐页许可。
- 字体与物理尺寸只记录已有证据或估计依据。未知 DPI 使用项目纸宽和原图比例；可靠书级样式摘要仅作为后续页面默认值，不无休止回写旧页。
- 首次正常识别承担能力确认，不增加付费预检。设置检查只核对本地配置；配置错误停止无效模型调用，剩余页可本地保留源内容，但运行保留失败原因。
- 封面书目信息不等于完整几何转录；缺完整布局证据时自动保留源页。复杂图形及不可辨内容可保留为图像，JSON 保留处置说明。
- 暂停、进程中断和发送结果未知会保留阶段与消费记录；主动恢复复用阶段，不重发无法确认消费的请求。运行快照不存密钥，恢复需要相同接入配置。
- 重排候选需要可用的 XeLaTeX、字体和模板组件。缺少依赖时报告原因；源内容可读取时可形成保留源页的输出。完整自定义文档仍依赖其自有宏包和字体。
- 旧直接导出代表调用时当前已保存稿；新默认清单下载代表冻结快照。当前设置或编辑变化不会改写已有清单。

## 未完成项与未验证范围

按本轮定义的编码和静态交付范围，没有待接线模块或已知必须返工项。以下运行事实没有确认，也不作为本轮完成门槛：真实 OCR/公式质量、实际排版与字体效果、界面交互、真实请求恢复、数据库迁移和历史库兼容效果。没有准确率、速度或自动通过率承诺。

迁移与写前 `app-before-workflow.db` 备份逻辑已编码，**迁移未执行**。未启动应用、服务或实现脚本，未试编译、调用模型、探测接口、安装依赖或发布；未执行测试、构建、类型检查、lint、SHA256/指纹验证或浏览器验收。

已有未提交的 Schema/HTTP 错误处理补丁、历史测试和报告予以保留。本轮未新增或扩充测试、样本、人工标注、评估脚本或验收工具，也未以历史结果证明本版效果。

**未测试、未运行验证。**

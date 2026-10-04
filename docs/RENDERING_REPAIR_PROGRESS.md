# 原书版式修复：执行进度

更新时间：2026-10-04（W00—W11 verified）

当前状态：原书渲染 Goal complete（工具已确认）；G1—G7 的隔离验收结论保持。用户追加的识别 HTTP 400 已获得 Schema 拒绝详情，错误详情与引用展开补丁均已完成、静态接受；按用户要求不再测试，实际服务重试尚未验证。

执行入口：[Goal 执行计划](RENDERING_REPAIR_GOAL.md)

详细要求：[修复规格](RENDERING_REPAIR_PLAN.md)

## 当前工作点

- 当前结论：source-r11实际11张/11PNG、一源一张，主代理逐页视觉审查与只读复测退出0、status passed；后端97项+223子测试、前端35项及构建、真实兼容/隔离UI和最终文档审计均通过，G1—G7满足。
- 交付：最终PDF、报告、进度及隔离对照已就绪，无必要门槛剩余工作。应用仍保留27条自然盒版心warning和1条Unicode映射info，quality needs_review；不将独立墨迹与人工验收通过冒充API passed。
- 执行分工：主代理负责需求、调度、审查和运行验收，不参与基础编码；M1—M5 均使用 gpt-6.1-sol / xhigh，所有子代理禁止测试及 computer use，只思考、编码和静态自查。
- 已知工作区：制定计划时存在未提交的后端、前端及文档修改，以及删除记录；执行时重新核查并保留。
- 最终证据：`runs/source-r11/document.pdf`、11张PNG、`review.json`、`geometry-summary-r4.json`；`runs/compatibility-r2/root-review.json`、`runs/ui-r3/root-ui-review.json`和截图；`evidence/backend-final-r3.log`、`frontend-tests-r2.log`、`frontend-vite-r2/r3.log`。tsc-b退出0无独立日志；旧失败与等待人工审查结果均保留。完整索引和误差见[验收报告](RENDERING_REPAIR_VERIFICATION.md)。
- 外部阻塞：pytest 已安装到项目venv；XeLaTeX受默认沙箱路径访问影响，安全提升权限后读取TeX Live 2026版本成功。尚无Goal级阻塞。
- Goal ID：01a0fd97-01fd-79d1-97b2-bda3caae5139，complete；工具确认累计执行15818秒（约4小时24分），无预设token预算。

## 工作包状态

| ID | 工作包 | 状态 | 证据 / 剩余工作 |
| --- | --- | --- | --- |
| W00 | 环境、改动和测试基线 | verified | 初始差异、快照、unittest/pytest与前端失败基线已留档；隔离目录建立 |
| W01 | 问题复现和原因定位 | verified | 旧12张及静默固定盒越界实际复现；自然测量、公式接缝胶和newgeometry字号重置原因已确认 |
| W02 | 诊断与质量状态 | verified | 实际API覆盖纸外字形、缺字、替换字体、异常页数、未知布局与语法失败；未验证未标通过 |
| W03 | 布局契约、迁移及修订 | verified | 22项边界/存储测试与147子测试通过；全量后端97项和223子测试通过 |
| W04 | 校准参考与指标冻结 | verified | 全11原图原行/基线/组/版心及原容差冻结，独立字号与修正依据、最终参考快照留档 |
| W05 | 还原排版、测量和公式组 | verified | source-r11四边/基线/行距及34实际锚点passed，11页复杂数学与内容主代理逐页通过 |
| W06 | 分页、页脚、纸型及合并 | verified | 最终11张/11PNG、一源一张，印刷13为output7；混合5张和源/项目/装订实际视觉通过 |
| W07 | 识别契约和校准闭环 | verified | 两协议固定响应、单次请求及CAS回归通过，实际UI校准/保存及源码脱离；不宣称真实OCR精度 |
| W08 | 原图对照与定位 | verified | 隔离UI实际草稿诊断、高亮、拆行、恢复、保存、156%同步、640px切换、整书定位及顶栏避让；浏览器文件保存未证实 |
| W09 | 草稿、缓存、兼容及导出 | verified | 真实混合ZIP/完整文档/空白页、重复和重排复用、单页修改失效；UI源码脱离和修订保护通过；缓存来源指纹/运行时版本另有回归证据 |
| W10 | 整体回归和视觉验收 | verified | 后端97项+223子测试、前端35项、tsc/Vite全通过；全部11页及最终隔离UI完成 |
| W11 | 文档、报告及完成审计 | verified | 最终报告/进度、八份说明准确，diff退出0；范围及原稿保护审计通过 |

## Goal 完成门槛

| 门槛 | 状态 | 证据 |
| --- | --- | --- |
| G1 内容完整 | 已验证 | source-r11最终11页逐页content/复杂数学/裁切检查passed；原行、文字、编号、上下标、页脚完整 |
| G2 分页正确 | 已验证 | 最终实际11张/11PNG，六项映射检查true，印刷13为输出7且仅一张 |
| G3 版式正确 | 已验证 | 最终几何summary passed；版心、普通基线/行距、34实际公式锚点及逐页视觉达标，原容差未放宽 |
| G4 诊断有效 | 已验证 | compatibility-r2实际故障夹具；静默makebox无Overfull仍报纸外字形 |
| G5 用户流程完整 | 已验证 | 隔离UI对照、诊断定位、同步/窄窗、校准保存、自由源码脱离、草稿失效及质量状态；浏览器保存下载文件路径仍未证实 |
| G6 兼容与数据保护 | 已验证 | 全量回归、两协议固定响应/CAS、实际混合与缓存验收；原库11页字段及原图哈希未改变 |
| G7 可复验交付 | 已验证 | 全量回归/构建、准确文档、最终证据及范围核查完成，diff退出0 |

## 模块派发与代码审查

| 模块 | 子代理任务名 | 模型 / 推理强度 | 派发状态 | 主代理审查决定 |
| --- | --- | --- | --- | --- |
| M1 数据契约与存储 | /root/layout_contracts_resume | gpt-6.1-sol / xhigh | 已交付 | 代码接受，边界/迁移及全量后端通过 |
| M2 排版与诊断 | /root/latex_rendering_resume、/root/sample_spacing_repair、/root/measurement_audit | gpt-6.1-sol / xhigh | 最终已交付 | 核心/私有参考/测量返工接受；真实11页及合成、全部几何和root视觉通过 |
| M3 识别协议与流水线 | /root/ocr_pipeline_resume | gpt-6.1-sol / xhigh | 已交付 | 代码接受；两协议固定响应、未知布局、CAS及人工保护定向/全量回归通过，未调用真实模型 |
| M4 校对与预览界面 | /root/proofing_frontend | gpt-6.1-sol / xhigh | 已交付 | 竞态/滚入视口返工接受；35测试/构建、实际对照/校准/切页/编排UI通过 |
| M5 后端接口与集成 | /root/api_integration | gpt-6.1-sol / xhigh | 已交付 | presence语义、PDF字形检查、旧表迁移修复接受；真实兼容与全量后端通过 |

每次派发补充实际代理 ID、工作包、允许文件及接口版本。共享文件同一时段只有一个写入者。派发和每次返工都重申禁测、禁 computer use、简洁实现及不得防御性编程。

审查记录格式：

| 轮次 / 模块 | 交付范围 | 决定：接受或退回 | 问题与具体修改要求 | 后续主代理验证 |
| --- | --- | --- | --- | --- |
| M1 / 契约初轮 | layout_contract.py、models布局/诊断/map | 接受（仅代码审查，待主代理验证） | 归一坐标、ID/引用校验和程序来源与模型观察分离成立；补原尺寸选项及CAS | 定向schema/迁移测试待最终交付 |
| M2 / 接口 | 生成器/编译单元/自然测量TSV | 接受（接口约定，非功能验收） | 每源页独立单元，完整fidelity生成源码不能误判custom | 缺布局明确校准错误，原行缺失不静默模板替代 |
| M1 / 实现 | 契约、来源变换、事务/CAS/归档/types | 接受（仅代码审查，待主代理验证）；运行发现项退回 | 旧迁移测试夹具缺实际默认；备份sqlite连接须明确关闭 | 38 passed，3 failed包含该项及两项M3夹具；返工临时归M5 |
| M3 / 实现 | 两协议v2、单次请求、待校准观察及CAS | 接受（仅代码审查，待主代理验证）；夹具断言退回 | v1不推断页侧，原fixture unknown不能断言left；明确left输入 | 同上；实际上游OCR未调用 |
| M2 / 运行首轮 | 自然测量/原创合成实际PDF | 退回 | TeX输出分隔符为空格导致TSV失效；公式前缀未含关系符胶而实际锚点错开；newgeometry重置模板行距 | 静态单测15 passed不替代真实编译；要求协议及实际几何验收 |
| M1—M5 / 最终 | 核心代码、接口接线及模块返工 | 接受 | 契约/权威/并发/自然测量/输出映射/UI问题按所属代理最小修复，无样本业务特判 | 后端97+223、前端35、tsc/Vite、实际兼容和UI通过 |
| M2 / source-r11 | 私有原稿参考及测量覆盖 | 接受 | 原文、字号层级、坐标、局部黑体及定义空隙按独立原图；原容差不变 | 全11张/34锚点及root逐页manifest、只读复测通过 |
| 文档 / 最终 | 八份使用/架构说明 | 接受 | 当前接口、质量、纸型与v1/v2准确；5处旧未完成状态交回修正，保留真实OCR/浏览器保存未验证限制 | 主代理读取修订与静态搜索、diff退出0 |

接受代码但尚未运行验证时，明确注明“仅代码审查接受，待主代理验证”。退回后由原模块子代理修改，主代理不代写。子代理交付包含“未测试、未运行验证、未使用 computer use”的声明。

## 实现决策与冻结指标

执行时填写，不将建议值记为实测值：

- Schema：LayoutObservation 1，模型响应v1/v2显式选择；fidelity结构行权威，自由源码保存→custom且保留旧layout；旧书迁移legacy，新书fidelity。
- 坐标：规范图0—1、左上原点，bbox[x0,y0,x1,y1]；baseline归一y。存原源可逆affine；输出affine为规范归一到bp。TeX pt按72/72.27换bp。
- 测量/标注：主代理已看全部11张源图/输出；参考固定原行、基线、组、版心和笔迹排除。原页392.16×576bp；按独立原图汉字步进修正练习9bp、部分正文10⅓bp、章节12bp、页脚10bp。源30标题局部被笔迹覆盖的位置不确定性明确保留。最终PNG全墨迹/实际PDF原点测量和root manifest passed；PLAN 11.2容差未放宽，FRAMES和BASELINES未改。
- 编译器：XeTeX 0.999998 / TeX Live 2026，ICU78.2、HarfBuzz12.3.2，PyMuPDF1.28.2；实际Fandol/LM/CM/RSFS文件指纹与版本见source-r11/runtime-identity.json，无最终缺字/字体替换诊断。
- 隔离位置：`.cache/rendering-repair/`；仅必要书稿字段和PNG，无真实凭据。最终UI使用ui-r3/isolated-data，127.0.0.1:8000/5187，进程与日志留档。

## 未解决问题与阻塞记录

必要门槛无剩余功能阻塞。历史Markdown/HTML断言已按当前行为修订且全量通过，失败日志保留。真实模型OCR精度、P2复杂视觉复刻、浏览器下载文件保存路径未验证；这些与已验证的接口/校准/后端导出分别记录。样本自然盒版心及Unicode提示保留needs_review，不能因人工核对通过伪改API状态。

### 2026-10-03：W00 / W01 启动证据

- 未发现适用 AGENTS.md。保留全部初始改动和删除，补丁保存到 `.cache/rendering-repair/evidence/initial-working-tree.patch`，初始代码快照在 `baseline/`。
- 数据仅从原库 SQLite `mode=ro` 读取 11 页字段和来源记录，复制 11 张 PNG；未调用 Storage 初始化原库，未复制 credentials/settings 表。
- 旧缓存 PDF 12 张，A5 419.53 × 595.28 bp；原书第 7 页旧输出右侧内容裁切，且含后加手写存在量词。
- 后端基线：`python -m unittest discover -s backend/tests -v`，退出 1，19 tests，failures=5/errors=12。pytest 尝试退出 1：缺少 pytest。
- 前端基线：`node --test tests/*.test.mjs`，退出 1，完整结果在 `evidence/frontend-baseline.log`。
- 工具：本地 venv/PyMuPDF、Node/pnpm、Poppler 可用；XeLaTeX 在 `C:/Users/David/AppData/Roaming/TinyTeX/bin/windows/xelatex.exe`。验证进程只用隔离目录与独立端口。

## 迭代记录

### 2026-10-04：页面识别 HTTP 400 跟进

- 用户确认 OpenAI Responses，手动连接测试成功，实际识别页面失败。两种请求的差异是页面图片与完整布局 JSON Schema；当前文本连接测试不验证这些能力。
- 主代理仅用 SQLite `mode=ro`、按字段查询非秘密配置：保存的服务主机为 `api.deepseek.com`，模型为 `deepseek-flash`，推理 `high`，上下文续接关闭，路径 `/responses`；未读取 credentials。官方文档已确认 Responses 和 flash 图片输入受支持，不能按旧知识改协议。
- 两名 `gpt-6.1-sol / xhigh` 子代理分工静态审查 Schema、补充 HTTP 错误详情和 MockTransport 回归；均禁止运行、测试及 computer use。首轮未发现必须拒绝的通用 OpenAI Schema 缺陷，当时尚缺供应商具体错误消息。
- 已确认实现缺陷：服务端 `error.message/param/code` 被丢弃，只显示状态码。诊断补丁保留有限长度的有效文本、脱敏当前密钥，保持请求参数、400 不重试、usage 回调和失败上下文不变；主代理要求总长适配流水线 1000 字符上限。
- 主代理静态接受返工：message 600 字符、param/code 各 150，组合不超过流水线 1000 字符上限。新增 9 项 MockTransport 测试代码，包含失败保存错误详情、旧正文/布局/人工修订及用量保护；子代理没有运行测试。
- 用户随后明确“不要测试了，改完就结束”。主代理立即中断已启动的后端回归，运行会话退出 1、没有取得测试结果；不将其记为通过或用旧 97 项结果代替本补丁验证。不再执行测试、构建、类型检查或运行验收。
- 第一阶段状态：错误详情补丁已完成并静态接受，留在工作区；当时尚未获得拒绝原因，未修改 Schema。未修改协议、推理设置或重试策略，未发起真实模型请求、重启用户服务或写入原库。
- 用户随后提供 `Invalid json schema: field anyOf: missing field type`，`code=invalid_request_error`。静态定位 `layout` 和公式编号等 `anyOf` 分支含裸 `$ref`，分支自身没有 `type`；这与服务端要求显式类型的错误吻合。
- 原 Schema 子代理继续最小修复：先展开固定、无环模型的全部本地引用，再进行既有 Schema 整理；外层 `layout` 嵌入完整对象，不发送 `$ref/$defs`。对象分支显式 `type:object`，空值分支仍为 `type:null`。保留全部必填、禁止额外字段、空值、枚举、长度/范围约束及 `review_reasons=100`，v1 与本地 Pydantic 校验不变；仅同步既有断言的内嵌对象访问路径。
- 主代理静态审查接受上述两文件差异并结束本轮，未运行测试、构建、类型检查、lint、试编译或真实请求。实际服务是否接受更新后完整 Schema 尚未验证；不把静态修复记为真实 OCR 已通过。

### 2026-10-04：用户追加本地合并指令

- 用户在验收后追加“合并”。已验收改动位于main，未附加独立工作树或PR；本轮将源码、测试及说明整理为main本地提交，合并记录以Git日志为准。
- 当前文件清单与最终范围审计一致；私有书稿、数据库、PDF/PNG及运行证据继续留在忽略目录，不纳入提交。前述“未提交”是验收时点记录。

### 2026-10-04：完成登记

- 最终全部门槛审计及diff检查通过后，Goal工具实际返回status complete。W00—W11 verified、G1—G7满足；无后续必要修复待办。
- 保留所有实际失败/待审查阶段记录、最终needs_review提示和已说明的范围限制。主代理交付source-r11 PDF、报告/进度及ui-r3最终对照；未进行付费OCR、原库覆盖或发布。

### 2026-10-04：source-r10/r11最终样本、逐页审查与UI补验

- 主代理静态审查并接受私有坐标、局部印刷黑体和源29定义后quad修正，实际生成source-r11：PDF11张/HTTP PNG11张；初轮退出1仅因等待root审查，自动几何passed。没有改变FRAMES、BASELINES或原容差。
- root逐页看完全部r10输出和原图；r11的10张PNG与r10字节完全相同，唯一不同的源29/output10另行实际查看。逐页content、complex_math_spacing及clipping_overlap均passed，准确覆盖特殊数学行/行距，记录源30标题被笔迹覆盖的不确定性；manifest绑定全部指纹和稳定身份。
- 主代理只读复测source-r11退出0，另存geometry-summary-r4.json、status passed。最大水平四边误差约2.715bp、垂直3bp，普通基线P95各页最大0.000604249em，34实际公式锚点最大0.000253357em，普通行距中位误差浮点精度下为零。极小数值不代表OCR/人工标注亚像素精度。
- API保留27 CONTENT_OUTSIDE_FRAME warning和1 TEXT_MAPPING_INCOMPLETE info，quality needs_review；无最终纸外/缺字/字体替换/异常续页错误。旧自动报告及失败证据不重写。
- 最终ui-r3副本实际更新整书PDF，diag→source26/output7双侧高亮；补验2s网络延迟下页7预览后快速切页8，旧请求200返回后仍source27/output8及已保存整书结果。交换前两文件并确认会失效旧输出，实际重编成功；恢复原序再重编并定位source26/output7。网络延迟已撤销，当前viewport无覆盖，对照保留。
- 真实UI截图final-comparison.webp已落盘并由root查看，root-ui-review.json记录观察与哈希。原库保护复核仍成立，此后仅操作私有副本；没有真实OCR、覆盖原库或发布。
- 八份说明经静态审查，将旧G1/G3未完成状态交回文档代理精确修正，5处状态文字重新审查接受。最终报告列R01—R08、W00—W11、G1—G7、逐页映射/误差、实际命令/退出码、模块审查及限制。最终diff退出0、范围审计通过；书稿、凭据/库及私有快照无Git暂存或跟踪，原有修改/删除保留。

### 2026-10-04：source-r7—r9自动几何与最终人工返工

- source-r7/r8/r9均经实际API编译，11张/11PNG、一源一张映射正确。墨迹版心通过页数由9→10→11；r9自动几何与paper_bounds全部passed，全部未归属墨迹为0。r9首次执行退出1，原因是缺主代理视觉manifest，不能把该退出码记成完整验收通过。
- 源30混排胶仅加到紧邻汉字的行内数学边界，数字间距恢复。r9源30右缘误差1.808bp，符合3.922bp原阈值；全部FRAMES、BASELINES及容差保持。
- 主代理已对照全部11张r9输出和原图：文字、数学符号、上下标、页码及后加笔迹排除成立，无纸外内容。发现少量初始参考左端不准确（21“因此/同理”、23“证明”、22六个小题、24/28/30若干独立公式）及局部印刷黑体遗漏，明确坐标/字族交回M2，仅改私有fixture。独立原图裁片见evidence/final-source-review-details。
- r9应用quality仍needs_review：27项自然盒CONTENT_OUTSIDE_FRAME警告和1项TEXT_MAPPING_INCOMPLETE信息（已绘制非零字形Unicode映射不全），无裁切错误。保留这些诊断，不过滤或擅改passed；独立墨迹指标及人工复核与API状态分别记录。
- compatibility-r2五张混合输出、源/项目/装订纸型的既有主代理视觉结论已另存root-review.json，绑定相关PDF/PNG/ZIP和API报告指纹；原自动运行文件的pending_main_agent字段是当时阶段状态。
- 业务实现无变化，复用已通过全量回归；最终样本、root审查manifest、只读复测及文档审计完成前Goal保持active。

### 2026-10-04：source-r5验收及参考误读纠正

- 后续source-r6真实API编译退出1（geometry_failed），11张/11PNG；源20/21/22/23/25/26/28墨迹四边通过，源23定义符碰撞消除且普通基线覆盖通过。剩余24/27/29/30右缘约-11.195/-6.219/-6.222/+5.308bp。22/23修正较小练习字体为9bp后符合原稿，24原先靠错误练习字号掩盖的正文宽度差异显露。
- 独立放大普通汉字段落，source27两“关”中心121/286.5px相隔8字，平均20.6875px；r6输出中心118.5/278.5为20px。source24、29、30普通汉字亦约20.6—20.7px。授权这四源普通正文统一10⅓bp，增加而非缩小，保留所有几何及容差；24练习9/章节12与各页页脚10不变。撤销source30临时边界胶假说，恢复0pt。测量与裁片保存在evidence/source-r5-visual-details，不以失败宽度反算字体。

- 主代理实际builder退出0；source-r5验收退出1（geometry_failed），PDF11张/PNG11张，稳定一源一张映射。实际墨迹四边通过source20/24/25/26/28；其余21/22/23/27/29/30右缘误差分别约-5.72/+5.80/+26.78/-6.22/-6.22/-12.19bp。全部unmatched为0；source23定义注记与下一行自然盒交叉，仍未验证。
- 放大原图明确纠正先前记录：source23定义符是三角形叠等号（triangleq），并非上/下置def；题6确实有“幂集的…各有多少个”，先前删“的”是误读。source27等价类最后一行同样triangleq，旧转录漏等号。私有代理修复这三项，主代理待审查与编译；原库未改。
- 独立原图列投影确认source23“元素”黑像素span为[71,85]、[89,103]，连续普通汉字约18px步进，而r5为20px。source23及前后承接练习区域应9bp，初始10bp标注待纠正。仅授权source23整正文、source22行17—24、source24行1—5按该源稿依据修正；页脚、框、坐标、基线和所有容差不变，source23的em相应9bp，不能维持10bp放宽。
- source27/29全角右括号advance与墨迹差约6.22bp，源与输出右注位置基本一致。自然盒右边不能证明墨迹右边；不平移标注/扩大框抵消误差，继续核对长混排行及标点字面留白。
- root已经实际查看r5输出21—30中的9页及原图局部；最终逐页content/complex-math/clipping manifest仍未提交，G1/G3/G7保持未完成。此次未改业务代码，不重复已通过回归。

### 2026-10-04：参考修正、独立墨迹测量与接续验收

- 接续核对确认旧子代理已停止；重新派发`sample_spacing_repair`、`measurement_audit`、`docs_current_audit`，均gpt-6.1-sol/xhigh，严格只静态编码与文件审查。主代理保留业务编码禁令。
- 原图复核确认source25独立公式左端和例6等号锚点有误；source26后半命题的T/S/R不能沿用前半例题U/V。该轮对source23的“下置def/删的”是初步误读，后续放大原稿已纠正为triangleq并恢复“的”（见source-r5记录）。这些仅写隔离参考，不改用户书稿。
- 私有测量改用实际PNG全页墨迹判断版心四边，字体框/自然框保留诊断；普通PDF基线、行距及公式锚点指标不变。人工结论绑定run、PDF/参考/PNG指纹及稳定页身份；没有主代理逐页审查不得总体通过。
- 主代理只读复测source-r3退出1，另存`geometry-summary-r4.json`。实际墨迹确认source26四边符合1%，source28四边符合1%；source21/22/23/24/25/27/29/30仍有真实边界差异。2/4个栅格边缘像素的归属待按固定1px footprint复核，不能删墨迹或放宽验收容差。
- 第四轮`source-r4`退出1、compile_failed，source28首行合法控制空格被私有边界清理误拼为换行；已给样本代理明确最小修复要求。失败源码、日志、参考和API结果保留，不能记11页通过。
- 八份使用/架构文档静态修订接受，清除当前六字段和连续模板分组的过时说明，历史记录保留。根代理`git diff --check`退出0（仅换行提示）。
- 前轮自动审批额度耗尽未执行的只读检查已在恢复时间后重新执行成功，当前无审批阻塞。旧UI服务/浏览器已不存在，主代理以`ui-r1`副本启动`ui-r2`，仅127.0.0.1:8000/5187；重新实测整书编译、diag→source30/output11双侧高亮。LaTeX导出返回200、浏览器下载事件仍未返回路径，记录此限制。
- 目标保持active；下一步修复控制空格后第五轮实际11页，完成墨迹覆盖和逐页内容/复杂数学审查。

### 2026-10-03：实际11页、兼容、回归与界面验收

- `source-r2`通过真实上传/确认/11页校准/整书编译API输出11张；源码、PDF、PNG、原参考快照与page_map留在`runs/source-r2/`。质量为needs_review，56项诊断，不能记版式完成。原印刷13稳定对应输出7。
- 首轮私有量词缺空格导致undefined existsx已交M1修正。M2的`--measure-existing`只读测量退出1，另存`geometry-summary-r3.json`；可测普通基线P95最大约0.00061em，行距误差接近0，但混排自然右缘可超原版心23.5bp、部分大并交及括号样式不符，部分数学行/悬挂标点测量覆盖待修正。保持原容差，不缩字号或扩版心。
- M5实际兼容`compatibility-r2`退出0：4源页混合输出5张，其中完整自定义文档200×280bp占2张；源码ZIP保留原宏包和内容，空白页正确，重复/重排复用单元缓存、修改只重编对应单元。源纸型/项目A5/装订输出quality均passed。主代理已看双栏、字体强调、表格、页眉页脚、模板和完整文档输出，后续补其余空白和纸型视觉记录。
- RSFS原离散字号引发真实tabular字体替换，M2用原5/7/10光学设计的连续尺寸声明修正，无滤警告。完整文档小纸型默认页码纸外是私有夹具本身错误，M5明确pagestyle empty修正；未改用户文档。
- 故障API真实覆盖缺布局/未知坐标、零宽makebox静默纸外、缺字、字体替换、语法错误及模板2张异常续页；对应compile_failed/needs_review/unverified，没有误报passed。PDF非零glyph但U+FFFD仅表Unicode映射未知，保留info/partial与unverified，不伪称缺字或通过。
- 后端全量`pytest backend/tests --basetemp <新绝对隔离目录> -q`退出0：97 passed、223 subtests passed、2项第三方弃用警告，日志`evidence/backend-final-r3.log`。此前临时目录权限/父目录未创建造成的9项导入错误由主代理验证命令修正，未改业务绕过。
- 前端初轮34项有3失败；纸型保存后detail失败导致processing被ready覆盖的竞态交M4最小修正，另外两项VM夹具补缺字段。返工35/35通过，tsc-b及Vite生产构建退出0，日志`frontend-tests-r2.log`、`frontend-vite-r2.log`。tsc-b无独立日志；此前frontend-build-r2.log索引有误，已纠正。
- 隔离UI使用`runs/ui-r1/isolated-data`副本，127.0.0.1:8000/5187；只主代理使用浏览器。已实测60字静默越界提示、草稿r+1诊断定位、原图/输出高亮、编辑清旧结果、拆行为5行保留全部字符且几何未知、恢复bbox、校准保存生成源码r3/r3。继续验收缩放/窄窗、源码脱离与整书定位。
- G1/G3/G5/G6/G7仍需最终证据，目标保持active；未访问真实模型，原库及已有稿件未写入。
- `source-r3`实际编译退出1（geometry_failed）：仍11张，全部普通CJK基线/行距覆盖及公式锚点通过，source20版心通过；其余版心仍需按原图校正多余显式空格/数学glue，source25右缘偏短，不能统一缩内容。末页大运算符碰撞已消除，source23对称差定义的上置def须按原图改下置，旧run与参考快照保留。
- UI新增验收：自由源码保存后custom_latex、内容r4/布局r3；旧结构保留不覆盖源码。整书diag→编排11/output11后PDF保留；156%双侧页内scroll一致，640px窄窗原图/输出切换保持注册坐标，重置viewport。点击诊断外层仍在列表的问题退回M4用最小scrollIntoView+72px顶栏留白修复，主代理实测比较区top=72.225px且直接可见，生产构建r3退出0。
- 浏览器LaTeX下载点击后API export.tex返回200，但IAB下载事件等待超时、未取得文件路径；真实后端ZIP导出已通过，不将浏览器保存文件记为已验证。
- 原库保护终检：主代理SQLite URI mode=ro比较初始11页全部字段，changed_source_pages=[]；11原图哈希均未改变。证据`evidence/original-preservation-check.json`，未读取凭据/settings。

### 2026-10-03：首轮主代理运行验证及模块接线

- M1/M3已交付并声明未测试、未运行、未CU。M4/M5依槽位并行接入，均gpt-6.1-sol/xhigh；未要求任何子代理运行验证。
- 主代理定向契约/存储/协议/流水线测试：38 passed、3 failed、178 subtests passed。失败为迁移夹具默认值/SQLite备份连接未关闭及两协议v1夹具页侧预期；具体返工交回编码代理。默认沙箱Python启动无输出，停止该验证进程后安全提升权限执行成功，未触及原库。
- 主代理排版/诊断定向单测：15 passed（0.16s），日志`evidence/renderer-unit-tests.log`。真实合成编译退出0、1页，但实测发现自然尺寸分隔符失效及首公式实际对齐偏差，已退回M2，未将单测通过记为版式通过。
- 主代理执行私有参考builder退出0，生成11页`reference/calibration.json`，正文行数按人工标注；尚待输出逐页核对。
- 主代理最小行距探针编译退出0。日志确认`newgeometry`把`baselineskip`从17.666pt（17.6bp）重置12.64725pt（12.6bp）；PDF文字原点证实。字族/字重切换不引发该重置。M2负责最小修复，保留旧书稿。
- PDF技能操作标记已在首次创建前成功执行一次。实际产物和日志均位于隔离`.cache/rendering-repair/`；原库未初始化、未写入、未复制凭据。
- G1—G7仍未满足全部证据，目标保持active。下一步：运行返工验证，完成API缓存/PNG及界面后执行11页实际导出和UI验收。

### 2026-10-03：接续状态核对及接口冻结

- 前轮留下了可用的基线证据、源图副本和部分布局契约，属于实际进展。接续时live代理列表仅root，旧编码任务不再运行；没有把历史handle误当仍在运行的任务等待。
- 重派M1/M2/M3，仍全部gpt-6.1-sol/xhigh，只静态编码，禁测试及computer use。M4/M5依并发槽位接续。
- 必要pytest依赖由主代理安装（退出0）。隔离基线 `pytest backend/tests -q --disable-warnings` 退出2：test_core缺PageResult、test_page_side和test_special_page_agent缺旧特殊页提示接口，均与既有旧断言相关。
- 主代理追加11页人工reference说明。所有门槛仍未验证，尚未创建虚假的验收报告。

### 2026-10-03：准备执行计划

- 完成：建立 Goal 入口、执行工作包、完成门槛和进度模板。
- 验证：仅文档一致性检查；未执行代码修复、应用测试、模型请求或数据迁移。
- 下一步：用户启动 Goal 后执行 W00。

### 2026-10-03：加入用户指定的子代理开发方式

- 完成：确定五个编码模块、共享文件归属、并行顺序、派发模板及审查退回流程。
- 固定要求：所有子代理使用 gpt-6.1-sol / xhigh；禁止测试和 computer use；简洁编码，不过度设计或进行防御性编程。
- 主代理职责：只提出要求、协调、审查并决定接受或退回，统一执行必要运行验证，不参与基础编码。
- 当前状态：仅更新计划；未创建子代理、未启动 Goal、未执行代码修复或测试。

后续每个工作单元追加：日期/工作包、变更摘要、验证命令与退出码、证据路径、未解决问题、下一步。同时更新上方当前工作点及状态表，不能只追加叙述。

# 原书版式与 PDF 修复验收报告

日期：2026-10-04（Asia/Singapore）。原书渲染验收结论：G1—G7 全部满足，W00—W11 verified。Goal 完成登记见执行进度；随后用户报告的真实识别 HTTP 400 已补错误详情并静态修正 Schema 引用，按用户要求未运行验证，不包含在固定响应与隔离渲染通过结论中。

11 个校准源页经应用实际上传、保存布局、编译、合并及 PNG 接口输出 11 张。主代理逐页核对文字、原行、公式组、编号、上下标和页脚，未发现裁字、异常续页或无依据的块重叠。所有冻结的几何指标通过。样本 API 质量仍为 `needs_review`，保留 27 条自然盒版心警告和 1 条文字映射提示；独立墨迹测量和人工验收通过不改变应用质量状态。

基础编码由多个 gpt-6.1-sol / xhigh 子代理分模块完成，依赖允许时并行。全部子代理只思考、编辑和静态自查，并声明未进行测试、构建、类型检查、lint、试编译、运行验证或 computer use。主代理提出要求、协调接口、审查并决定接受/退回，统一执行必要测试、编译、界面验证和本报告；未代写业务或验收工具实现。

## 交付与证据入口

以下路径均相对项目根目录；私人书稿附件保存在忽略目录中，不加入提交。

| 产物 | 路径及用途 |
| --- | --- |
| 最终 11 页 PDF | `.cache/rendering-repair/runs/source-r11/document.pdf` |
| 11 张实际接口 PNG | 同目录 `output-01.png`—`output-11.png` |
| 最终几何及完成结论 | 同目录 `geometry-summary-r4.json`，主代理只读复测退出 0、`status: passed`；文件名 r4 是工具的固定后缀，不代表 source-r4 |
| 主代理逐页视觉记录 | 同目录 `review.json`，绑定 run、PDF、fixture、PNG 指纹与稳定源页身份 |
| 应用编译结果 | 同目录 `compile-result.json`，保留原始诊断与 `needs_review` |
| 可复验参考与工具快照 | 同目录 `calibration.json`、`REFERENCE_NOTES.md`、`reference-builder.py`、`acceptance-harness.py` |
| 编译器与字体身份 | 同目录 `runtime-identity.json`；实际缓存的引擎、字体文件 SHA256、文件元数据及排版版本 |
| 通用合成验证 | 同目录 `synthetic-checks.json`、`synthetic-alignment.pdf`、`synthetic-overflow-result.json` |
| 兼容验证及视觉补签 | `runs/compatibility-r2/compatibility.json`、`root-review.json`（均在 `.cache/rendering-repair/` 下） |
| 原库保护 | `.cache/rendering-repair/evidence/original-preservation-check.json` |
| 最终 UI 对照 | `.cache/rendering-repair/runs/ui-r3/final-comparison.webp`、`root-ui-review.json`；完整过程亦见进度和主代理浏览器操作记录 |
| 执行进度 | [RENDERING_REPAIR_PROGRESS.md](RENDERING_REPAIR_PROGRESS.md) |

最终 PDF SHA256：`581d1e288562cc99d7b9e3462cdf2a6a5ee9e0221f1324cf5aad8f0e965bccbe`。参考 SHA256：`28ccc198654104364bde9a87941cb464b22800fa5027409e95c8cb0e1df04f0c`。隔离样本项目 ID：`9036812d-14ae-49c5-97b9-c1f8079853ec`。

`source-r11/acceptance.json` 和首次 `geometry-summary.json` 保留当时的 `awaiting_main_agent_visual_review`，首轮命令退出 1。最后提交逐页审查后，只读复测另写 `geometry-summary-r4.json` 并退出 0，未修改历史失败结果。兼容自动报告的 `pending_main_agent` 也保留，其最终人工结论另见 `root-review.json`。

## G1—G7 完成门槛

| 门槛 | 结论 | 主代理证据 |
| --- | --- | --- |
| G1 内容完整 | 已通过 | 全 11 张原图/输出逐页查看；`review.json` 每页 content、复杂数学及裁切/碰撞均 passed；实际 PDF 和全部 HTTP PNG |
| G2 分页正确 | 已通过 | PDF 实际 11 张，接口 PNG 11 张，映射六项检查全 true；源 26 / 印刷 13 恰为输出 7，没有重复页码 |
| G3 版式正确 | 已通过 | 全页墨迹四边、普通基线/行距及 34 个实际公式锚点通过冻结容差；公式组原行、顺序、特殊留白、局部字族经视觉检查 |
| G4 诊断有效 | 已通过 | 静默固定盒零 Overfull 仍检出纸外字形；缺字、字体替换、未知布局、语法错误和意外 2 张实际故障验证；未覆盖不标 passed |
| G5 用户流程 | 已通过 | 主代理实际操作同页对照、高亮/滚入视口、校准/拆行/保存、草稿失效、自由源码、整书定位、同步/窄窗、快速切页旧请求返回及编排变化 |
| G6 兼容与保护 | 已通过 | 两协议固定响应、迁移/重复初始化、CAS及人工保存保护回归；实际模板/完整文档/空白/混合 ZIP、纸型/装订与缓存；原库字段/原图无变化 |
| G7 可复验交付 | 已通过 | 后端 97 项及 223 子测试、前端 35 项、tsc-b 和 Vite 构建退出 0；准确说明/报告、证据及范围审计完成，`git diff --check` 退出 0 |

## 11 页映射、视觉与误差

原项目 `cc21eed6-8ba1-45db-a307-4235e44cc372`，来源 `fb238b21-7277-4a67-9377-049a197754fc`。原 PDF 源页 20—30，印刷页 7—17；每个源页 392.1600036621094 × 576 bp，规范 PNG 785 × 1152 px。隔离上传后每张 PNG 是各自来源的第 1 页，报告的“原源页”用于追溯原书，不冒充上传来源页码。

表中正文原行数不含单独的印刷页码。四边为实际墨迹相对原标注的有符号误差（左/上/右/下，bp），均通过；各页页脚完整且只出现一次。

| 原源页 | 印刷页 | 输出页 | 正文原行 | 四边误差 bp | 逐页重点核对 |
| --- | --- | --- | --- | --- | --- |
| 20 | 7 | 1 | 28 | +0.055 / 0 / −0.196 / +1 | 例 7 原断点、量词次序、两组三行推导；后加存在量词/手画线排除 |
| 21 | 8 | 2 | 28 | +0.030 / −1 / +1.281 / +1 | 否定推导、四条短续行、因此/同理缩进、例 8 |
| 22 | 9 | 3 | 24 | +0.052 / −3 / −2.195 / +0.5 | 高分式、附注和习题印刷黑体、六小题与末行 |
| 23 | 10 | 4 | 26 | +0.030 / −0.5 / −2.218 / 0 | 并/交上下限、≜ 定义符、题 6 原文、题 7 空白引号 |
| 24 | 11 | 5 | 25 | +0.053 / −1 / −2.195 / +1 | 承接习题、§2、Dom/Ran、父/妻关系式原落点 |
| 25 | 12 | 6 | 27 | +0.536 / −0.5 / −2.715 / +2.5 | R/U/V、右花括号、逆/复合关系两公式组、原断点 |
| 26 | 13 | 7 | 26 | +0.051 / 0 / −2.699 / −1 | 4/3/2/7 行四组，等号/双箭头、条件续行、T/S/R 与末两行 |
| 27 | 14 | 8 | 27 | +0.030 / −1 / +0.781 / +0.5 | 三个右注、k,s∈Z、四行等价类与 ≜、定理及末条件 |
| 28 | 15 | 9 | 28 | −0.444 / −1 / −0.694 / +0.5 | 证明黑方块、商集、例 7/8/9、粗体 N/Z、印刷上横线 |
| 29 | 16 | 10 | 25 | −0.475 / −0.5 / −2.222 / 0 | 分式/上横线、序关系右注、末短续行、定义后原一字空隙 |
| 30 | 17 | 11 | 24 | +0.055 / −0.5 / +1.808 / +1 | 首段三行、例 11 原句点、习题/题 1、末四行 Dom/Ran 并交组 |

主代理先查看全部 source-r10 输出和原图。r11 仅修订源 29 定义后的印刷空隙；10 张 PNG 字节与 r10 完全相同，源 29 的 r11 输出另行实际查看。此复用关系逐页写入 `review.json`，未用未看的新图替代已看的版本。源 30 的“习”部分被后加笔迹覆盖，标题左端按可辨“题”字及原区域估计，明确保留这一标注不确定性。

| 指标 | 冻结门槛 | 最终实际值 |
| --- | --- | --- |
| 版心左右边 | 页宽 1% = 3.921600 bp | 最大绝对误差约 2.715 bp |
| 版心上下边 | 页高 1% = 5.76 bp | 最大绝对误差 3 bp |
| 普通基线中位误差 | ≤ 0.25 em | 各页最大 0.000402833 em |
| 普通基线 P95 | ≤ 0.5 em | 各页最大 0.000604249 em |
| 显式公式锚点 | ≤ 0.5 em | 34 个实际锚点通过，最大 0.000253357 em（约 0.002534 bp） |
| 普通行距中位相对误差 | ≤ 5% | 各页最大约 3.56×10⁻¹⁵，浮点精度下为零 |
| 纸外可见内容 | 零容忍 | 实际纸外诊断为零，视觉无裁字 |
| 未归属墨迹/正文与页码区域交叉 | 不得隐藏未验证内容 | 11 页均为零；特殊数学及块间碰撞由主代理逐页核对 |

基线参考来自独立原图人工标注，像素依据约有 ±2 px 不确定性。上述极小误差证明生成器遵循校准坐标，不是 OCR 或人工标注有亚像素精度。无显式关系符的 4 条组内续行记 `not_applicable`，不计作已测锚点，由逐页数学视觉记录覆盖。

### 测量方法与诊断解释

PDF 普通文字原点与实际关系符原点，经输出 affine 逆变换回源画布后比较；统一纸型缩放先消除，TeX pt 按 72/72.27 转 bp。最终版心使用实际 PNG 的全部非白 RGB 墨迹，正文归属采用实际字形盒与自然盒的并集，仅排除已确认印刷页码。固定 1px mask 足迹只处理整数栅格舍入与抗锯齿，不是验收容差；未归属墨迹不忽略，也不按原版心裁掉再测。

所有 FRAMES、BASELINES 和规格 11.2 容差保持，未为失败样本放宽。字体仅按独立原稿连续汉字步进修正区域字号：源 23 及相邻承接练习区 9bp；源 24/27/29/30 正文 10⅓bp；源 24 章节 12bp；页码 10bp。没有逐行缩放、删字、裁切、扩大参考版心或把整页扫描图贴入 PDF。

API 的 27 条 `CONTENT_OUTSIDE_FRAME` 采用自然尺寸盒/字符 advance，与冻结的可见墨迹四边指标不同；最终无纸外错误。输出 3 的 28 个已绘制非零字形存在不完整 Unicode 映射，保留 1 条 `TEXT_MAPPING_INCOMPLETE` / partial 信息，不能误判为缺字，也不能宣称全文自动文字覆盖完整。主代理对照原图确认可见内容完整，API 继续显示 `needs_review`。

## R01—R08 处理结果

| 问题 | 结果与证据 |
| --- | --- |
| R01 长句裁切 | 已修复核心样本；结构化原行、自然尺寸和实际字形纸外检查，11 页无裁字 |
| R02 11 页变 12 张 | 已修复；逐源页独立输出/合并，11 张且印刷 13 只一张，稳定映射 |
| R03 没有 Overfull 仍裁切 | 已修复漏报；合成 60 字固定盒零 Overfull，真实诊断检出约 330bp 纸外；兼容零宽 makebox 也检出 |
| R04 连续推导漂移 | 已修复；显式公式组、实际关系符前缀胶修正，原组行数/顺序和 34 锚点通过 |
| R05 密度与位置偏差 | 核心样本通过；原画布/版心/基线、字号层级与统一纸型变换，保留位置不确定性说明 |
| R06 后加笔迹混入 | 校准集及固定响应验证通过；手写量词/圈线排除，印刷下划线/上横线保留；未验证真实新 OCR 的识别精度 |
| R07 原图另窗 | 已完成同页对照、双侧诊断高亮、页映射、同步缩放/位置及窄窗切换 |
| R08 封面/复杂图形 | 保留书目兼容和不确定区域；完整视觉复刻属于 P2，未宣称完成，见限制 |

## 实现与工作包结果

三策略明确分开：新书默认 `source_fidelity`，旧书保持 `legacy_template`；还原布局为内容权威，自由源码实际修改保存后进入 `custom_latex`，保留旧布局与归档。LayoutObservation 1 与响应 v1/v2 明确版本，未知信息不伪造，来源/修订/指纹由程序维护。单源页独立编译单元的缓存与按编排合并的缓存分离，完整文档保留自己的宏、纸型和多页；质量、诊断、输出范围及 affine 随修订返回。

| 工作包 | 结果 | 对应证据 |
| --- | --- | --- |
| W00 | verified | 初始补丁/代码快照、失败基线、私有隔离与原库只读记录 |
| W01 | verified | 旧 12 张复现、静默越界、公式接缝胶及 newgeometry 行距探针 |
| W02 | verified | 真实故障诊断与四质量状态，未验证不报通过 |
| W03 | verified | 契约/迁移/修订边界回归及全量后端 |
| W04 | verified | 全 11 原图校准参考、修正依据、冻结原指标及工具快照 |
| W05 | verified | source-r11 几何/视觉、公式锚点与通用原创合成 |
| W06 | verified | 11 张映射、原页码、空白/完整双页、源/项目/装订输出 |
| W07 | verified | 两协议固定响应/单次请求/CAS、校准与源码脱离实际操作 |
| W08 | verified | 主代理隔离 UI 对照、定位、同步/窄窗、快速切页 |
| W09 | verified | 草稿及并发回归、实际缓存重排/修改、混合 ZIP、旧数据备份/归档 |
| W10 | verified | 后端 97+223、前端 35、构建、全部 11 页视觉及最终 UI |
| W11 | verified | 八份当前说明、本报告/进度更新与范围审计完成；diff检查退出0，私有书稿及库无待提交文件 |

### 模块交付及审查

| 模块 | 代理及模型 | 主代理结论 |
| --- | --- | --- |
| M1 契约与存储 | `layout_contracts` / `layout_contracts_resume`，gpt-6.1-sol / xhigh | 接受；坐标/版本/来源权威/CAS/旧策略及增量迁移，通过边界和全量回归 |
| M2 排版与诊断 | `latex_rendering` / resume、`sample_spacing_repair`、`measurement_audit`，同模型/强度 | 接受；自然测量、原行/组、单源页、实际全 PNG 墨迹及 root 审查门槛；最终实编/视觉通过 |
| M3 识别及流水线 | `ocr_pipeline` / resume，同模型/强度 | 接受；两协议显式 v2/v1、单次请求、未知布局与人工修订保护；固定响应回归通过 |
| M4 校对界面 | `proofing_frontend`，同模型/强度 | 接受；布局保存竞态与诊断滚入视口返工后，35 测试/构建和实际 UI 通过 |
| M5 接口及集成 | `api_integration`，同模型/强度 | 接受；省略/null 语义、缓存/合并/导出、实际字形检测及迁移兼容，通过 API/TeX 及回归 |
| 当前说明审计 | `docs_current_audit`，同模型/强度 | 八份文档按最终接口和状态修订；主代理静态审查接受后完成文档审计 |

主要退回记录：自然测量 TSV 分隔符为空导致读数丢失；公式前缀未含关系胶；newgeometry 重置行距；RSFS 离散尺寸引发替换；迁移备份连接关闭及 v1 夹具预期；字体字形/Unicode 误归类；排版保存响应竞态；诊断未滚入视口。各项由相应子代理修正，主代理运行验证。私有参考另返工纠正原文/定义符、字体区域、数学样式、缩进与印刷标签；未在业务代码加入样本 ID/原页号/原文特判。完整过程见进度中的逐轮记录。

## 主代理执行命令与结果

所有运行均由主代理执行。后端测试以独立 `EBOOK_OCR_DATA_DIR` 和隔离临时目录运行；实际 API 工具自行创建各 run 的独立数据库，不初始化用户原库。命令中的相对路径以项目根目录为工作目录。

| 执行命令或操作 | 退出 / 实际结果 | 记录 |
| --- | --- | --- |
| `.venv/Scripts/python.exe -m pytest backend/tests --basetemp <工作区绝对路径>/.cache/rendering-repair/pytest-temp/backend-final-r3 -q` | 0；97 passed、223 subtests passed，14.18s；2 个第三方弃用警告 | `evidence/backend-final-r3.log` |
| `node --test tests/*.test.mjs`，工作目录 frontend | 0；35/35，约 141ms | `evidence/frontend-tests-r2.log` |
| 前端 `tsc -b`、`vite build`（分别调用已安装工具，等价 package build） | 均 0；tsc 无输出；Vite 8.3.0、34 模块、372ms | `evidence/frontend-vite-r2.log`、`frontend-vite-r3.log`；没有 `frontend-build-r2.log` |
| `.venv/Scripts/python.exe .cache/rendering-repair/reference/build_calibration.py` | 0；11 页私有参考 | `evidence/reference-build-r11.log` |
| `.venv/Scripts/python.exe .cache/rendering-repair/run_acceptance.py --run-dir .cache/rendering-repair/runs/source-r11` | 1；实际编译 11 张/11 PNG，自动几何已通过，等待 root 逐页审查 | `evidence/source-r11.log`、run 快照 |
| `.venv/Scripts/python.exe .cache/rendering-repair/run_acceptance.py --measure-existing .cache/rendering-repair/runs/source-r11 --review-manifest .cache/rendering-repair/runs/source-r11/review.json` | 0；最终几何、映射、纸外及 root 视觉均 passed；不编译、不写库 | `evidence/source-r11-final-review.log`、`geometry-summary-r4.json` |
| `.venv/Scripts/python.exe .cache/rendering-repair/run_compatibility.py --run-dir .cache/rendering-repair/runs/compatibility-r2` | 0；实际 API/TeX/导出/缓存断言通过；人工结论另补 root-review | `evidence/compatibility-r2.log`、`compatibility.json`、`root-review.json` |
| 主代理隔离浏览器操作 | 同页对照、源码/校准、诊断、缩放、窄窗、异步/编排等实际通过 | `runs/ui-r1`—`ui-r3`、浏览器操作记录和最终截图 |
| 原库必要字段 `mode=ro` 比对及原图 SHA256 | 11 页字段无变化、11 图无变化，未读凭据/settings | `evidence/original-preservation-check.json` |
| `git diff --check` 与变更范围核查 | 0；仅LF/CRLF提示；已有修改/删除保留，无暂存文件，私有书稿/库不在Git跟踪范围 | `evidence/final-diff-check.log`、`evidence/final-scope-audit.json` |

首轮历史 Markdown/HTML 假设造成的后端/前端失败均留存基线，不删除失败测试换取通过。迁移/协议夹具、前端竞态、实际排版问题按模块返工；最终全量通过。私有 r3/r5/r6 等几何失败、r4 控制空格编译失败及缺视觉审查退出 1 也保留，不能冒充成功。默认沙箱运行时路径受限的失败和 pytest 临时目录父级错误由主代理调整隔离验证命令解决，未改业务逻辑绕过检查。

XeTeX 0.999998 / TeX Live 2026、ICU 78.2、HarfBuzz 12.3.2、PyMuPDF 1.28.2；实际 Fandol Song/Hei/Kai/Fang、Latin Modern、Computer Modern 和 RSFS 文件指纹见 runtime-identity。兼容实际 PDF 也验证宋体/黑体/楷体、局部粗体/倾斜、印刷下划线、简单表格、双栏和页眉页脚。未通过修改字体文件来演练缓存失效；来源图片实际改变和运行时身份改变的回归已通过，不扩大实验声明。

## 实际用户流程与回退

1. 旧项目继续用现有模板，已有人工源码继续使用；缺布局不自动重识别。要还原，进入原书布局校准，按原图补原行、区域、基线与公式组，未知位置保持未知。
2. 预览校准草稿，查看同页原图/输出及质量。点击诊断定位双侧区域；检查字号、原行断点与公式关系后保存校准，按双修订提交并生成源码，再生成整书 PDF。
3. 自由源码实际修改保存后使用自定义源码，旧布局仍保留。完整文档继续自己的宏包、纸型和分页；不把旧布局重新覆盖到源码上。
4. 内容/布局/纸型/编排变化使旧预览失效。实际 UI 中交换前两源文件、保存后结果变为“尚未生成”，重新生成可下载；恢复原序后重编，并重新定位源 26 / 输出 7。2s 网络延迟下启动第 7 页校准预览后快速切第 8 页，旧请求 200 返回后仍显示 source-27 / 输出 8 和已保存整书结果；验收后恢复网络设置。
5. 需要回退渲染时显式选择模板或自定义源码，保留布局供再校准。迁移写前保存 `app-before-latex.db` / `app-before-layout.db`，旧正文与修订内部归档保留；当前无一键历史恢复界面。数据库恢复须停止服务，保留现库和备份，在独立目录用相容版本核对；备份之后的编辑不会出现在旧备份中。本次验证隔离迁移、重复初始化、备份和策略兼容，未恢复或覆盖用户原库。

多源 LaTeX 导出为 ZIP，单源为 TEX；实际混合导出 4 源页对应 5 输出页，映射 1/2/3—4/5，200×280bp 完整自定义双页及 300×450bp 空白还原页保留。ZIP 内容和相对编排映射有效，没有本机绝对资源路径。重复生成和重排复用单元；修改仅重编受影响单元。源/项目 A5/装订输出另行实际编译和视觉核对通过。

## 数据保护与剩余限制

原书渲染验收阶段，原库只以 SQLite URI `mode=ro` 读取必要 11 页字段及来源信息，未调用原库 Storage 初始化、未复制/读取凭据或 settings 表。原 11 页字段和 PNG SHA256 与基线一致。初始未提交修改及 `backend/emphasis.py`、`backend/latex_layout.py` 已有删除保留，没有 reset/clean、覆盖稿件、提交、推送或发布。后续识别 HTTP 400 跟进仅另行只读查询指定非秘密配置字段，见下文。

隔离服务仅监听 127.0.0.1，前端 5187、后端 8000，数据在 `runs/ui-r3/isolated-data`；启动/替换前核对自己的进程，未停止用户 OCR 作业。最终对照留在浏览器，进程和日志位置记录于 `processes.json`。用于原书的附件与快照均为本地忽略文件，公开代码夹具采用原创内容。

未发起真实付费 OCR，也未调用真实模型。两个模型协议的固定响应、提示词/结构边界和校准集证明数据及排版链路；不能证明新 OCR 对手写过滤或未知书页已达到样本精度。封面艺术字、装饰图形、复杂图表、复杂跨页表格和任意完整文档自然尺寸覆盖仍有限；未知部分明确保留未验证，P2 未宣称完整复刻。

浏览器 LaTeX 下载请求实际返回 200，但 IAB 下载事件超时，未拿到保存文件路径；后端 ZIP 实际校验通过与浏览器文件保存分别记录。两个第三方测试弃用警告仍存在。最终样本 needs_review 的自然盒与文字映射提示仍存在，已解释并可在界面定位；没有过滤警告或把未覆盖内容伪装为 passed。

验收后的版本整合：用户在2026-10-04追加“合并”指令，授权将已验收源码、测试和文档整合为本地main提交。前述“没有提交”仅描述验收时点；本次提交记录以本地Git日志为准，私有书稿、数据库和验证产物仍保留在忽略目录。

## 验收后跟进：识别 HTTP 400

用户确认 OpenAI Responses、手动连接测试成功但开始识别失败。主代理只读查询保存的协议、服务主机、模型、推理、续接及接口路径，未读取 credentials：当前为 `api.deepseek.com` / `deepseek-flash` / `/responses`、`high`、续接关闭。[DeepSeek 官方 Responses 参考](https://api-docs.deepseek.com/api/create-response/)支持该协议及 flash 图片输入；连接测试仅发送文本，不包含识别所用图片与布局 Schema，不能证明页面请求会被接受。最初未取得具体拒绝原因，随后用户提供了下述 Schema 错误。

两名 `gpt-6.1-sol / xhigh` 子代理仅静态审查与编码，主代理静态审查接受错误详情补丁。`responses_client.py` 的页面请求和模型列表错误现在保留 HTTP 状态与有效 `error.message/param/code`，限制组合长度并脱敏当前密钥；无效或过大响应仍只显示状态码。请求协议、Schema、推理、400 不重试、上下文和用量回调保持。新增 9 项 MockTransport 回归代码，包括错误详情经流水线保存及旧正文、布局、人工修订保护。

主代理曾启动隔离后端回归；用户随即要求“不要测试了，改完就结束”，已中断会话（退出 1，无测试结果）并停止验证。本补丁仅静态接受，未验证运行效果，也未确认真实 HTTP 400 根因；原书渲染的 97 项历史通过结果不能替代这次补丁验证。未调用真实模型、重启用户服务、写入原库、推送或发布；补丁及更新文档保留在工作区。

用户随后返回 `Invalid json schema: field anyOf: missing field type`，`code=invalid_request_error`。当前 wire Schema 的 `layout` 和公式编号等 `anyOf` 分支使用只有 `$ref` 的对象，与服务端要求显式 `type` 的错误吻合。Schema 子代理在 `prompts.py` 展开模型的全部本地引用，外层 `layout` 改为内嵌完整对象：`anyOf` 对象分支有 `type:object`，空值分支保留 `type:null`，wire Schema 不再含 `$ref/$defs`。必填字段、禁止额外字段、nullable/enum、长度与范围、本地 Pydantic 校验、100 条模型复核原因上限及 v1 语义保持；既有断言仅更新访问路径。

主代理已静态审查接受该最小修复。遵照用户不测试的要求，本阶段没有运行测试、构建、类型检查、lint、试编译、脚本、computer use 或真实模型请求。修改已完成，实际服务对更新后 Schema 的接受情况仍未验证，不宣称真实 OCR 验收通过。

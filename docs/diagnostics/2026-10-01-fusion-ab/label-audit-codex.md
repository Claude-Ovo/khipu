# LoCoMo 缺失证据标注抽查（GPT / Codex）

审计日期：2026-09-30。范围：本目录已有数据；只读核查原始问答、对话及已落盘检索轨迹。本次只生成 `label-audit-codex.md` 和同名 `.jsonl`，未修改检索代码、数据或其他报告，未调用付费接口、未连服务器、未运行窗口实验。

**结论：值得做一轮受控的 200→300 诊断实验，但不应直接据此改默认窗口，也不能承诺整体答题提升。** 两条较清楚的新增窗口候选是 (3,82) D4:3（融合第242名，巧克力香草旋纹冰淇淋）和 (7,1) D6:4（第247名，朋友去世）；本次未发现它们在最终100条中的明确等价替代。相反，多条缺失 gold 已由其他返回轮次覆盖，另有错标、图片信息不足及答案本身的问题。

## 抽样方式与可复现范围

先读 `HANDOFF-label-audit.md`，并读取 README、Q1–Q4、Q4-verification 和 LoCoMo-rerank-pair 报告。Q3 中“尚无 LoCoMo 重排配对数据”是较早时点的描述；本轮以较新的交接及本机配对数据为准。历史 LME / 平台结论仅作背景，不借用为本轮 LoCoMo 的事实判定。

抽样单位为 **(conv, qi, missing_gold) 一条缺失标注**，不是一道题。总体137条、90题；本次30条、28题，覆盖conv 0–9全部10段对话。同题 (4,25) 抽中B/C各一条，(9,46) 抽中B两条，不能当成独立答题失败。D类3条未纳入，优先覆盖用户指定的A/B/C。

| 分层 | 材料条数 | 本次条数 | 选择方式 |
|---|---:|---:|---|
| A：融合列表未召回 | 8 | 8 | 全量 |
| B：融合201–300 | 62 | 10 | 已知错标(0,48) D12:14强制对照1条，余61条随机抽9条 |
| B：融合301–400 | 14 | 2 | 随机 |
| B：融合>400 | 18 | 2 | 随机 |
| C：融合≤100、重排后>100 | 12 | 4 | 随机 |
| C：融合101–200、重排后>100 | 20 | 4 | 随机 |

随机抽取使用 Python `random.Random(20260930)`；每池先按 `(conv, qi, missing_gold)` 升序，依次调用 `sample`：B201–300去掉对照后抽9、B301–400抽2、B>400抽2、C融合≤100抽4、C融合101–200抽4。固定抽样后再做语义审计，未因判定结果换样。源文件1起行号：

- A：27、65、71、90、112、119、134、136。
- B：6（对照）、18、49、54、63、72、80、86、95、102、107、126、131、133。
- C：4、8、9、17、61、76、81、123。

这是为诊断设计的不等比例分层样本，并含强制对照、同题相关条目。**不把8/30或任何样本错误比例当成137条、全部问题或平台数据的错误率；也不据此重算“清洗后all@100”。** A类8条已全查，其结论只适用于本份材料的A类。

## 判定口径与核查方法

- **有效**：该轮提供答案的一个事实分项，或能通过明确上下文连接的证据节点。允许多跳/指代，逐条指出需要什么上下文；不要求每条独自证明整段答案。有效不保证标准答案的所有限定、计数和日期都正确。
- **错误**：指向了错误人物/话题，或本轮只是无答案信息的提问、寒暄，答案实际在其他轮次。不能因为旁边有正确答案就把错位轮次“救成有效”。
- **不确定**：人物经验只能弱推断，或本地图片caption/元数据不足或冲突，无法可靠确认。
- 每条对照 `locomo10.json[conv].qa[qi]`、目标轮及相关完整session；按答案主题检索整个conv的正文/caption，核对替代轮次是否存在于该题 `final_on_dias`。重要指代超出±3时扩读原对话。**“未发现替代”是本次核查结论，不是穷尽证明。**
- 现有 `blip_caption` 是本次可用文本证据；未访问外链图片。原始 `query` 仅是图片检索元数据，不能当成已确认图像内容或返回给模型的事实。
- 三个并行只读分组审查A/B/C，主审复核判定边界、关键缺口和错误/不确定例，统一口径；这不是两名标注员全量盲审，未计算一致性系数。
- 对30条原始QA/证据ID/正文/说话人/日期作机器交叉核对；按目标conv、qi逐行流式取 `pair.jsonl` 中28道题，重建融合名次、完整重排名次和最终dia顺序，与抽查文件一致。完整重排顺序按“重排窗口内after＋融合窗口外尾部”计算，不能只看窗口内after。未把51MB原始文件整份装进上下文。
- 下文所有“最终#N”都是**该题**开重排后的最终返回名次，1起；缺失标注本身最终名次均为空。JSONL保存完整原问题、答案、gold正文、来源行、判定及替代名次，便于再查。

## 样本结果及解释

| 类别 | 有效 | 错误 | 不确定 | 合计 |
|---|---:|---:|---:|---:|
| A（全量） | 3 | 5 | 0 | 8 |
| B（含已知错误对照1条） | 10 | 3 | 1 | 14 |
| C | 7 | 0 | 1 | 8 |
| 合计 | 20 | 8 | 2 | 30 |

剔除已知对照后，其余29条为20有效、7错误、2不确定；仍不能外推总体比例。这里统计的是证据定位判定，标准答案的时长、时态、计数和漏项问题另列在条目内。

A类的5条错误集中于人物或轮次错位；3条有效均需上下文且已有同事实证据返回。因而本份材料的“完全未召回gold”不能一概解释成答案事实完全未召回。

B类201–300的10条中，7有效、2错误、1不确定（2错误含强制对照）。7条有效中：4条（源行49、72、107、133）的目标事实已有明确返回替代；2条（54、95）有较清楚的实际缺口；1条（102）必须结合仍被窗口内重排压低的语录正文。后者仅取回纸条照片不能补齐答案。

C类并非都由重排“变差”：样本中4条从融合前100降出，1条原在100外且略降，3条排名改善但仍在100外。有效且仍有实际缺口的例子包括 John 的签名篮球(4,25) D7:7，以及 Evan 的露营(8,50) D25:8；它们已进入200窗口，扩大候选窗口没有直接补救保证。

原报告的 **190/280题all@100、90题至少缺一个gold ID** 与数据一致，但这属于精确标注轮次命中口径。抽查显示，不能把90题全部称为“至少缺一条不可替代的答案事实”，也不能把20条有效样本都当成真实回答障碍。

## 窗口200→300：建议做什么、能验证什么

建议先做一轮小规模、固定题单的配对诊断，再决定是否扩到完整280题或修改默认配置。理由是确有位于新增窗口内的可用事实，而非仅有未知质量的62条候选；这足以支持验证假设，尚不足以预测收益规模。

1. 以(3,82)、(7,1)为主要诊断案例，带上(7,86)的上下文依赖案例、(4,25)/(8,50)的窗口内遗漏，以及已覆盖题作为退化对照。小样本结果不当作整体增益；若要估计总体收益，应覆盖完整280题。
2. 固定commit、语料、问题、查询向量、融合候选顺序、重排模型/MIX=1、doc chars、top_k=100、预算和邻居规则，仅改变窗口200/300；另查查询向量能否复用，当前trace只证明当时两臂共用，不能假定本地保存了向量。若无法复用，应在新配对两臂内共用一次向量，不与旧臂混比。
3. 分别记录：新增201–300证据是否进重排、是否进最终100、需要的相邻上下文是否齐全、是否新增了此前没有的答案事实，以及原来已在前100的有用证据是否被挤掉。保留原gold-ID指标以便比较，另报本次人工核查题的语义覆盖；不静默修改黄金标签。
4. 若只增加冗余/错误gold命中，不视作答案证据收益；若修复上述实际缺口且退化可控，才有理由扩大验证。新的重排调用也可能波动，必要时复跑200作为控制，避免把运行噪声全部归到窗口。
5. 扩窗只直接触及B中融合201–300的62条；A、融合>300的B，以及已经进窗口却排低的C，均不由“纳入新窗口”直接解决。在固定窗口内得分的假设下，给C增加竞争者不会自动改善其名次。
6. 本轮没有答题/判卷调用。即使证据覆盖改善，也需后续答案层验证才能宣称正确率提升。这里不给出付费承诺或执行新实验；历史报告的成本估计未在本轮重新核实。

## 逐条核查

以下按A/B/C及conv、qi排序。`源行`指 `data/missing_evidence.jsonl` 的1起行号；定位原始对话使用conv和dia_id，定位题目使用qi（0起）。

### A类

**A-27｜(conv=2, qi=49) D25:19｜错误｜融合未召回 → 重排—**

- 问题：What food item did Maria drop off at the homeless shelter? 标准答案：Cakes。
- 标注原句节选（John）：“Yeah, it's been great for me. Let me know if you need any advice to get started.” **依据：**John 谈瑜伽/健身建议，没有食品或捐赠事实，属于错位标注。
- 上下文：相邻 D25:20 才是 Maria 要烤蛋糕；应与下一会话 D26:1 的送烘焙食品到收容所连接。旁边有答案不能使本条有效。
- 其他证据与返回情况：`D25:20` 最终#21（Maria 要烤 cakes）；`D26:1` 最终#1（把烤的食品送到 homeless shelter）。指定答案两跳已返回。
- 扩窗含义：A 类不在融合列表，扩窗不能直接纳入；不应追逐此错标。

**A-65｜(conv=4, qi=26) D17:11｜错误｜融合未召回 → 重排—**

- 问题：Which TV series does Tim mention watching? 标准答案：That, Wheel of Time。
- 标注原句节选（John）：“Yeah, I saw "That"! It's amazing to see those worlds and characters come alive. It's a great way to escape reality!” **依据：**D17:11 是 John 说看过 That，问题问 Tim，人物错位。
- 上下文：前轮 D17:10 是 Tim 说 That 是自己喜欢的剧，本条仅回答 Tim 的问话，不承担 Tim 观看经历的证明。
- 其他证据与返回情况：`D17:10` 最终#13（Tim 自述喜欢 That）；`D26:36` 最终#1（Tim 期待看 The Wheel of Time）；`D26:34` 最终#7（该剧下月上映）。Tim 与剧名的正确关系已返回。
- 答案/证据限制：Wheel of Time 仅为期待观看；若题意是已观看，标准答案时态有问题。保留原文剧名 That，不擅自纠正。
- 扩窗含义：不直接处理 A；人物/时态问题不能靠扩窗解决。

**A-71｜(conv=5, qi=17) D26:20｜有效｜融合未召回 → 重排—**

- 问题：How many times did Audrey and Andew plan to hike together? 标准答案：three times。
- 标注原句节选（Andrew）：“Yay! Does Saturday sound good? We can grab some snacks and have a blast exploring. Because on Sunday I am going on a picnic date with my girlfriend.” **依据：**约周六的对话支持其中一次共同徒步计划，单条不证明总数 three times。
- 上下文：需要 D26:18–19 明确活动是两人徒步；D26:21 确认周六。不能把 gold 中周日和女友野餐另算共同徒步。
- 其他证据与返回情况：`D26:19` 最终#13（Audrey 接受并提议选日期徒步）；`D26:23` 最终#34（延续该计划）；`D11:6` 最终#11（另一会话提出一起徒步）；`D11:8` 最终#9（接受该计划）；`D24:13` 最终#47（另一条原标注已返回）。缺失这一计划已有明确替代。
- 答案/证据限制：D11/D12 都说下月，D20/D24 也说下月；反复讨论与独立行程未必一一对应，恰好三次的去重口径不确定。
- 扩窗含义：有效但冗余的 A 类证据；不支持扩窗收益预期。

**A-90｜(conv=6, qi=24) D13:7｜错误｜融合未召回 → 重排—**

- 问题：What kind of games has James tried to develop? 标准答案：football simulator, virtual world inspired by Witcher 3。
- 标注原句节选（John）：“Cool! That sounds awesome. Combining your love of gaming and coding sounds like a dream. Tell me more! Are there any interesting projects you're working on?” **依据：**John 只问 James 正在做什么项目，未给游戏类型；答案在下一轮。
- 上下文：应使用 D13:8 足球模拟器；提问本身不是这一类型的证据。
- 其他证据与返回情况：`D13:8` 最终#92（James 正在做 football simulator）；`D6:2` 最终#58（created virtual world inspired by Witcher 3）；`D22:5` 最终#6（另有自制策略游戏的说明）。标准答案两项都有直接证据返回。
- 答案/证据限制：D22:1/5 另有自制策略游戏，标准答案可能漏项；不改变本条错位判定。
- 扩窗含义：不直接处理 A；无需为恢复这个提问轮次优化召回。

**A-112｜(conv=8, qi=11) D5:5｜错误｜融合未召回 → 重排—**

- 问题：What health issue did Sam face that motivated him to change his lifestyle? 标准答案：Weight problem。
- 标注原句节选（Evan）：“Ginger snaps are my weakness for sure! Dealing with health issues has been tough, but it's made me appreciate the good moments more. These are the ones who bring lots of joy even throug…” **依据：**说话人 Evan 在谈自己的健康，既不是 Sam，也未说明体重问题。
- 上下文：D5:2–9 反而确认是 Sam 慰问 Evan；相邻上下文无法修复人物错位。
- 其他证据与返回情况：`D12:1` 最终#9（Sam 说医生告知体重构成严重健康风险）；`D2:6` 最终#62（体检后意识到体重问题）；`D4:1` 最终#26（因体重被取笑而决定改变）。目标人物及体重事实已有多条直接证据。
- 扩窗含义：错误 A 标注，不是扩窗可修复的有效缺口。

**A-119｜(conv=8, qi=49) D7:10｜有效｜融合未召回 → 重排—**

- 问题：Who was injured in Evan's family? 标准答案：Evan's son and Evan himself。
- 标注原句节选（Sam）：“Glad to hear his ankle is getting better. It's hard seeing someone we care about hurt. Look after yourself too, yeah? We gotta look after our health.” **依据：**Sam 说 his ankle 正在好转，确实支持 Evan 儿子受伤这一跳。
- 上下文：需要 D7:1 的 my son ... hurt his ankle 确定 his；D7:8–9 只恢复足球事故话题。必要指代超过主文件 ±3 轮窗口。
- 其他证据与返回情况：`D7:1` 最终#1（Evan 直接说儿子足球事故伤踝）；`D11:2` 最终#46（Evan 直接说自己打篮球伤膝）；`D11:3` 最终#47（呼应本人伤膝）。儿子和 Evan 本人两跳均已有直接证据。
- 扩窗含义：A 类有效但冗余；不应等同整题事实未召回。

**A-134｜(conv=9, qi=58) D25:12｜错误｜融合未召回 → 重排—**

- 问题：Which events in Dave's life inspired him to take up auto engineering? 标准答案：attending a car show with Dad, working on an old car in a neighbor's garage when he was young, spent a summer restoring an old car with Dad。
- 标注原句节选（Calvin）：“Yeah, it's a way for me to express myself and work through my emotions. It's like my own form of therapy.” **依据：**Calvin 谈音乐与情绪疗愈，不是 Dave 学汽车工程的起因。
- 上下文：D25:13 后才转到 Dave 的汽车，D25:15 才讲邻居车库旧车；这是人物/话题错位。
- 其他证据与返回情况：`D26:6` 最终#2（父亲带 Dave 看车展）；`D25:15` 最终#4（十岁时修邻居车库旧车）；`D12:4` 最终#17（和父亲暑假修车）。指定答案三项直接证据全部返回。
- 扩窗含义：错误 A 标注，不提供扩窗理由。

**A-136｜(conv=9, qi=63) D16:13｜有效｜融合未召回 → 重排—**

- 问题：What style of guitars does Calvin own? 标准答案：custom-made yellow guitar with an octopus on it, shiny purple guitar。
- 标注原句节选（Dave）：“Sure, let me know when, I'm here to lend a hand. It's great to fuel your ideas. Remember that photo you sent me once? Love how this guitar shows our different artistic styles. [shared i…” **依据：**caption 明确章鱼图案吉他，是答案的一部分；不能单条确认定制与所有权。
- 上下文：需 D16:14 的 Calvin 自述 custom made / octopus 确认归属和定制。本条 Dave 所说 photo you sent me 与下轮明确衔接。
- 其他证据与返回情况：`D16:14` 最终#31（Calvin 自述定制章鱼吉他）；`D16:18` 最终#8（caption 为 purple glow）；`D16:19` 最终#22（明确 purple hue、shiny）；`D16:20` 最终#4（Calvin 自述定制 shiny finish）。定制章鱼吉他、闪亮紫色吉他已有证据。
- 答案/证据限制：黄色在本地 text/caption/query 中未见；未看外链图片，黄色仍不确定。有效只指可验证的答案分项。
- 扩窗含义：A 类有效但已有更直接替代；扩窗不解决缺失的颜色信息。

### B类

**B-6｜(conv=0, qi=48) D12:14｜错误｜融合270 → 重排270**

- 问题：What types of pottery have Melanie and her kids made? 标准答案：bowls, cup。
- 标注原句节选（Melanie）：“I appreciate our friendship too, Caroline. You've always been there for me.” **依据：**本条是感谢友情与支持，D12:11–17 也无陶艺种类，不能支持 bowls/cup。此条为强制纳入的已知错标对照。
- 上下文：不是缺少相邻消歧；实际陶艺在 D12:2–5，不能用附近别的话替本条作证。
- 其他证据与返回情况：`D12:4` 最终#48（彩色碗caption）；`D5:8` 最终#14（明确 made this bowl）；`D8:4` 最终#7（孩子制作杯子的caption）；`D8:5` 最终#41（明确 cup）。碗/杯已有正确证据返回。
- 扩窗含义：270 会进入新窗口，但召回错标没有答案信息收益。

**B-18｜(conv=2, qi=3) D3:5｜有效｜融合444 → 重排444**

- 问题：What type of volunteering have John and Maria both done? 标准答案：Volunteering at a homeless shelter。
- 标注原句节选（John）：“We held some events and got to meet some people. We went to a homeless shelter to give out food and supplies. Seeing the smiles on their faces, we knew we made a real difference. We als…” **依据：**John 自述 We went to a homeless shelter to give out food and supplies，直接支持其志愿服务这一跳。
- 上下文：D3:1–4 说明 we 是其服务团体，可补背景，不是确认 John 参加的必要邻句。Maria 是另外一跳。
- 其他证据与返回情况：`D2:1` 最终#7（Maria 在收容所服务，另一人物一跳）；`D9:17` 最终#11（二人一起志愿活动但未注明地点，不完全等价）；`D3:3` 最终#63（团队服务但无收容所地点）。未发现已返回且明确说明 John 在 homeless shelter 服务的等价证据。
- 扩窗含义：444 超过300；存在实际缺口，但该实验不能直接覆盖。

**B-49｜(conv=3, qi=75) D3:4｜有效｜融合229 → 重排229**

- 问题：What are Nate's favorite desserts? 标准答案：coconut milk icecream, dairy-free chocolate cake with berries, chocolate and mixed-berry icecream, dairy-free chocolate mousse。
- 标注原句节选（Nate）：“Thanks, Joanna. Not much has changed for me, but I just discovered that I can make coconut milk icecream and gave it a try. It was actually pretty good, so I'm proud of myself. [shared …” **依据：**本条确定椰奶冰淇淋，结合后文支持 favorite 这一分项。
- 上下文：需 D3:6 的 might be my new favorite snack 把 pretty good 与偏好相连；D3:5 承接问题。
- 其他证据与返回情况：`D3:6` 最终#13（同场 favorite，需对象指代）；`D21:10` 最终#2（直接说 coconut milk ice cream is one of my favorites）；`D21:6` 最终#36（at the top of my list）。同一偏好已有更明确的返回证据。
- 扩窗含义：229 可进300窗口，但恢复此gold主要改善轮次命中。

**B-54｜(conv=3, qi=82) D4:3｜有效｜融合242 → 重排242**

- 问题：What recipes has Nate made? 标准答案：coconut milk icecream, chocolate and vanilla swirl。
- 标注原句节选（Nate）：“I whipped up some chocolate and vanilla swirl. [shared image: a photo of a person holding a chocolate and vanilla ice cream cone]” **依据：**I whipped up some chocolate and vanilla swirl 直接支持 Nate 做过这一口味。
- 上下文：caption 已说明冰淇淋；若只看裸文本，可由 D4:1–2 确认是在回答冰淇淋口味。
- 其他证据与返回情况：`D4:1` 最终#43（做过冰淇淋，但无所缺口味）；`D4:2` 最终#13（询问口味）；`D3:4` 最终#31（另一答案分项椰奶冰淇淋）；`D8:19` 最终#15（椰奶香草配方，不等价于巧克力香草旋纹）。全conv及最终返回核查未发现该具体口味的明确等价替代；当前答案确有分项缺口。
- 答案/证据限制：D3:12 还明确做过蛋糕，标准答案可能不是所有配方的完整列举；不影响本条有效。
- 扩窗含义：242 位于新增范围；是较强的200→300诊断候选，入窗不保证进最终100。

**B-63｜(conv=4, qi=25) D16:9｜错误｜融合240 → 重排240**

- 问题：What similar sports collectible do Tim and John own? 标准答案：signed basketball。
- 标注原句节选（Tim）：“I just love watching LeBron. There was this Finals game a few years back with an epic block that totally changed the game and ended up winning it. Seeing him go for it like that was suc…” **依据：**Tim 在讲喜爱 LeBron 的比赛精神，未说明收藏品类型或持有签名篮球。
- 上下文：D16:8 问为何喜欢该球员；目标物品其实在 D16:7。本条并非物品身份的有效一跳。
- 其他证据与返回情况：`D16:7` 最终#38（Tim 明确拥有签名篮球）；`D7:8` 最终#61（John 一侧只有 sign it 的询问，缺物品身份）。Tim 一跳已返回；真正缺的是 John 的 D7:7（本次 C 样本）及其明确物品信息。
- 扩窗含义：240 进新窗口也不补 John 缺口。

**B-72｜(conv=5, qi=23) D5:5｜有效｜融合227 → 重排227**

- 问题：What are some problems that Andrew faces before he adopted Toby? 标准答案：Finding the right dog and pet-friendly apartments close to open spaces。
- 标注原句节选（Andrew）：“Yeah! Finding a pet-friendly place to live has been tough too. I'm contacting landlords and checking out neighborhoods to find the perfect spot.” **依据：**Andrew 明说找允许养宠物的住处困难，直接支持住房困难分项。
- 上下文：本条无需邻句；D5:7 才说明靠近公园/树林及开放空间，不能把该限定全归给本条。
- 其他证据与返回情况：`D7:8` 最终#49（明确 pet-friendly spot 难找、合适住处和狗都未找到）；`D5:1` 最终#27（寻找合适的狗）；`D5:3` 最终#55（公寓空间限制）。本条住房困难已有等价返回；开放空间限定仍缺 D5:7。
- 扩窗含义：227 可入新窗，但 D5:7 是160→132的C类，扩大本身不直接补该限定。

**B-80｜(conv=5, qi=39) D2:15｜不确定｜融合231 → 重排231**

- 问题：What did Audrey get wtih having so many dogs? 标准答案：Companionship。
- 标注原句节选（Audrey）：“You'll love them! They're great for cuddles and companionship.” **依据：**Audrey 对 Andrew 说 You'll love them，泛指狗带来陪伴；不能直接等同 Audrey 已获得陪伴的亲历自述。
- 上下文：D2:8–14 是 Andrew 找狗/住处，D2:17 的 Our furry friends 使经验性理解合理，仍不足以无条件确认题干人物关系。
- 其他证据与返回情况：`D23:18` 最终#10（Audrey 自述 my little family 给自己companionship）；`D14:16` 最终#29（my furry friends ... my companions）。目标事实已有明确替代。
- 答案/证据限制：存在合理但非直接的人物经验推断，保守记不确定。
- 扩窗含义：231 可入新窗；不应把弱标签命中当成新答案信息。

**B-86｜(conv=6, qi=2) D23:6｜有效｜融合401 → 重排401**

- 问题：Which places or events have John and James planned to meet at? 标准答案：VR Club, McGee's, baseball game。
- 标注原句节选（John）：“Yeah! Let's do it. It'll be a fun experience!” **依据：**John 的 Let's do it 明确接受前轮棒球邀请，支持两人计划一起去。
- 上下文：必须接 D23:5 的 James 邀请 John 下周日参加棒球比赛；裸句无地点/活动。
- 其他证据与返回情况：`D23:5` 最终#47（邀请已返回，但并非明确接受的等价替代）；`D23:3` 最终#2（James 和 Samantha 去McGee's，人物组合不同）。未发现已返回的同次棒球邀请被接受的明确替代；有邀请，缺接受关系。
- 答案/证据限制：若把邀请本身宽松算作计划则缺口减弱，本审计区分邀请与明确接受。
- 扩窗含义：401 仍在300外；不能直接修复。

**B-95｜(conv=7, qi=1) D6:4｜有效｜融合247 → 重排247**

- 问题：Which of Deborah`s family and friends have passed away? 标准答案：mother, father, her friend Karlie。
- 标注原句节选（Deborah）：“The roses and dahlias bring me peace. I lost a friend last week, so I've been spending time in the garden to find some comfort.” **依据：**I lost a friend last week 直接支持朋友去世这一跳。
- 上下文：需 D6:5–8 的悼念/回忆链，把逝去朋友与 D6:8 的 Karlie 最后合影连接；名字位于+4轮，超过±3。属自然指代推断，非逐字同句。
- 其他证据与返回情况：`D6:8` 最终#18（提供Karlie名字和最后合影，不独立明确死亡）；`D6:6` 最终#93（回忆相处，不独立明确死亡）；`D2:1` 最终#1（父亲去世，另一跳）；`D2:13` 最终#2（母亲，另一跳）。未发现另一条已返回的明确朋友死亡陈述；名字已经返回但死亡关系仍缺。
- 答案/证据限制：Karlie 与所失朋友的关联依赖连贯语境，不能宣称原句逐字点名。
- 扩窗含义：247 可入新窗，能与现有名字证据配合，是有实际信息价值的候选。

**B-102｜(conv=7, qi=86) D23:20｜有效｜融合299 → 重排299**

- 问题：What gifts has Deborah received? 标准答案：an appreciate letter from her community, a flower bouqet from her friend, a motivational quote from a friend。
- 标注原句节选（Deborah）：“Exploring historical places and learning their stories is so fun. It was a great experience. I want to share this photo with you. [shared image: a photo of a hand holding a piece of pap…” **依据：**共享写字纸条与后续解释形成照片载体→朋友所赠语录的连贯证据链，单条图片caption不足。
- 上下文：必须 D23:21–22：问纸条内容，回答 written to me by a friend 及具体语录。D23:20–23 均未返回。
- 其他证据与返回情况：`D23:24` 最终#76（泛问其他语录，不等价）；`D4:28` 最终#1（朋友送花，另一礼物）；`D2:7` 最终#19（收到信，未单独说明群体来源）。未找到语录来源/内容的已返回明确替代；D23:22更直接但也缺失。
- 答案/证据限制：判断基于现有caption及后续正文，未查看图片。
- 扩窗含义：299可入窗但只加照片；D23:22已在200内（136→185），群体来信D2:9在354。单扩至300不能直接补齐此题。

**B-107｜(conv=8, qi=7) D7:4｜有效｜融合270 → 重排270**

- 问题：What new hobbies did Sam consider trying? 标准答案：Painting, kayaking, hiking, cooking, running。
- 标注原句节选（Sam）：“The cooking class has been great, I've learned awesome recipes. Last night I made this yummy grilled dish, so good! [shared image: a photo of a plate of food with a piece of salmon and …” **依据：**Sam 自述烹饪课及做菜，直接支持 cooking 分项，已开展比仅考虑更进一步。
- 上下文：识别 cooking 不需邻句；题干把计划和已开展爱好混写，需保留这个措辞边界。
- 其他证据与返回情况：`D7:2` 最终#95（直接 I'm taking a cooking class）；`D1:11` 最终#1（painting）；`D13:8` 最终#4（考虑kayaking）；`D9:11` 最终#46（hiking）；`D21:9` 最终#43（running，未在原gold清单中）。烹饪已有明确替代；其他列举项也有返回线索。
- 答案/证据限制：new/consider 与已从事、重新拾起的范围宽泛，不因此判本条错位。
- 扩窗含义：270可入窗，但本条事实已返回。

**B-126｜(conv=9, qi=19) D12:7｜错误｜融合399 → 重排399**

- 问题：Which places or events has Calvin visited in Tokyo? 标准答案：music festival, car museum, Shibuya crossing, Shinjuku。
- 标注原句节选（Calvin）：“Aww, that's cool, Dave. Reminiscing is always fun! That pic you shared takes me back to my trip to the Ferrari dealership. I saw a lot of amazing cars, but as for me, my car is the best…” **依据：**Ferrari dealership 不是 car museum，本条也没有 Tokyo；D12上下文是保养、修车与童年故事，不能补成东京汽车博物馆。
- 上下文：邻句无可修复的地点/机构同一性；经销店与博物馆不能互换。
- 其他证据与返回情况：`D3:3` 最终#4（东京音乐节，另一项目）；`D24:19` 最终#15（Shibuya/Shinjuku）。全conv检索未发现明确的东京car museum依据；其他项目不能挽救本条。
- 答案/证据限制：car museum 分项本身依据可疑；D24:17–19也混有下月再去与曾经经历的时态，未作为完全可靠整答认证。
- 扩窗含义：399仍在300外；即使召回也不证明博物馆。

**B-133｜(conv=9, qi=46) D19:7｜有效｜融合241 → 重排241**

- 问题：What is Dave's favorite activity? 标准答案：Restoring cars。
- 标注原句节选（Dave）：“Thanks, Calvin! It's been awesome. Been restoring this vintage beauty - here is the final result pic, take a look! [shared image: a photography of a man standing next to a classic car]” **依据：**修复 vintage beauty 并称 awesome，caption与上下文指向车，支持修复汽车偏好。
- 上下文：D19:6明确car hobby，D19:9说修复后满足；裸文本需邻句识别对象，含caption可识别车。favorite是综合偏好归纳。
- 其他证据与返回情况：`D12:2` 最终#16（Fixing cars ... therapy ... fulfillment）；`D4:21` 最终#1（working on cars is what I'm passionate about）；`D22:5` 最终#14（修车热爱语境）。未标注为本题gold的返回证据已明确支持答案。
- 答案/证据限制：并非本句逐字声称唯一最爱；与source_line131为同一题，不能算两次独立答题失败。
- 扩窗含义：241可入窗，但当前事实已覆盖。

**B-131｜(conv=9, qi=46) D21:4｜有效｜融合375 → 重排375**

- 问题：What is Dave's favorite activity? 标准答案：Restoring cars。
- 标注原句节选（Dave）：“Wow, Calvin, that car looks great! Working on cars really helps me relax, it's therapeutic to see them come back to life. I've been working on that Ford Mustang I found in a junkyard - …” **依据：**Working on cars ... relax ... therapeutic ... come back to life 直接支持修复旧车带来满足的偏好。
- 上下文：无需邻句才能识别活动，D21:6只是进一步说restoring令人满足。
- 其他证据与返回情况：`D12:2` 最终#16（修车带来疗愈/满足）；`D4:21` 最终#1（明确对working on cars有热情）。与source_line133同题，已有明确返回替代；多个缺失ID不等于多个独立缺口。
- 答案/证据限制：favorite属综合偏好归纳，非直接最高级原话。
- 扩窗含义：375仍在300外；但答案已有替代支持。

### C类

**C-4｜(conv=0, qi=34) D3:3｜有效｜融合96 → 重排173**

- 问题：What events has Caroline participated in to help children? 标准答案：Mentoring program, school speech。
- 标注原句节选（Caroline）：“Thanks, Mel! Your backing really means a lot. I felt super powerful giving my talk. I shared my own journey, the struggles I had and how much I've developed since coming out. It was won…” **依据：**演讲回顾支持 school speech 这一跳。
- 上下文：需要 D3:1 说明是学校活动、听众为学生；本条单独没有 school/children。
- 其他证据与返回情况：`D3:1` 最终#38（明确 school event、students）；`D9:2` 最终#8（mentorship program for LGBTQ youth）。学校演讲与辅导项目两项均已返回。
- 扩窗含义：96→173 确实降出，但已在200窗口；扩大不能直接修复，且有替代。

**C-8｜(conv=0, qi=56) D4:1｜不确定｜融合152 → 重排126**

- 问题：What symbols are important to Caroline? 标准答案：Rainbow flag, transgender symbol。
- 标注原句节选（Caroline）：“Hey Melanie! Long time no talk! A lot's been going on in my life! Take a look at this. [shared image: a photo of a person holding a necklace with a cross and a heart]” **依据：**正文只有看照片，caption 是 cross and heart 项链；原始 query=pendant transgender symbol。元数据与 caption 不一致，未核实图片像素。
- 上下文：D4:2–3 说明项链及其重要性，但只解释 love/faith/strength，仍不能确认 transgender symbol。query 是图片检索元数据，不是已传入检索文本的答案证据。
- 其他证据与返回情况：`D4:2` 最终#6（询问项链意义，不证明符号身份）；`D4:3` 最终#34（项链家庭意义，不证明 transgender symbol）；`D14:15` 最终#1（明确支持另一跳 rainbow flag）。未找到 transgender symbol 的明确已返回文本替代；保留图像不确定性。
- 答案/证据限制：不能凭 caption 断言原图错，也不能凭 query 断言原图正确。
- 扩窗含义：152→126 已改善仍未入100；扩窗不能补足图像语义。

**C-9｜(conv=0, qi=75) D18:1｜有效｜融合143 → 重排117**

- 问题：How many children does Melanie have? 标准答案：3。
- 标注原句节选（Melanie）：“Hey Caroline, that roadtrip this past weekend was insane! We were all freaked when my son got into an accident. We were so lucky he was okay. It was a real scary experience. Thankfully …” **依据：**my son got into an accident 支持存在儿子这一跳，单条不证明总数3。
- 上下文：三人推断还需 D18:5 的 two children caption、D18:6 和 D18:7 的 their brother；超出本条 ±3。照片人数等于全部子女数仍有假设。
- 其他证据与返回情况：`D18:3` 最终#92（再次提及 my son，等价覆盖本条一跳）；`D10:8` 最终#7（caption 有 three children）；`D4:8` 最终#15（2 younger kids）；`D18:5` 最终#10（caption 有 two children）。儿子一跳已返回；精确总数仍需图片/指代组合。
- 答案/证据限制：不是直接且无条件的精确计数证据。
- 扩窗含义：143→117 改善，已有同义一跳，非强扩窗理由。

**C-17｜(conv=1, qi=31) D1:2｜有效｜融合29 → 重排107**

- 问题：How long did it take for Jon to open his studio? 标准答案：six months。
- 标注原句节选（Jon）：“Hey Gina! Good to see you too. Lost my job as a banker yesterday, so I'm gonna take a shot at starting my own business.” **依据：**失业后决定创业是可追溯的起点；证据定位有效，标准答案 six months 另有计算问题。
- 上下文：需同日 D1:4 明确 business=dance studio；起点2023-01-20，D15:13 在2023-06-19说明天开业，即6月20日。
- 其他证据与返回情况：`D1:4` 最终#32（同日 I'm starting a dance studio）；`D1:6` 最终#11（同日工作室计划）；`D15:5` 最终#7（明天正式开业）；`D15:13` 最终#55（明天 grand opening）。起点/开业日期的替代证据均已返回。
- 答案/证据限制：按这组日期约5个月，不支持six months；即从1月19日失业起算也约5个月。不要把答案计算问题混入证据位置错标。
- 扩窗含义：29→107 确实降出；扩窗不能修复标准答案的日期计算。

**C-61｜(conv=4, qi=25) D7:7｜有效｜融合83 → 重排129**

- 问题：What similar sports collectible do Tim and John own? 标准答案：signed basketball。
- 标注原句节选（John）：“You're a real bookworm! It would be awesome to go to a book conference with you. Check out this photo of what my teammates gave me when we met. It's a sign of our friendship and all the…” **依据：**John 说队友送给自己，caption 为 basketball with autographs，直接支持其签名篮球。
- 上下文：含 caption 可识别物品；D7:8–9 补签名、D7:11 补 this ball，但D7:9/11未返回。另一人 Tim 的证据不能代替 John。
- 其他证据与返回情况：`D7:8` 最终#61（仅 Did they sign it，缺物品身份，不完全等价）；`D16:7` 最终#38（Tim 的签名篮球，另一跳）。未找到已返回且明确支持 John 拥有签名篮球的等价证据，仍有实际缺口。
- 答案/证据限制：物品识别依赖现有 caption，未验证原图片像素。
- 扩窗含义：83→129 的窗口内遗漏；扩大到300不能保证改善。

**C-76｜(conv=5, qi=28) D19:6｜有效｜融合60 → 重排111**

- 问题：What is something that Audrey often dresses up her dogs with? 标准答案：Hats。
- 标注原句节选（Audrey）：“Building trust with them needs patience and regular training. Give them time and love, and praise their successes. [shared image: a photography of three dogs wearing birthday hats and s…” **依据：**caption 明确 three dogs wearing birthday hats，提供又一次戴帽子的实例。
- 上下文：D19:7 的 your pup ... green hat 补人物归属；证明 often 还要结合较早 D4 的实例，单次照片不足。
- 其他证据与返回情况：`D4:23` 最终#37（帽子caption及 always ... when I bring those out）；`D4:25` 最终#28（狗戴帽子 for fun and treats）。Hats 已有更直接、同类重复证据。
- 答案/证据限制：caption 支持实例，频率来自多处对话。
- 扩窗含义：60→111 虽降出，但冗余，扩窗不是必要补救。

**C-81｜(conv=5, qi=40) D14:2｜有效｜融合102 → 重排107**

- 问题：What is a good place for dogs to run around freely and meet new friends? 标准答案：The dog park。
- 标注原句节选（Audrey）：“That's awesome! That must be fun! I just started agility classes with my pups at a dog park. It's awesome to watch them learn and build relationships with other dogs. Seeing them face a…” **依据：**dog park 与 build relationships with other dogs 直接支持地点及社交部分。
- 上下文：识别地点不需邻句；D14:4 只补练习。原句没有独立证明 freely 这一限定。
- 其他证据与返回情况：`D4:25` 最终#6（run and mingle with other pooches）；`D23:10` 最终#11（socializing、running around）；`D4:21` 最终#10（公园运动与结交朋友）。更完整的地点/跑动/社交证据已返回。
- 扩窗含义：102→107 原本就在100外；缺此gold不等于缺答案依据。

**C-123｜(conv=8, qi=50) D25:8｜有效｜融合111 → 重排107**

- 问题：What kind of hobbies does Evan pursue? 标准答案：painting, hiking, reading books, biking, skiing, snowboarding, ice skating, swimming, camping, kayaking。
- 标注原句节选（Evan）：“Glad to hear it! Nature really has a way of calming and reviving the soul. Last summer, I took this pic on a camping trip - it was such an amazing sunset. Moments like these remind us o…” **依据：**Evan 自述 on a camping trip，明确支持 camping 分项。
- 上下文：camping 无需相邻上下文；若证明 kayaking，D25:10 的明确自述更可靠，该轮也未返回。
- 其他证据与返回情况：`D6:1` 最终#14（旅行和帐篷caption，仅为露营弱旁证）；`D13:7` 最终#9（推荐kayaking，非已从事的等价自述）；`D13:11` 最终#34（将来同行计划，非过去已完成）。未找到已返回的明确 camping 同义自述；kayaking 也只有较弱线索。
- 答案/证据限制：不把推荐、将来计划和照片暗示自动当作已进行活动。
- 扩窗含义：111→107 已改善但仍在100外；窗口内问题，扩大不直接解决。

## 材料指纹与边界

SHA-256：

- `data/missing_evidence.jsonl`：`08bf9d63639ffcd6d774cfb7a18ea2fab7426b4bdac6411096d9a252a6d25ddd`
- `data/locomo10.json`：`79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
- `data/per-question.json`：`d9c9a5fc65db4745ddf7d30c18d4143e2e976daa9477b508092e116aa46d2b4f`
- `data/pair.jsonl`：`3d66d5c60105a97640d67970c80c1e3275011e167d47dacd0b2b918af3fcf6a3`

未读取交接列明的禁区文件，未查看外链图片。本机对话文本/caption足够完成这轮有限抽查；对图像、精确计数和全题答案的未决点已逐项保留。家庭群与笔记本工具受当前审批策略阻止，未用于证据或交付。

交付核验：固定种子重抽与JSONL的30条完全一致；全部替代证据的最终名次核验通过；MD与JSONL判定一致。写入后，本目录原有16个文件的SHA-256与写入前一致。成稿另经只读交叉检查，未发现实质矛盾。

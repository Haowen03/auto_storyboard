# LTX 音视频生成工作流指令文档 v1.5

> 适用阶段：本工作流用于 **多宫格分镜参考帧生成工作流完成之后**，即已经获得用户初始 idea、`workflow_summary.json` 以及 `case_final_frameXX.png` 或 `case_edit_frameXX.png` 等参考帧之后，进一步规划并生成 LTX 音视频 shot 参数。

> v1.5 更新重点：在 v1.4 的跨 Shot 转场双模式基础上，进一步明确 **Bridge Shot 的生成策略与使用策略分离**：
> 1. Bridge shot 默认生成，不再让大模型判断“是否需要生成”；
> 2. Bridge shot 是一个可选转场资产，最终是否使用由用户或后期流程决定；
> 3. Bridge shot 必须包含明确的转场载体与转场动作，否则它与直接拼接没有本质区别；
> 4. 继续保留 **Direct Concatenation Mode（直接拼接模式）** 与 **Trim-and-Overlap Mode（裁剪余量模式）** 两个参数分支。
>
> 代码层建议拆成两个开关：`generate_bridge_candidates = True` 默认生成桥接候选；`use_bridge_at_export` 决定最终拼接时是否使用。

---

## 0. 工作流定位

参考帧生成阶段的目标是获得一组具有剧情连续性、环境一致性、角色与核心道具稳定性、镜头多样性的关键视觉支点。LTX 音视频生成阶段的目标不是机械地把每一张参考帧变成一段视频，而是根据用户想要的总时长、参考帧之间的剧情关系、镜头切换强度和动作连续性，决定：

1. 哪些参考帧适合单独生成一个 shot；
2. 哪些参考帧适合合并为一个连续动作 shot；
3. 哪些参考帧适合作为长 shot 内部的中段或后段支点；
4. 每个 shot 应该持续几秒；
5. 每个参考帧应放在 `image_idxs` 的哪个时间位置；
6. `image_strengths` 应该保持 1.0 还是降低以释放运动空间；
7. 每个 shot 的英文 prompt 与 negative_prompt 应该如何写；
8. 多个 shot 拼接时如何避免动作停顿、动量断裂和幻灯片感；
9. 当单个 shot 受显存限制不能容纳所有关键帧时，如何默认生成 **bridge shot candidate** 作为可选转场资产。

核心原则：

```text
Reference frames are temporal anchors, not mandatory start/end frames.
LTX shots should preserve story continuity, camera continuity, motion momentum, and audio continuity.
Prompt planning must follow the user's idea, workflow_summary.json, and the actual generated reference frames.
When multiple shots are required, cross-shot continuity must be explicitly designed rather than left to random hard cuts.
```

中文概括：

```text
参考帧是视频内部的剧情支点和镜头支点，不是必须作为每段视频首尾帧使用。LTX 分镜规划必须同时考虑剧情连续、环境一致、镜头切换、动作动量、总时长和音频氛围。若必须拆成多个 shot，则 shot 与 shot 之间的衔接也要被显式设计，而不是直接硬切。
```

---

## 1. 输入材料

执行 LTX 音视频生成规划时，应读取以下输入：

```text
1. 用户初始 idea
2. 用户指定或期望的总时长
3. workflow_summary.json
4. 参考帧文件列表，通常为 case_final_frame01.png 到 case_final_frameNN.png
5. 如果 final_frames 不存在，则使用 case_edit_frameXX.png
6. 如有必要，读取资源库信息 resources 或 resource_registry
7. 用户对镜头、风格、时长、声音、对白、产品展示的补充要求
8. 是否允许后期裁剪（trim），即本次是 Direct Concatenation 还是 Trim-and-Overlap
9. 最终导出时是否使用 bridge shot；默认生成 bridge candidate，但使用权交给用户或后期流程
```

### 1.1 参考帧优先级

默认使用顺序：

```text
case_final_frameXX.png > case_edit_frameXX.png > case_base_frameXX.png
```

---

## 2. 必须继承的上游信息

LTX 规划必须继承参考帧生成阶段已经确定的关键信息：

```text
1. case_name
2. idea
3. grid 或帧数 N
4. resources / resource_registry
5. frame_entity_plan
6. reference_resolver
7. edit_plan
8. final_frames
9. 每帧的 story_state
10. 每帧的 edit_strategy 和 camera delta
11. 每帧必须保持的人物、道具、场景和核心商品形象
```

不能只看图像表面重新编故事。应以 `workflow_summary.json` 为剧情与镜头语义的第一依据，再结合参考帧本身判断如何生成视频。

---

## 3. LTX Prompt 基本规范

每个 shot 的 `prompt` 必须遵守 LTX prompt 规范：

```text
1. 英文
2. 单段落
3. 4 到 8 句
4. 现在时
5. 一个清晰的起承转合动作序列
6. 按 Shot -> Scene -> Character -> Action -> Camera -> Audio 的顺序组织
7. 情绪用可见动作、姿态、表情表达，不只写抽象情绪词
8. 正向 prompt 描述应该出现的干净画面，不在正向 prompt 里堆叠 no subtitles / no text 等否定词
9. 字幕、UI、漫画标注、lower-third 等放入 negative_prompt
```

---

## 4. Negative Prompt 标准模板

基础模板：

```text
subtitles, captions, lower-third, chyron, nameplate, news broadcast, TV graphics, UI overlay, watermark, logo, title card, floating text, speech bubble, comic annotation, manga annotation, character introduction overlay, vertical subtitles, vertical title card, duplicate character, extra people, distorted hands, broken fingers, extra fingers, fused fingers, warped face, unstable scene layout, sudden camera jump
```

若为跨-shot桥接或长 shot，还应追加：

```text
abrupt unrelated scene cut, uncontrolled random transition, slideshow effect, repeated still image, freeze frame, chaotic camera rotation, hard cut feeling, motion discontinuity
```

---

## 5. LTX Shot Planning 总流程

```text
Step 1  读取用户初始 idea、目标总时长、workflow_summary.json 和参考帧文件
Step 2  建立 Reference Frame Inventory，列出每张参考帧的 story_state、camera、focus、motion potential
Step 3  判断参考帧之间的关系：连续动作、镜头切换、情绪缓冲、产品特写、收束帧
Step 4  根据总时长规划 shot 数量和每个 shot 的时长
Step 5  决定每个 shot 使用单帧、多帧，或长 shot 多参考帧
Step 6  当出现多个主 shot 时，默认为相邻主 shot 生成 bridge shot candidate
Step 7  判断使用 Direct Concatenation Mode 还是 Trim-and-Overlap Mode
Step 8  标记每个 bridge candidate 的 transition carrier 与 can_skip_bridge 状态
Step 9  为每个 shot 设置 images、image_idxs、image_strengths、video_seconds
Step 10 按 LTX prompt 规范生成英文 prompt 与 negative_prompt
Step 11 输出 LTX Shot Plan 表和逐 shot 参数代码块
Step 12 给出 bridge 使用建议、可跳过策略和常见失败修正策略
```

---

## 6. Reference Frame Inventory

在规划 LTX shot 前，先为每张参考帧建立清单。

推荐表格：

| Frame | Source File | Story State | Camera Role | Motion Potential | Suggested Use |
|---|---|---|---|---|---|
| frame01 | case_final_frame01.png | 开场建立 | wide establishing | low | single opening shot or first anchor |
| frame02 | case_final_frame02.png | 动作开始 | medium / side | medium | single shot or second anchor |
| frame03 | case_final_frame03.png | 动作触发 | close-up / insert | high | product/action shot |
| frame04 | case_final_frame04.png | 结果反应 / 新阶段开始 | reaction shot / new beat | medium | next shot or bridge target |

### 6.1 Motion Potential 判断

| 类型 | 说明 | LTX 用法 |
|---|---|---|
| low | 人物静坐、凝视、收束、情绪停顿 | 单帧 5 到 6 秒 |
| medium | 走近、抬手、转身、观察、轻动作 | 单帧 5 到 6 秒或双帧 8 秒 |
| high | 奔跑、追逐、打斗、爆发、喷水、飞行 | 多帧 5 到 8 秒，必要时降低 image_strength |
| transition | 明显切镜、从全景到近景、从过肩到低角度 | 单独成 shot 或 bridge shot |
| closure | 结尾、余波、情绪收束、光效消散 | 单帧 5 到 6 秒，避免写成静态定格 |

---

## 7. Shot 拆分原则

### 7.1 什么时候使用单帧参考

以下情况适合单帧参考生成一个 shot：

1. 参考帧本身已经是明确镜头切换后的新机位；
2. 当前 shot 是情绪缓冲、反应、收束或产品展示；
3. 前后帧之间不是连续肢体动作，而是正常分镜切换；
4. 单帧包含完整画面关系，LTX 可在前后自由生成动作；
5. 希望避免多参考帧把模型锁成幻灯片。

### 7.2 什么时候使用多帧参考

以下情况适合多帧参考生成一个连续 shot：

1. 多张参考帧表现同一动作的起承转合；
2. 参考帧之间机位差异不大，主要是动作推进；
3. 用户希望这段动作不要硬切；
4. 动作需要完整连续性；
5. 多张参考帧已经经过优化，角色、道具、环境一致。

### 7.3 什么时候使用长 shot

如果参考帧较少，但每张参考帧都具有强镜头多样性和剧情连续性，可以使用一个 10 到 12 秒左右的长 shot，让 LTX 在 shot 内部完成镜头切换。

### 7.4 Bridge Shot 默认生成规则（v1.5）

v1.5 不再让大模型判断“是否需要生成 bridge shot”。只要视频被拆成多个主 shot，并且存在相邻主 shot 边界，就默认生成一个 bridge shot candidate。

标准形式：

```text
Main Shot 1: frame01 -> frame02 -> frame03
Bridge Candidate 1-2: frame03 -> frame04
Main Shot 2: frame04 -> frame05 -> frame06
```

这里的关键是：

```text
Bridge generation is default.
Bridge usage is optional.
```

中文概括：

```text
bridge shot 默认生成，但最终是否使用由用户或后期流程决定。
```

这样做的原因是：让大模型判断“是否需要 bridge”不稳定，而默认生成 bridge candidate 可以提供额外转场资产。若直接拼接已经足够顺，用户可以跳过 bridge；若直接拼接突兀，则使用 bridge。

### 7.5 Bridge Shot 的有效性要求

默认生成 bridge 不等于随便生成一段 frame03 -> frame04 的普通视频。bridge shot 必须提供明确的转场价值。

硬规则：

```text
A bridge shot must contain an explicit transition carrier.
If there is no transition carrier, the bridge shot has little advantage over direct concatenation.
```

中文规则：

```text
bridge shot 必须包含明确转场载体；如果没有转场载体，它与 shot1 和 shot2 直接拼接没有本质区别。
```

常见有效转场载体：

| Transition Carrier | 示例 |
|---|---|
| light wipe | 莲心金光扩张并吞没镜头 |
| motion wipe | 火焰飘带、衣袖、水花、莲花瓣扫过镜头 |
| action bridge | 抬手、转身、起身、落地等同一动作延续 |
| trajectory bridge | 镜头跟随能量束、水柱、飞剑轨迹进入下一机位 |
| camera bridge | 推进、绕行、拉远、沿主体运动方向跟拍 |
| audio bridge | 雷声、魔法嗡鸣、火焰呼啸、水声提前或延续 |

---

## 8. 时长规划规则

### 8.1 基础经验值

| 参考方式 | 推荐时长 | 说明 |
|---|---:|---|
| 单帧参考，情绪或收束 | 5 到 6 秒 | 不容易戛然而止，也能留出情绪流动 |
| 单帧参考，强动作 | 5 到 6 秒 | 需要降低 image_strength，强化连续动作描述 |
| 双帧参考，普通动作 | 8 秒 | 适合完整动作起承转合 |
| 双帧参考，高动态动作 | 6 到 8 秒 | 防止过长导致漂移 |
| 三帧参考，连续动作 | 5 到 8 秒 | 参考帧太多时避免 1.0 强锁死 |
| 多参考长 shot | 10 到 12 秒 | 受显存限制时推荐上限 |
| bridge shot | 3 到 5 秒 | 负责跨段衔接，不承担完整新剧情 |

### 8.2 总时长重分配

如果原本规划为两个 12 秒主 shot：

```text
shot1 = 12s
shot2 = 12s
总时长 = 24s
```

插入 bridge 后有两种做法：

#### A. 总时长放宽

```text
shot1 = 12s
bridge = 4s
shot2 = 12s
总时长 = 28s
```

#### B. 总时长不变

```text
shot1 = 10s
bridge = 4s
shot2 = 10s
总时长 = 24s
```

如果总时长固定，优先压缩主 shot，而不是把 bridge 压缩得过短。

---

## 9. image_idxs 设置规则

### 9.1 核心原则

参考帧不一定放在首帧或尾帧。它们可以放在视频内部，用作动作和镜头的时间支点。

```text
Do not force every reference image to be the first or last frame.
Leave motion before and after each anchor when appropriate.
Avoid ending exactly on a reference frame unless the user explicitly wants a freeze-frame ending.
```

### 9.2 推荐位置

| 场景 | image_idxs 推荐 |
|---|---|
| 开场建立，第一帧必须锁定 | `[0.0]` 或 `[0.0, 0.70]` |
| 单帧情绪镜头 | `[0.35]` 到 `[0.45]` |
| 单帧动作镜头 | `[0.45]` 到 `[0.60]` |
| 双帧连续动作 | `[0.10, 0.72]` 或 `[0.18, 0.76]` |
| 三帧连续动作 | `[0.10, 0.48, 0.82]` |
| 收尾镜头 | `[0.30]` 到 `[0.40]`，后半段留给情绪自然结束 |

### 9.3 关于边界帧的补充说明

这一条在 v1.4 中做了重要修正：

```text
Whether the last anchor should be placed near 0.8 or near 0.95 depends on the stitching mode.
```

即：

- **没有 bridge** 或 **允许后期裁剪** 时，边界帧可以在 `0.70–0.82` 左右；
- **有 bridge 且直接整段拼接** 时，边界帧应放在 `0.88–0.95`，保证边界状态尽量贴近桥接段起点。

---

## 10. image_strengths 设置规则

### 10.1 默认值

```python
image_strengths = [1.0] * len(images)
```

适合：
- 人物身份需要强锁定；
- 核心商品或道具需要稳定；
- 场景布局必须保持；
- 参考帧本身动作不强。

### 10.2 高动态镜头

如果出现幻灯片感、人物像静态图片间切换、动作无法连续，可以降低：

```python
image_strengths = [0.90]
```

或：

```python
image_strengths = [0.90, 0.90, 0.90]
```

### 10.3 bridge shot 推荐值

bridge shot 需要兼顾稳定与过渡，推荐：

```python
image_strengths = [0.92, 0.95]
```

如果 bridge 内部还有中间过渡帧：

```python
image_strengths = [0.92, 0.88, 0.95]
```

---

## 11. Shot 内部动作设计规则

每个 prompt 必须写成完整动作，而不是参考帧静态描述。

高动态镜头应写清：
- 位移方向；
- 步态或动作节奏；
- 手臂、腿部、衣服、头发的运动；
- 道具作用轨迹；
- 摄像机如何跟随；
- 动作如何结束或自然延续到下一 shot。

---

## 12. Shot 间连续性规则（v1.5 核心更新）

### 12.1 不能把 shot 内流畅等同于全片流畅

常见错误：

```text
shot1(frame01~03) 内部流畅
shot2(frame04~06) 内部流畅
=> 直接拼接就会流畅
```

这是错误的。问题在于：

```text
frame03 -> frame04 这个边界从未被真正生成过。
```

因此，v1.5 默认生成 bridge candidate，为每个相邻主 shot 提供一个可选转场资产。

### 12.2 Bridge Shot 定义（v1.5）

bridge shot 是插入在两个主 shot 之间的 **可选转场短 shot**。它默认生成，但不默认强制使用。

其目标不是创造新剧情，而是：
- 提供 shot1 与 shot2 之间的可选转场；
- 吸收 shot1 结尾动量；
- 过渡到 shot2 开头状态；
- 通过光效、遮挡、动作桥、镜头路径、音频桥等方式降低硬切感。

核心规则：

```text
Generate bridge candidates by default.
Let the user or editing pipeline decide whether to use them.
```

中文概括：

```text
bridge 默认生成，是否使用交给用户或后期流程。
```

### 12.3 Bridge Shot 的真正价值

bridge shot 的价值不只是“多生成一段 frame03 到 frame04 的视频”，而是提供一个明确转场表达。

如果 bridge prompt 只是：

```text
The scene moves from frame03 to frame04.
```

或只是普通描述：

```text
The character continues the motion and reaches the next state.
```

那么它很可能与直接拼接没有区别。

有效 bridge 必须回答：

```text
1. 用什么东西或动作完成转场？
2. 这个转场从哪里开始？
3. 它如何遮挡、引导或连接到下一镜头？
4. 转场后到达什么状态或机位？
5. 音频是否延续或提前进入？
```

### 12.4 Bridge Shot 最常见结构

```text
bridge candidate = [last anchor of previous shot] -> [first anchor of next shot]
```

例如：

```text
shot1: frame01 -> frame02 -> frame03
bridge candidate: frame03 -> frame04
shot2: frame04 -> frame05 -> frame06
```

### 12.5 Bridge 可跳过规则

因为 bridge candidate 默认生成，所以最终输出时需要允许用户选择：

```text
1. 使用 bridge：shot1 + bridge + shot2
2. 跳过 bridge：shot1 + shot2
```

推荐在 Shot Plan 中显式标记：

```json
{
  "bridge_id": "bridge_01_02",
  "generated_by_default": true,
  "optional_for_export": true,
  "transition_carrier": "fire ribbon motion wipe",
  "user_may_skip": true
}
```

### 12.6 如果 frame03 与 frame04 差异过大

可以额外生成一张过渡参考帧：

```text
case_transition_frame03_04.png
```

然后 bridge 使用：

```text
frame03 -> transition_frame03_04 -> frame04
```

这样可以降低桥接难度。

---

## 13. 跨 Shot 双模式：Direct Concatenation vs Trim-and-Overlap

这是 v1.4 引入并在 v1.5 保留的关键机制。由于不同团队的后期能力不同，工作流必须明确支持两种模式。

注意：这两种模式控制的是 **如何设置边界参考帧和如何拼接**，不是控制是否生成 bridge。v1.5 默认生成 bridge candidates；两种模式都可以生成 bridge，只是参数位置和后期使用方式不同。

---

### 13.1 模式 A：Direct Concatenation Mode（直接拼接模式）

#### 13.1.1 定义

适用于：
- 生成出来的 mp4 会被 **完整保留并按顺序直接拼接**；
- 不做中间裁剪；
- 不做 overlap 淡入淡出；
- 每个 shot 的开头和结尾都必须天然可接。

#### 13.1.2 核心规则

在 Direct Concatenation Mode 下：

```text
Boundary anchors must stay close to the shot boundaries.
```

即：
- shot1 的最后一个边界参考帧，必须靠近 shot1 结尾；
- bridge 的第一个参考帧，必须靠近 bridge 开头；
- bridge 的最后一个参考帧，必须靠近 bridge 结尾；
- shot2 的第一个参考帧，必须靠近 shot2 开头。

#### 13.1.3 推荐区间

| 位置 | 推荐值 |
|---|---|
| 主 shot 的首边界帧 | `0.00–0.08` |
| 主 shot 的尾边界帧 | `0.88–0.95` |
| bridge 开始帧 | `0.00–0.08` |
| bridge 结束帧 | `0.85–0.95` |
| 后续主 shot 的开场帧 | `0.00–0.08` |

#### 13.1.4 参数示例

```python
# main shot 1
images = [
    f"{FRAMES_DIR}/case_final_frame01.png",
    f"{FRAMES_DIR}/case_final_frame02.png",
    f"{FRAMES_DIR}/case_final_frame03.png",
]
image_idxs = [0.00, 0.45, 0.92]
image_strengths = [0.92, 0.92, 0.95]
video_seconds = 10
```

```python
# bridge shot 1-2
images = [
    f"{FRAMES_DIR}/case_final_frame03.png",
    f"{FRAMES_DIR}/case_final_frame04.png",
]
image_idxs = [0.05, 0.88]
image_strengths = [0.95, 0.95]
video_seconds = 4
```

```python
# main shot 2
images = [
    f"{FRAMES_DIR}/case_final_frame04.png",
    f"{FRAMES_DIR}/case_final_frame05.png",
    f"{FRAMES_DIR}/case_final_frame06.png",
]
image_idxs = [0.05, 0.45, 0.88]
image_strengths = [0.95, 0.92, 0.92]
video_seconds = 10
```

#### 13.1.5 优点与缺点

优点：
- 最适合自动化；
- 代码逻辑简单；
- 每个 mp4 都是可直接使用的成段。

缺点：
- 对边界设计要求高；
- 可剪辑余量少；
- 若 bridge 不够好，仍可能保留轻微缝隙感。

---

### 13.2 模式 B：Trim-and-Overlap Mode（裁剪余量模式）

#### 13.2.1 定义

适用于：
- 允许对每个 shot 的输出视频做局部裁剪；
- 允许不完整保留整个 mp4；
- 把 shot 的尾段或头段当作 **handle / overlap handle**；
- 在后期中选择最自然的切点。

#### 13.2.2 核心规则

在 Trim-and-Overlap Mode 下：

```text
Boundary anchors may appear earlier, leaving trailing handles for editorial trimming.
```

即：
- shot1 的最后参考帧可以放在 `0.70–0.82` 左右；
- 后面留出的 18%–30% 时间不是必须保留，而是作为可剪辑余量；
- shot2 的第一个参考帧也可以不贴边，但桥接时要通过剪辑保证自然衔接。

#### 13.2.3 推荐区间

| 位置 | 推荐值 |
|---|---|
| 主 shot 的尾边界帧 | `0.70–0.82` |
| bridge 开始帧 | `0.08–0.18` |
| bridge 结束帧 | `0.75–0.88` |
| 主 shot 的中段帧 | 根据节奏均匀分布 |

#### 13.2.4 参数示例

```python
# main shot 1
images = [
    f"{FRAMES_DIR}/case_final_frame01.png",
    f"{FRAMES_DIR}/case_final_frame02.png",
    f"{FRAMES_DIR}/case_final_frame03.png",
]
image_idxs = [0.00, 0.44, 0.76]
image_strengths = [0.92, 0.92, 0.92]
video_seconds = 10
```

```python
# bridge shot 1-2
images = [
    f"{FRAMES_DIR}/case_final_frame03.png",
    f"{FRAMES_DIR}/case_final_frame04.png",
]
image_idxs = [0.10, 0.78]
image_strengths = [0.90, 0.92]
video_seconds = 4
```

```python
# main shot 2
images = [
    f"{FRAMES_DIR}/case_final_frame04.png",
    f"{FRAMES_DIR}/case_final_frame05.png",
    f"{FRAMES_DIR}/case_final_frame06.png",
]
image_idxs = [0.00, 0.42, 0.82]
image_strengths = [0.95, 0.92, 0.92]
video_seconds = 10
```

#### 13.2.5 使用方式说明

此模式下，不能简单完整拼接 `shot1 + bridge + shot2`。

正确理解是：
- `shot1` 生成 10 秒；
- 其中 frame03 在约 7.6 秒附近达到；
- 后面剩余的 2.4 秒是剪辑余量；
- 实际使用时在 frame03 附近或其后短时间内选取最佳切点，再接入 bridge。

#### 13.2.6 优点与缺点

优点：
- 更灵活；
- 更有机会得到自然切点；
- 便于做精细后期。

缺点：
- 不能直接自动整段拼接；
- 需要后期剪辑配合；
- 若代码流程默认整段拼接，则不能使用此模式。

---

## 14. 代码分支规则：生成 Bridge 与使用 Bridge 分离

v1.5 明确将两个问题分开：

```text
1. 是否生成 bridge candidate？默认生成。
2. 最终是否使用 bridge？由用户或后期流程决定。
```

### 14.1 建议代码开关

```python
generate_bridge_candidates = True   # 默认 True
stitch_mode = "direct"              # "direct" or "trim_overlap"
use_bridge_at_export = True          # 用户可选 True / False
```

### 14.2 generate_bridge_candidates

默认值：

```python
generate_bridge_candidates = True
```

含义：
- 只要存在相邻主 shot，就生成 bridge candidate；
- 不让大模型判断“是否需要 bridge”；
- 避免弱模型误判导致没有可用转场资产。

### 14.3 use_bridge_at_export

最终拼接时，用户或后期流程可以选择：

```python
use_bridge_at_export = True
```

输出：

```text
shot1 + bridge + shot2
```

或：

```python
use_bridge_at_export = False
```

输出：

```text
shot1 + shot2
```

### 14.4 stitch_mode

`stitch_mode` 控制边界参考帧位置策略。

```python
stitch_mode = "direct"       # 整段 mp4 直接拼接
stitch_mode = "trim_overlap" # 允许裁剪余量
```

#### Direct Mode

若 `stitch_mode = "direct"`，边界参考帧必须靠近边界：

```text
shot1 tail anchor: 0.88–0.95
bridge first anchor: 0.00–0.08
bridge last anchor: 0.85–0.95
shot2 first anchor: 0.00–0.08
```

#### Trim-and-Overlap Mode

若 `stitch_mode = "trim_overlap"`，边界参考帧可以更早出现，后面作为剪辑 handle：

```text
shot1 tail anchor: 0.70–0.82
bridge first anchor: 0.08–0.18
bridge last anchor: 0.75–0.88
```

### 14.5 推荐输出状态字段

```json
{
  "bridge_id": "bridge_01_02",
  "generated_by_default": true,
  "optional_for_export": true,
  "transition_carrier": "golden flame light wipe",
  "stitch_mode": "direct",
  "use_bridge_at_export": "user_selectable"
}
```

---

## 15. Bridge Shot Prompt 写法

bridge shot 的 prompt 重点不是描述完整新剧情，而是描述 **转场载体**。

推荐写法重点：
- 光效如何扩张、吞没镜头、散开；
- 道具或飘带如何扫过镜头形成遮挡；
- 镜头如何跟随动作轨迹进入下一状态；
- 音频如何延续到下一段。

### 15.1 错误写法

```text
The scene transitions from frame03 to frame04.
```

问题：
- 只说明“要转场”；
- 没有说明画面如何转；
- 模型容易随机切镜。

### 15.2 正确写法示例

```text
The golden lotus core flares upward and a broad fire ribbon sweeps across the lens, creating a controlled motion-wipe rather than a hard cut. As the flame clears, the camera settles into the next state where Nezha is closer to sitting upright on the lotus. The motion remains continuous and the glow carries across the transition. Crackling fire and a rising magical hum bridge the cut smoothly.
```

---

## 16. 多参考帧 shot 内部转场细节控制

当一个 shot 使用多张参考帧，尤其是 10 秒以上的长 shot 时，不能只写 `then cuts to`、`the camera transitions to` 或让模型随机完成转场。转场本身必须被写成一个可见的画面事件，让 LTX 有明确的视觉路径把前一帧带到后一帧。

核心原则：

```text
Do not ask LTX to randomly transition between reference frames.
Describe the transition as an in-frame visual event, a camera path, an object wipe, a light wipe, a scale change, or a motion bridge.
```

---

## 17. 镜头多样性规则

LTX 阶段应继承参考帧生成阶段的镜头多样性，但不必让每个 shot 都强行换镜头。重点是根据参考帧功能决定：

```text
连续动作段：镜头可以跟拍或轻微变化，重点保持动量。
镜头切换段：可以单帧参考，让 LTX 生成完整镜头语义。
bridge 段：以连接功能优先，不追求额外复杂分镜。
产品或手部细节：使用 medium close-up 或 insert shot。
反应镜头：使用 medium close-up 或 side reaction shot。
收束镜头：使用 wide ending shot 或 quiet medium shot。
```

---

## 18. 音频设计规则

LTX prompt 的最后一到两句应包含音频设计。

bridge shot 尤其适合使用：
- rising hum
- sustained magical tone
- flame whoosh
- water rush
- cloth sweep
- impact echo
- sound carry-over into the next shot

因为音频桥能够进一步减弱拼接缝隙感。

---

## 19. 输出格式

每次 LTX 工作流输出应包含两部分。

### 19.1 Shot Plan 表

| Shot | Reference Images | Duration | image_idxs | Shot Function | Stitch Mode | Transition Notes |
|---|---|---:|---|---|---|---|
| Shot 1 | frame01, frame02, frame03 | 10s | [0.00, 0.45, 0.92] | 主动作段 1 | direct | 结尾接近 frame03 |
| Bridge 1-2 | frame03, frame04 | 4s | [0.05, 0.88] | 跨 shot 转场 | direct | 火焰扫镜作为 motion wipe |
| Shot 2 | frame04, frame05, frame06 | 10s | [0.05, 0.45, 0.88] | 主动作段 2 | direct | 开头接 frame04 |

### 19.2 参数代码块

格式固定为：

```python
images = [
    f"{FRAMES_DIR}/case_final_frame03.png",
    f"{FRAMES_DIR}/case_final_frame04.png",
]
image_idxs = [0.05, 0.88]
image_strengths = [0.95, 0.95]
video_seconds = 4

prompt = "Cinematic ..."

negative_prompt = "subtitles, captions, lower-third, ..."
```

---

## 20. 常见失败与修正

### 20.1 bridge 不流畅

原因：
- bridge 只是把两个帧硬塞进去；
- 没有转场载体；
- 边界帧位置与拼接模式不匹配。

修正：

```text
1. 增加明确的 light wipe / motion wipe / action bridge
2. direct 模式下把边界帧移近 shot 边界
3. trim 模式下确认后期会裁掉尾巴
4. 必要时增加 transition frame03_04
```

### 20.2 直接拼接时仍然有停顿

修正：

```text
1. 检查是否误用了 trim 模式参数
2. 把 shot1 尾帧从 0.76 调整到 0.90 左右
3. 把 bridge 首帧提前到 0.00–0.08
4. 把 shot2 首帧提前到 0.00–0.08
```

### 20.3 trim 模式下尾巴不好用

修正：

```text
1. 重新生成更长一点的主 shot 以获得更好的 handle
2. 提高过渡桥的清晰度
3. 降低主 shot 后段 image_strength，释放更多连续动作空间
```

---

## 21. 执行检查清单（v1.5 增补）

生成 LTX 参数前检查：

```text
1. 是否读取了用户 idea？
2. 是否读取了 workflow_summary.json？
3. 是否使用 final_frames 或 edit_frames 作为参考帧？
4. 是否根据 story_state 判断参考帧功能？
5. 是否考虑用户目标总时长？
6. 是否区分了连续动作和镜头切换？
7. 是否默认生成了相邻主 shot 之间的 bridge candidates？
8. 是否明确 bridge 只是可选转场资产，最终是否使用由用户或后期流程决定？
9. 是否明确 stitch_mode 是 direct 还是 trim_overlap？
10. 若是 direct，边界帧是否靠近 shot 边界？
11. 若是 trim_overlap，是否明确后期会裁剪 handle？
12. prompt 是否英文、单段、4 到 8 句、现在时？
13. prompt 是否包含 shot、scene、character、action、camera、audio？
14. bridge prompt 是否写清楚转场载体，而不是普通 frame03 -> frame04 描述？
15. bridge 是否可被用户跳过？
16. negative_prompt 是否包含静态画面抑制项和随机转场抑制项？
17. 多参考帧 shot 内部是否写清楚转场细节，而不是让模型随机转场？
18. 各 shot 的拼接点是否自然？
19. 是否给出最终参数代码块？
```

---

## 22. 一句话总结

```text
v1.5 upgrades the LTX workflow by making bridge shots default-generated but optional-to-use transition assets. Bridge generation no longer depends on LLM necessity judgment, while final bridge usage remains user-selectable. Every bridge must contain an explicit transition carrier, and boundary anchor positions still follow either Direct Concatenation or Trim-and-Overlap mode.
```

中文概括：

```text
v1.5 将 LTX 工作流进一步升级：bridge shot 默认生成，但最终是否使用由用户或后期流程决定；bridge 的价值在于提供明确转场载体，而不是机械填补两个 shot 的空隙。边界参考帧的位置仍然服从“直接拼接模式”或“裁剪余量模式”。
```

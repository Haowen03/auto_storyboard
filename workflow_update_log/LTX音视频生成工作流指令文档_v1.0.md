# LTX 音视频生成工作流指令文档 v1.0

> 适用阶段：本工作流用于 **多宫格分镜参考帧生成工作流完成之后**，即已经获得用户初始 idea、`workflow_summary.json` 以及 `case_final_frameXX.png` 或 `case_edit_frameXX.png` 等参考帧之后，进一步规划并生成 LTX 音视频 shot 参数。
>
> 注意：本文档不是参考帧生成工作流的合并版本，而是其下游的 **LTX 参考图生视频与音频提示词规划工作流**。

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
8. 多个 shot 拼接时如何避免动作停顿、动量断裂和幻灯片感。

核心原则：

```text
Reference frames are temporal anchors, not mandatory start/end frames.
LTX shots should preserve story continuity, camera continuity, motion momentum, and audio continuity.
Prompt planning must follow the user's idea, workflow_summary.json, and the actual generated reference frames.
```

中文概括：

```text
参考帧是视频内部的剧情支点和镜头支点，不是必须作为每段视频首尾帧使用。LTX 分镜规划必须同时考虑剧情连续、环境一致、镜头切换、动作动量、总时长和音频氛围。
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
```

### 1.1 参考帧优先级

默认使用顺序：

```text
case_final_frameXX.png > case_edit_frameXX.png > case_base_frameXX.png
```

原因：

- `case_final_frameXX.png` 通常是最终确认帧；
- `case_edit_frameXX.png` 通常包含镜头再设计结果，适合视频镜头多样性；
- `case_base_frameXX.png` 更适合剧情骨架，不一定适合最终视频镜头。

如果 `workflow_summary.json` 中明确标注 `final_frames` 的 `source` 字段，则应优先使用该字段确定最终参考图。

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

推荐结构：

```text
[STYLE / SHOT]. [SCENE with lighting, atmosphere, texture, time of day]. [CHARACTER details]. [ACTION unfolds from beginning to end in present tense]. [CAMERA movement relative to subject]. [AUDIO details]. [Emotion shown through posture, gesture, or facial expression].
```

### 3.1 Prompt 写法要点

必须写清楚：

- 当前 shot 的镜头类型；
- 当前 shot 的环境与光线；
- 当前人物、服饰、道具；
- 动作从哪里开始、如何推进、到哪里结束；
- 运镜方式，例如 tracking、push in、pull back、pan、over-the-shoulder、low-angle、overhead；
- 音频，包括环境声、脚步、布料、纸张、水声、魔法声、笑声、风声、音乐氛围等；
- 与前后 shot 的承接状态。

避免写：

- 只有静态画面描述；
- 只有参考帧复述；
- 过多抽象情绪标签；
- 同时堆叠 zoom、pan、rotate、tilt 等复杂运镜；
- 将字幕、新闻包装、角色卡片等高风险词放在正向 prompt 中；
- 长篇中文对白直接塞进英文 prompt。

---

## 4. Negative Prompt 标准模板

基础模板：

```text
subtitles, captions, lower-third, chyron, nameplate, news broadcast, TV graphics, UI overlay, watermark, logo, title card, floating text, speech bubble, comic annotation, manga annotation, character introduction overlay, vertical subtitles, vertical title card, duplicate character, extra people, distorted hands, broken fingers, extra fingers, fused fingers, warped face, unstable scene layout, sudden camera jump
```

### 4.1 动作类 shot 追加项

追逐、奔跑、打斗、喷水、飞行等高动态镜头应追加：

```text
freeze frame, still image sequence, slideshow effect, static pose, mannequin pose, repeated identical pose, distorted running pose, broken legs, excessive motion blur, disappearing subject
```

### 4.2 道具或商品 shot 追加项

商品广告或核心道具清晰可见时应追加：

```text
warped product shape, deformed prop, missing nozzle, wrong product color, inconsistent product design, disappearing product, detached water stream, missing water stream
```

### 4.3 魔法、发光文字、符文类 shot 注意

如果剧情本身需要“发光文字、符文、光符、山河图、文字法术”等视觉元素，不要在 negative_prompt 中简单写 `text`，否则可能压掉目标元素。应改成：

```text
subtitles, captions, lower-third, UI overlay, modern fonts, flat graphic overlay, dialogue captions, glyphs arranged like subtitle lines, readable modern text, watermark, logo
```

---

## 5. LTX Shot Planning 总流程

```text
Step 1  读取用户初始 idea、目标总时长、workflow_summary.json 和参考帧文件
Step 2  建立 Reference Frame Inventory，列出每张参考帧的 story_state、camera、focus、motion potential
Step 3  判断参考帧之间的关系：连续动作、镜头切换、情绪缓冲、产品特写、收束帧
Step 4  根据总时长规划 shot 数量和每个 shot 的时长
Step 5  决定每个 shot 使用单帧、多帧，或长 shot 多参考帧
Step 6  为每个 shot 设置 images、image_idxs、image_strengths、video_seconds
Step 7  按 LTX prompt 规范生成英文 prompt 与 negative_prompt
Step 8  输出 LTX Shot Plan 表和逐 shot 参数代码块
Step 9  给出拼接建议和常见失败修正策略
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
| frame04 | case_final_frame04.png | 结果反应 | reaction shot | medium | next shot or reaction anchor |

### 6.1 Motion Potential 判断

| 类型 | 说明 | LTX 用法 |
|---|---|---|
| low | 人物静坐、凝视、收束、情绪停顿 | 单帧 5 到 6 秒 |
| medium | 走近、抬手、转身、观察、轻动作 | 单帧 5 到 6 秒或双帧 8 秒 |
| high | 奔跑、追逐、打斗、爆发、喷水、飞行 | 多帧 5 到 8 秒，必要时降低 image_strength |
| transition | 明显切镜、从全景到近景、从过肩到低角度 | 单独成 shot 或作为长 shot 内部锚点 |
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

推荐设置：

```python
images = [f"{FRAMES_DIR}/case_final_frame04.png"]
image_idxs = [0.35]
image_strengths = [1.0]
video_seconds = 5
```

如果该 shot 动作很强，可用：

```python
image_idxs = [0.50]
image_strengths = [0.90]
video_seconds = 6
```

### 7.2 什么时候使用多帧参考

以下情况适合多帧参考生成一个连续 shot：

1. 多张参考帧表现同一动作的起承转合；
2. 参考帧之间机位差异不大，主要是动作推进；
3. 用户希望这段动作不要硬切；
4. 动作需要完整连续性，例如拆信、奔跑追逐、举枪喷水、触碰玉简、能量爆发；
5. 多张参考帧已经经过优化，角色、道具、环境一致。

推荐设置：

```python
images = [
    f"{FRAMES_DIR}/frame01.png",
    f"{FRAMES_DIR}/frame02.png",
]
image_idxs = [0.10, 0.72]
image_strengths = [1.0, 1.0]
video_seconds = 8
```

三帧连续动作可用：

```python
images = [
    f"{FRAMES_DIR}/frame01.png",
    f"{FRAMES_DIR}/frame02.png",
    f"{FRAMES_DIR}/frame03.png",
]
image_idxs = [0.10, 0.48, 0.82]
image_strengths = [0.90, 0.90, 0.90]
video_seconds = 6
```

### 7.3 什么时候使用长 shot

如果参考帧较少，但每张参考帧都具有强镜头多样性和剧情连续性，可以使用一个 10 到 15 秒左右的长 shot，让 LTX 在 shot 内部完成镜头切换。

适用条件：

1. 总参考帧数量较少，例如 3 到 5 张；
2. 参考帧之间剧情紧密；
3. 用户希望减少拼接点；
4. 每张参考帧本身已经是稳定的镜头支点；
5. 本地 LTX 配置支持较长时长。

长 shot 不建议滥用。人物多、动作复杂、商品形象敏感或场景差异大的案例，仍建议拆成多个短 shot。

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
| 多参考长 shot | 10 到 15 秒 | 仅在参考帧少且剧情连续时使用 |

### 8.2 整数秒规则

除非用户指定小数，默认使用整数秒：

```text
7.5s -> 8s
4.5s -> 5s
5.5s -> 6s
```

### 8.3 总时长分配

先确定总时长，再分配到 shot：

```text
目标总时长 = 20s：3 到 4 个 shot
目标总时长 = 30s：5 到 7 个 shot
目标总时长 = 45s：7 到 10 个 shot
```

如果用户说“不限制 20s 内”，可以根据剧情自然扩展，但仍要避免每个 shot 拖沓。

---

## 9. image_idxs 设置规则

### 9.1 核心原则

参考帧不一定放在首帧或尾帧。它们可以放在视频内部，用作动作和镜头的时间支点。

```text
Do not force every reference image to be the first or last frame.
Leave motion before and after each anchor.
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
| 多帧长 shot | 根据剧情节奏均匀分布，但最后一帧一般不放到 `1.0` |
| 收尾镜头 | `[0.30]` 到 `[0.40]`，后半段留给情绪自然结束 |

### 9.3 避免的问题

不推荐：

```python
image_idxs = [0.0, 1.0]
```

除非明确需要首尾锁定，否则这种设置容易导致下一段拼接时出现停顿感。

更推荐：

```python
image_idxs = [0.08, 0.78]
```

或：

```python
image_idxs = [0.12, 0.50, 0.82]
```

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

强动态如奔跑、追逐、跳跃、打斗、喷水追逐时，推荐先试：

```python
image_strengths = [0.90, 0.90, 0.90]
```

如果人物身份或商品形态漂移，再回到 0.95 或 1.0。

### 10.3 商品广告例外

核心商品清晰可见时，不能为了动作完全牺牲商品形象。若水枪、玉简、法器、角色服饰严重变形，应提高 image_strength 或重新优化参考帧。

---

## 11. Shot 内部动作设计规则

每个 prompt 必须写成完整动作，而不是参考帧静态描述。

### 11.1 静态描述错误示例

```text
The boy is running across the lawn while the father runs after him.
```

这容易生成几张跑步姿势图片。

### 11.2 连续动作正确示例

```text
The boy runs forward with quick small steps, his body bouncing naturally with each stride as he looks back and laughs. The father follows one or two meters behind him, taking longer running steps while sweeping a stream of water through the air toward the boy. The camera tracks sideways and slightly backward with them, keeping both characters moving through the frame instead of holding them in a static pose.
```

### 11.3 必须写出的动作细节

高动态镜头应写清：

- 位移方向；
- 步态或动作节奏；
- 手臂、腿部、衣服、头发的运动；
- 道具作用轨迹；
- 摄像机如何跟随；
- 动作如何结束或自然延续到下一 shot。

---

## 12. Shot 间连续性规则

### 12.1 不要使用“首帧 尾帧 首帧”机械拼接

LTX shot 的参考帧不是必须首尾对齐。机械将一个 shot 的尾帧作为下一 shot 的首帧，容易导致：

- 动作动量不一致；
- 人物突然停住；
- 视频边界有明显暂停；
- 前后段像两段独立短片；
- 结尾变成定格画面。

### 12.2 拼接点选择

优先在以下状态切换：

| 拼接点 | 适用案例 |
|---|---|
| 光效最强时 | 魔法爆发、信封金光、能量柱 |
| 水花遮挡时 | 水枪喷水、落水、喷溅 |
| 人物转身或离开画面时 | 追逐、走位、进入新镜头 |
| 镜头自然切换时 | 从全景到近景、从近景到反应 |
| 情绪停顿后 | 大笑、凝视、接招、收束 |

### 12.3 每段结尾留动作余量

最后一个参考帧一般不要放到 `1.0`，而应在后面留 15% 到 25% 的时间，让动作自然继续。

例如：

```python
image_idxs = [0.76]
video_seconds = 6
```

表示参考帧在 4.56 秒左右出现，后面还有 1.44 秒用于自然延续。

---

## 13. 镜头多样性规则

LTX 阶段应继承参考帧生成阶段的镜头多样性，但不必让每个 shot 都强行换镜头。重点是根据参考帧功能决定：

```text
连续动作段：镜头可以跟拍或轻微变化，重点保持动量。
镜头切换段：可以单帧参考，让 LTX 生成完整镜头语义。
产品或手部细节：使用 medium close-up 或 insert shot。
反应镜头：使用 medium close-up 或 side reaction shot。
收束镜头：使用 wide ending shot 或 quiet medium shot。
```

最终视频中应至少包含：

- 开场建立镜头；
- 一个动作推进镜头；
- 一个细节或产品镜头；
- 一个反应或情绪镜头；
- 一个收束镜头。

如果参考帧数量较少，可以让一个长 shot 内部完成多种镜头变化，例如从 close-up 拉到 over-the-shoulder，再扩展到 wide shot。

---

## 14. 音频设计规则

LTX prompt 的最后一到两句应包含音频设计。

### 14.1 环境音

根据场景加入：

```text
soft night wind, distant insects, wooden creaks, room tone, garden ambience, faint traffic hum, distant thunder
```

### 14.2 动作音效

根据动作加入：

```text
paper tearing, cloth movement, footsteps on grass, water splashing, magical hum, crackling sparks, chair creak, breathing, laughter
```

### 14.3 音乐

广告或情绪片可加入轻音乐，但不要过度复杂：

```text
a light upbeat summer music bed
an airy magical music swell
soft emotional strings
```

### 14.4 对白

如果需要对白，推荐简短意图描述，不推荐长文本台词。若字幕伪影严重，改写为：

```text
he speaks briefly in Mandarin with natural mouth movement
she responds softly with synchronized mouth movement
```

不要在 prompt 中塞长段中文台词。

---

## 15. 输出格式

每次 LTX 工作流输出应包含两部分。

### 15.1 Shot Plan 表

| Shot | Reference Images | Duration | image_idxs | Shot Function | Transition Notes |
|---|---|---:|---|---|---|
| Shot 1 | frame01 | 6s | [0.0] | 开场建立 | 结尾保持动作余量 |
| Shot 2 | frame02, frame03 | 8s | [0.12, 0.76] | 连续动作 | 在动作进行中切出 |

### 15.2 参数代码块

格式固定为：

```python
images = [
    f"{FRAMES_DIR}/case_final_frame01.png",
    f"{FRAMES_DIR}/case_final_frame02.png",
]
image_idxs = [0.10, 0.72]
image_strengths = [1.0, 1.0]
video_seconds = 8

prompt = "Cinematic ..."

negative_prompt = "subtitles, captions, lower-third, ..."
```

如果用户要求 `canshu.py` 格式，则按其脚本结构输出或生成 `.py` 文件。

---

## 16. Shot Resolver 输出格式

在正式参数前，大模型应内部完成 Shot Resolver 判断。必要时可以显式输出：

```json
{
  "shot": "shot_03",
  "story_function": "the boy fires the water blaster and the father reacts",
  "reference_images": [
    "case_final_frame03.png",
    "case_final_frame04.png"
  ],
  "selection_reason": {
    "case_final_frame03.png": "product-focused firing moment",
    "case_final_frame04.png": "reaction result after being sprayed"
  },
  "duration_reason": "two-frame action-and-reaction shot, 8 seconds gives enough time for firing, water travel, and reaction",
  "image_idx_reason": "first anchor appears early as product shot, second anchor appears later as reaction result, leaving tail motion after impact",
  "continuity_notes": "cut from previous shot when the boy raises the blaster; cut to next shot after the father laughs and accepts the game"
}
```

---

## 17. 参考帧组合策略

### 17.1 连续动作型

例：打开信封、按下玉简、奔跑追逐、举枪喷水。

```text
多帧合并，image_idxs 分散在 shot 内部，prompt 强调连续动作和镜头跟随。
```

### 17.2 镜头切换型

例：全景切近景、过肩镜头、低角度大全景、俯视镜头。

```text
通常单帧独立成 shot，或作为长 shot 内部中段支点。
```

### 17.3 情绪缓冲型

例：两人对视大笑、人物沉默凝视、父亲笑着接招。

```text
单帧 5 到 6 秒，prompt 写动作余韵，不写静态定格。
```

### 17.4 产品展示型

例：水枪近景、道具特写、手部触碰、核心商品发射。

```text
单帧或双帧。需要强锁定商品形状，水流、喷嘴、握持关系必须清楚。
```

### 17.5 收束型

例：光字消散、魔法余波、人物收起道具、父子停下大笑。

```text
单帧 5 到 6 秒。参考帧作为中前段锚点，后面留给余波自然结束。
```

---

## 18. 常见失败与修正

### 18.1 像几张图片在放映

原因：

- prompt 只描述静态状态；
- image_strength 太高；
- image_idxs 过于平均且动作描述不足；
- 多帧之间动作差异大但未写动量；
- 参考帧过多导致模型被锁死。

修正：

```text
1. 降低 image_strengths 到 0.90 或 0.85
2. 减少参考帧数量
3. 强化连续步态、手臂摆动、衣物运动、水流轨迹等动作描述
4. 加入 camera tracks / follows / moves with them
5. negative_prompt 加 freeze frame, still image sequence, slideshow effect, static pose
```

### 18.2 shot 结尾戛然而止

修正：

```text
1. 单帧 shot 提升到 5 或 6 秒
2. 双帧 shot 提升到 8 秒
3. 最后一张参考帧不要放到 1.0，改到 0.72 到 0.82
4. prompt 结尾写动作余韵或自然延续
```

### 18.3 人物或商品漂移

修正：

```text
1. image_strength 提高到 0.95 或 1.0
2. prompt 明确服饰、道具颜色、核心结构
3. 使用更稳定的参考帧
4. 如果上游参考帧商品已经变形，先重新编辑参考帧，不要直接让 LTX 修正
```

### 18.4 水柱、光效、动作轨迹不稳定

修正：

```text
1. 在 prompt 中写清楚轨迹从哪里开始、到哪里结束
2. 明确 nozzle, stream, arc, droplets, impact point
3. 魔法光效写 core, beam, sparks, particles, reflection
4. 必要时先优化参考帧中的水柱或光效
```

### 18.5 最后一段变成定格画面

修正：

```text
1. 不要在 prompt 中写 poster-like ending、freeze、hold as final frame
2. 收尾 shot 写完整动作：走近、停下、呼吸、对视、笑、放松、余波消散
3. 定格、logo、slogan 留给后期剪辑，不让 LTX 在生成阶段完成
```

---

## 19. 执行检查清单

生成 LTX 参数前检查：

```text
1. 是否读取了用户 idea？
2. 是否读取了 workflow_summary.json？
3. 是否使用 final_frames 或 edit_frames 作为参考帧？
4. 是否根据 story_state 判断参考帧功能？
5. 是否考虑用户目标总时长？
6. 是否区分了连续动作和镜头切换？
7. 是否避免把所有参考帧都机械设置为首尾帧？
8. 是否为每个 shot 设置合理整数秒？
9. image_idxs 是否单调递增？
10. 最后一张参考帧是否避免无必要地放在 1.0？
11. prompt 是否英文、单段、4 到 8 句、现在时？
12. prompt 是否包含 shot、scene、character、action、camera、audio？
13. negative_prompt 是否包含字幕和静态画面抑制项？
14. 各 shot 的拼接点是否自然？
15. 是否给出最终参数代码块？
```

---

## 20. 输出示例模板

```python
images = [
    f"{FRAMES_DIR}/case_edit_frame01.png",
    f"{FRAMES_DIR}/case_edit_frame02.png",
]
image_idxs = [0.10, 0.72]
image_strengths = [1.0, 1.0]
video_seconds = 8

prompt = "Cinematic summer backyard commercial video, beginning with a relaxed establishing view of the father seated in the sunny backyard before the boy enters with a blue shark-shaped water blaster. Warm golden-hour light filters through the trees, the green lawn and wooden deck remain stable, and a glass of lemonade sits on the table as a summer lifestyle anchor. The boy walks closer with a playful smile, holding the water blaster in both hands while the father turns toward him and notices the challenge. The camera gently tracks from the table foreground toward the two characters, shifting the quiet afternoon into a playful family moment. Soft outdoor ambience, leaves moving in the breeze, distant neighborhood sounds, and light laughter carry the scene forward."

negative_prompt = "subtitles, captions, lower-third, chyron, nameplate, UI overlay, watermark, title card, floating text, speech bubble, comic annotation, character introduction overlay, extra people, duplicate father, duplicate child, distorted hands, broken fingers, extra fingers, warped water blaster, unstable backyard layout, sudden camera jump"
```

---

## 21. 一句话总结

```text
LTX 音视频生成工作流的核心不是逐帧转视频，而是把参考帧重新组织为时间支点：连续动作可多帧合并，镜头切换可单帧独立，参考帧可放在 shot 内任意位置，并通过英文 prompt 明确动作、运镜、音频和拼接连续性。
```

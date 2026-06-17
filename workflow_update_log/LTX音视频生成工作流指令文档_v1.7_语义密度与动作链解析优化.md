# LTX 音视频生成工作流指令文档 v1.7

> v1.7 更新重点：在 v1.6 的“单 Shot 参考帧密度与容量优化”基础上，进一步新增 **语义密度、动作链解析、高风险动作对拆桥、重叠锚点复用** 四类规则。目标是避免大模型僵硬地按“4 张或 5 张参考帧”机械切分，而是让模型先理解每张参考帧的剧情功能和动作关系，再决定 shot 拆分、bridge 位置和 prompt 组织方式。

---

## 0. 本次修正的核心问题

v1.6 已经解决了一个重要问题：单个 shot 不能塞入过多参考帧，否则会像播放 PPT。但进一步实验表明，仅靠“参考帧数量 / shot 秒数”的密度规则仍然不够。

原因是：

```text
同样是 12s 使用 5 张参考帧，效果可能完全不同。
```

如果 5 张参考帧属于同一条连续动作链，例如：

```text
脚踩稳飞剑 -> 飞剑升空 -> 加速穿过竹林 -> 穿出竹林 -> 远飞云海
```

那么 12s 内是可以成立的。

但如果 5 张参考帧包含多个独立剧情阶段，例如：

```text
站立建立 -> 施法 -> 飞剑显现 -> 飞剑下降 -> 脚踩上飞剑
```

虽然也是 5 张，但它实际上包含多个语义阶段，容易拥挤。

因此，v1.7 的核心修正是：

```text
Do not only count reference frames.
Parse the story function, semantic stage, action chain, and high-risk transitions between frames.
```

中文概括：

```text
不要只数参考帧张数，而要解析每张参考帧对应的剧情功能、语义阶段、动作链关系和高风险动作对。
```

---

## 1. v1.7 核心原则

### 1.1 参考帧不是等价节点

每张参考帧承担的任务不同。有的帧只是同一动作链中的中间状态，有的帧则代表新的剧情阶段或高风险动作节点。

大模型必须先回答：

```text
What does this frame do in the story?
Is this frame a new semantic stage, a continuous motion state, a high-risk physical contact, or a transition anchor?
```

### 1.2 Shot 拆分不应机械按数量

错误：

```text
12s shot 最多 4–5 张参考帧，所以随便取连续 4–5 张。
```

正确：

```text
先识别动作链与语义阶段，再决定哪些帧应合并，哪些帧应拆成 micro-bridge，哪些帧应作为相邻 shot 的重叠锚点。
```

---

## 2. Reference Frame Semantic Parser

在规划 shot 前，必须先为每张参考帧建立语义解析表。

### 2.1 每张帧需要解析的字段

```json
{
  "frame": "case_final_frame05.png",
  "visible_state": "Xiaolongnu's right foot steps onto the ice sword",
  "semantic_stage": "boarding",
  "action_role": "high-risk physical contact",
  "motion_direction": "downward foot placement",
  "camera_role": "side or low contact emphasis",
  "risk_level": "high",
  "suggested_use": "micro-action bridge endpoint"
}
```

### 2.2 推荐语义阶段类型

| semantic_stage | 说明 | 典型帧 |
|---|---|---|
| establishing | 建立人物与场景关系 | 人物站在场景中 |
| preparation | 动作准备、抬手、蓄势 | 施法手势、起跑前 |
| manifestation | 道具或能量出现 | 飞剑显现、光效生成 |
| positioning | 道具移动到可用位置 | 飞剑下降到脚前 |
| contact | 身体与道具接触 | 脚踩剑、手触碰、握住 |
| boarding | 站上载具或进入动作平台 | 双脚上剑、坐上坐骑 |
| lift_off | 离地、升空、起跳 | 飞剑升空 |
| acceleration | 加速、奔跑、冲刺 | 御剑前冲 |
| breakthrough | 穿过遮挡或边界 | 冲出竹林、破云 |
| ascent | 上升到更高空间 | 高空飞行 |
| departure | 远离、收束、消失到远方 | 云海远景 |
| reaction | 人物反应 | 表情、回头、惊讶 |
| aftermath | 余波与收束 | 光效消散、安静停留 |

---

## 3. Action Chain Graph

### 3.1 建立相邻帧关系

对每一对相邻参考帧，判断它们属于哪种关系：

| relation_type | 说明 | 处理方式 |
|---|---|---|
| same_motion_chain | 同一动作方向连续推进 | 可合并进同一 main shot |
| semantic_stage_shift | 进入新的剧情阶段 | 可能需要拆 shot 或 bridge |
| high_risk_pair | 高风险身体或道具接触 | 优先拆成 micro-action bridge |
| camera_jump | 景别或机位大幅变化 | 需要 transition bridge 或单独 shot |
| environment_shift | 场景空间发生变化 | 需要明确转场载体 |
| closure_shift | 进入收束阶段 | 可作为最后 shot 后段 |

### 3.2 输出示例

```json
{
  "frame_pair": "frame04 -> frame05",
  "relation_type": "high_risk_pair",
  "reason": "the ice sword hovers near the shoes, then the right foot steps onto the sword blade",
  "recommended_split": "micro_action_bridge",
  "bridge_duration": "3-4s",
  "anchor_reuse": true
}
```

```json
{
  "frame_pair": "frame05 -> frame09",
  "relation_type": "same_motion_chain",
  "reason": "all frames continue the sword-flight action from boarding to lift-off, acceleration, ascent, and distant departure",
  "recommended_split": "same_main_shot_if_duration_allows"
}
```

---

## 4. Semantic Event Density Rule

v1.6 的 reference frame density 仍然保留，但 v1.7 新增 semantic event density。

### 4.1 定义

```text
semantic_event_density = number_of_major_semantic_stages / shot_seconds
```

其中 major semantic stage 不等于参考帧数量。

例如：

```text
frame05 -> frame06 -> frame07 -> frame08 -> frame09
```

虽然有 5 张参考帧，但它们可以被视为一条连续 flight chain，因此主要语义阶段可以压缩为：

```text
boarding/lift-off -> acceleration/ascent -> departure
```

而：

```text
frame01 -> frame02 -> frame03 -> frame04 -> frame05
```

则包含：

```text
establishing -> preparation -> manifestation -> positioning -> contact
```

语义阶段更多，12s 内会更紧。

### 4.2 规则

```text
For a 12s shot, 3–4 reference frames are generally safe.
5 reference frames are acceptable only if they form one continuous action chain.
If the 5 frames contain 4–5 independent semantic stages, split one high-risk pair into a micro-bridge.
```

中文：

```text
12s shot 推荐 3–4 张参考帧。5 张可以接受，但前提是它们属于同一连续动作链。如果 5 张参考帧包含 4–5 个独立语义阶段，应拆出一个高风险动作对作为 micro-bridge。
```

---

## 5. Continuous Motion Chain Exception

### 5.1 允许例外

当多张参考帧属于同一个连续动作方向时，可以适当提高单 shot 内参考帧数量。

判断条件：

```text
1. 主体动作方向一致；
2. 角色状态没有频繁重置；
3. 道具功能保持一致；
4. 镜头变化服务于同一动作，而不是进入多个新分镜；
5. prompt 可以写成一条连续动作曲线，而不是逐帧罗列。
```

### 5.2 示例

```text
frame05 -> frame06 -> frame07 -> frame08 -> frame09
```

可以合并为：

```text
foot settles on sword -> sword lifts -> accelerates through bamboo -> breaks above canopy -> flies into cloud sea
```

这是一条连续 flight chain。即使 12s 使用 5 张参考帧，也可能成立。

---

## 6. High-Risk Pair Micro-Bridge Rule

### 6.1 高风险动作对定义

以下相邻帧如果直接塞进主 shot，容易生成不稳定，应优先拆成 micro-action bridge：

| 高风险动作对 | 风险 |
|---|---|
| 手触碰道具 | 手指变形、接触不准 |
| 脚踩上载具 | 脚部畸形、接触漂移、重心不稳 |
| 握住武器 | 握持关系错误 |
| 道具套上手腕 | 道具位置漂移 |
| 人物落地 | 重心断裂、姿势跳变 |
| 起飞 / 离地 | 身体与地面关系突变 |
| 击中 / 碰撞 | 冲击点不清 |
| 进入载具 / 坐上道具 | 身体与物体穿模 |

### 6.2 micro-action bridge 的作用

micro-action bridge 不是普通跨-shot bridge，而是专门处理一个高风险动作对。

推荐形式：

```text
Micro-Bridge: frameA -> frameB
Duration: 3–4s
Reference count: 2
image_idxs: direct 模式 [0.05, 0.90]
Function: make the physical contact or state transition readable
```

### 6.3 御剑案例

```text
frame04: ice sword hovers just above moss in front of her shoes
frame05: right foot steps onto the sword blade
```

这是典型 high-risk pair，应优先拆成 3s 或 4s micro-action bridge，而不是塞进 12s 的召唤主 shot 尾部。

---

## 7. Overlap Anchor Reuse Rule

当存在 high-risk pair 或关键过渡时，应复用边界锚点。

标准结构：

```text
Main Shot 1: frame01 -> frame02 -> frame03 -> frame04
Micro-Bridge: frame04 -> frame05
Main Shot 2: frame05 -> frame06 -> frame07 -> frame08 -> frame09
```

这里：

- frame04 同时作为 Main Shot 1 的结尾与 Micro-Bridge 的起点；
- frame05 同时作为 Micro-Bridge 的终点与 Main Shot 2 的起点。

这样可以避免：

```text
Main Shot 1: frame01 -> frame02 -> frame03 -> frame04 -> frame05
Bridge: frame05 -> frame06
```

因为后者会把 “脚踩上飞剑” 这个关键动作压缩到主 shot 尾部，反而不如单独拆出来稳定。

---

## 8. Shot Segmentation Algorithm v1.7

### 8.1 规划步骤

```text
Step 1  解析每张参考帧的 semantic_stage、action_role、motion_direction、risk_level。
Step 2  为每对相邻参考帧建立 relation_type。
Step 3  标记 high_risk_pair，例如 contact、boarding、lift_off、impact。
Step 4  识别 continuous motion chain，例如 flight chain、running chain、water-spray chain。
Step 5  优先把 high_risk_pair 拆成 2 帧 micro-action bridge。
Step 6  将剩余连续动作链合并进 main shot，但受 shot duration 与 capacity rule 限制。
Step 7  对每个主 shot 计算 reference_frame_density 与 semantic_event_density。
Step 8  若 frame density 合理但 semantic density 过高，继续拆 micro-bridge 或新 main shot。
Step 9  输出 main shots + bridge candidates + optional export rules。
```

### 8.2 决策优先级

```text
1. High-risk physical transition must be readable.
2. Continuous motion chain should stay continuous if duration allows.
3. Main shot should not contain too many independent semantic stages.
4. Bridge shots should reduce crowding, not add unnecessary plot.
5. Reference frame count is a constraint, not the only decision rule.
```

---

## 9. Bridge 类型重新定义

v1.7 中 bridge 至少分为两类。

### 9.1 Transition Bridge

用于两个主 shot 之间的视觉转场。

典型载体：
- light wipe
- fire ribbon wipe
- water splash wipe
- motion blur wipe
- camera whip
- object occlusion
- audio bridge

### 9.2 Micro-Action Bridge

用于一个高风险动作对。

典型动作：
- foot steps onto sword
- hand touches artifact
- ring fits onto wrist
- weapon is grasped
- character lands
- object is picked up
- water stream hits target

### 9.3 输出字段建议

```json
{
  "shot_id": "bridge_04_05",
  "shot_type": "micro_action_bridge",
  "bridge_subtype": "physical_contact",
  "bridges_between_frames": ["frame04", "frame05"],
  "generated_by_default": true,
  "optional_for_export": true,
  "can_skip_bridge": true,
  "transition_carrier": "robe hem sweep + foot placement + blue contact ripple",
  "risk_reason": "foot-to-sword contact requires a dedicated readable motion"
}
```

---

## 10. Prompt Motion-First Rule

实验表明，好的 prompt 更像连续动作描写，而不是规划报告。

### 10.1 原则

```text
Use prompt capacity for visible motion, camera path, transition carrier, and audio continuity.
Keep safety sentences compact.
```

中文：

```text
prompt 的主要容量应留给可见动作、运镜路径、转场载体和音频连续。安全约束句要短，不要反复写长篇规则解释。
```

### 10.2 不推荐

```text
The sequence stays within the story shown by the reference frames, depicting only the sword summoning and initial boarding without any additional characters, weapons, or scene changes.
```

如果每个 shot 都写得很长，会占用动作描述空间。

### 10.3 推荐压缩

```text
The shot stays grounded in the shown sword-summoning action, with no new characters or props.
```

或直接放到 negative_prompt 中控制。

---

## 11. 御剑案例推荐规划

### 11.1 推荐结构

根据 v1.7 规则，御剑案例更推荐：

```text
Shot 1: frame01 -> frame02 -> frame03 -> frame04
Micro-Bridge: frame04 -> frame05
Shot 2: frame05 -> frame06 -> frame07 -> frame08 -> frame09
```

而不是：

```text
Shot 1: frame01 -> frame02 -> frame03 -> frame04 -> frame05
Bridge: frame05 -> frame06
Shot 2: frame06 -> frame07 -> frame08 -> frame09
```

原因：

```text
frame04 -> frame05 是 foot-to-sword contact，高风险动作对。
frame05 -> frame09 是连续御剑飞行动作链，可以在 12s 中合并。
```

### 11.2 推荐参数示例

```python
# Shot 1: summoning and sword positioning
images = [
    f"{FRAMES_DIR}/case_final_frame01.png",
    f"{FRAMES_DIR}/case_final_frame02.png",
    f"{FRAMES_DIR}/case_final_frame03.png",
    f"{FRAMES_DIR}/case_final_frame04.png",
]
image_idxs = [0.00, 0.30, 0.62, 0.92]
image_strengths = [0.94, 0.92, 0.92, 0.95]
video_seconds = 12
```

```python
# Micro-Bridge: foot steps onto sword
images = [
    f"{FRAMES_DIR}/case_final_frame04.png",
    f"{FRAMES_DIR}/case_final_frame05.png",
]
image_idxs = [0.05, 0.90]
image_strengths = [0.95, 0.95]
video_seconds = 3
shot_type = "micro_action_bridge"
bridge_subtype = "physical_contact"
transition_carrier = "robe hem sweep + foot placement + blue contact ripple"
```

```python
# Shot 2: continuous flight chain
images = [
    f"{FRAMES_DIR}/case_final_frame05.png",
    f"{FRAMES_DIR}/case_final_frame06.png",
    f"{FRAMES_DIR}/case_final_frame07.png",
    f"{FRAMES_DIR}/case_final_frame08.png",
    f"{FRAMES_DIR}/case_final_frame09.png",
]
image_idxs = [0.05, 0.28, 0.52, 0.72, 0.88]
image_strengths = [0.95, 0.93, 0.90, 0.90, 0.93]
video_seconds = 12
```

---

## 12. 自动拒绝规则 v1.7

在 v1.6 的基础上新增以下拒绝规则：

```text
1. 如果 12s shot 使用 5 张参考帧，但这些帧包含 4 个以上独立 semantic stages，则要求重拆。
2. 如果出现 contact / boarding / grasping / landing / impact 等 high-risk pair，却没有单独考虑 micro-bridge，则要求重评估。
3. 如果模型只按每 4 张或每 5 张机械切分，而没有输出 action chain analysis，则要求重写规划。
4. 如果 prompt 只是逐帧复述，而不能写成连续动作曲线，则判为不合格。
5. 如果 bridge 没有 transition_carrier 或 micro-action carrier，则判为无效 bridge。
```

---

## 13. 输出结构建议 v1.7

规划输出应包含以下新增字段。

### 13.1 frame_semantic_inventory

```json
"frame_semantic_inventory": [
  {
    "frame": "frame04",
    "semantic_stage": "positioning",
    "action_role": "sword hover before contact",
    "risk_level": "medium"
  },
  {
    "frame": "frame05",
    "semantic_stage": "contact / boarding",
    "action_role": "foot-to-sword contact",
    "risk_level": "high"
  }
]
```

### 13.2 frame_pair_relations

```json
"frame_pair_relations": [
  {
    "pair": "frame04->frame05",
    "relation_type": "high_risk_pair",
    "recommended_handling": "micro_action_bridge"
  },
  {
    "pair": "frame05->frame09",
    "relation_type": "continuous_motion_chain",
    "recommended_handling": "same_main_shot_if_duration_allows"
  }
]
```

### 13.3 shot_capacity_review

不要混入 `shot_plan_table`，应独立输出：

```json
"shot_capacity_review": [
  {
    "shot_id": "shot_01",
    "video_seconds": 12,
    "reference_frame_count": 4,
    "semantic_stage_count": 3,
    "reference_frame_density": 0.33,
    "semantic_density_verdict": "ok"
  }
]
```

---

## 14. 一句话总结

```text
v1.7 upgrades shot planning from frame-count control to semantic-action planning: the model must parse each reference frame's story role, identify continuous motion chains and high-risk physical transitions, split micro-action bridges when needed, and only then decide how many frames each shot should use.
```

中文概括：

```text
v1.7 将 LTX 规划从“参考帧数量控制”升级为“语义动作链规划”：大模型必须先解析每张参考帧的剧情作用，识别连续动作链与高风险动作对，再决定主 shot、micro-bridge 和参考帧数量，而不是机械地每段选 4 张或 5 张。
```

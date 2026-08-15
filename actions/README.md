# 动作包

每个动作目录都是可独立接入生命周期的资源包：

- `manifest.json`：状态、触发方式、帧率、锚点及播放规则
- `*-once.gif`：透明背景、只播放一次的交付动画
- 其余 GIF / `preview.webp`：循环预览
- `preview.webp`：循环预览
- `frames/`：透明 PNG 帧序列
- `source/`：用户提供的原始动作参考

## 当前动作

### `stand_tap_button`

守岸人从趴伏姿势撑起身体、敲击左侧按钮，再回到趴伏姿势。

- 类型：`interaction`
- 推荐触发：`interaction.button_tap`
- 播放：单次、不可打断
- 完成后：返回 `awake.idle`

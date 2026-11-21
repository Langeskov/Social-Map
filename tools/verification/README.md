# tools/verification

可重复的验收辅助脚本（从干净仓库即可执行）。

- `phase12_layout_probe.py` — 离屏布局探测：多分辨率 × 多缩放渲染核心页面，
  检查控件裁切、横向溢出、核心操作可见性与调查工作台分区尺寸，输出 JSON + PNG。
  **这不是 Windows DPI 人工验收**（离屏平台不加载原生字体与控件度量）。

用法见脚本头注释与 `docs/phase12-verification.md`。输出默认写入被忽略的
`.verification/phase12-layout/`，截图作为工件保存，不入库。

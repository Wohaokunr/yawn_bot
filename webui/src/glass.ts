// 液态玻璃指针光效:为一个委托 pointermove 监听(含 rAF 节流)
// 向命中的玻璃元素写入 --glass-mx/--glass-my 相对坐标,配合
// styles.css 中 ::before 的径向渐变实现"鼠标走到哪里亮到哪里"。
// GLASS_SELECTOR 必须与 styles.css 里光斑层的选择器保持同步。
const GLASS_SELECTOR = ".liquid-glass, .app-content .ant-card, .env-collapse > .ant-collapse-item";

export function installGlassGlow(): void {
  if (typeof document === "undefined" || typeof requestAnimationFrame === "undefined") return;
  if (typeof matchMedia === "function") {
    // 减少动态效果时不安装跟手光斑，避免持续 pointermove + rAF 造成视觉运动。
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    // 粗指针(触屏)没有悬停概念,不启用跟随光斑。
    if (!matchMedia("(pointer: fine)").matches) return;
  }

  let frame = 0;
  let target: HTMLElement | null = null;
  let clientX = 0;
  let clientY = 0;

  const paint = () => {
    frame = 0;
    const node = target;
    target = null;
    if (!node || !node.isConnected) return;
    const rect = node.getBoundingClientRect();
    node.style.setProperty("--glass-mx", `${clientX - rect.left}px`);
    node.style.setProperty("--glass-my", `${clientY - rect.top}px`);
  };

  document.addEventListener(
    "pointermove",
    (event) => {
      target =
        event.target instanceof Element
          ? event.target.closest<HTMLElement>(GLASS_SELECTOR)
          : null;
      if (!target) return;
      clientX = event.clientX;
      clientY = event.clientY;
      if (!frame) frame = requestAnimationFrame(paint);
    },
    { passive: true },
  );
}

// Простой график без библиотек: история метрики камеры (Prometheus, 24 ч).

export function Sparkline({ points, width = 220, height = 36 }: { points: [number, number][]; width?: number; height?: number }) {
  if (points.length < 2) return <span className="muted">—</span>;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  const [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  const sx = (x: number) => ((x - x0) / Math.max(x1 - x0, 1)) * width;
  const sy = (y: number) => height - ((y - y0) / Math.max(y1 - y0, 1e-6)) * (height - 4) - 2;
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(" ");
  return (
    <svg width={width} height={height} className="sparkline" role="img" aria-label="история">
      <path d={d} fill="none" strokeWidth="1.5" />
    </svg>
  );
}

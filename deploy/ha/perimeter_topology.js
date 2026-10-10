// Business Charts / Apache ECharts. Data comes only from Grafana's backend query.
const frames = context.panel.data.series || [];
const rows = [];
const valueAt = (values, i) => typeof values.get === "function" ? values.get(i) : values[i];
for (const frame of frames) {
  for (let i = 0; i < frame.length; i++) {
    const row = {};
    for (const field of frame.fields) row[field.name] = valueAt(field.values, i);
    rows.push(row);
  }
}
const chart = context.panel.chart;
const width = chart.getWidth();
const height = chart.getHeight();
const scale = Math.min(width / 1400, height / 660);
const offsetX = (width - 1400 * scale) / 2;
const offsetY = Math.max(0, (height - 660 * scale) / 2);
const palette = {
  active: ["#49db97", "#14372d"], ready: ["#62ccea", "#153441"],
  warning: ["#f2bd59", "#3b3020"], critical: ["#fa6c78", "#3f2530"],
  control: ["#f2bd59", "#3b3020"], off: ["#728599", "#152231"],
  unknown: ["#8294a8", "#192634"]
};
const cards = new Map(rows.filter(row => ["card", "header"].includes(row.kind)).map(row => [row.id, row]));
const items = [];
const px = x => offsetX + Number(x) * scale;
const py = y => offsetY + Number(y) * scale;
const text = (id, x, y, content, size, color, weight = 500) => ({
  id, type: "text", x: px(x), y: py(y), silent: true,
  style: { text: String(content || ""), font: `${weight} ${Math.max(10, size * scale)}px sans-serif`,
           fill: color, verticalAlign: "middle", align: "left" }
});
// Thin grey routes show reserves; only observed healthy paths are highlighted.
for (const row of rows.filter(row => row.kind === "edge")) {
  const source = cards.get(row.source), target = cards.get(row.target);
  if (!source || !target) continue;
  const active = ["active", "control"].includes(row.state);
  const color = (palette[row.state] || palette.off)[0];
  let points;
  if (Math.abs(Number(source.y) - Number(target.y)) < 15) {
    const rightward = Number(source.x) < Number(target.x);
    points = [[Number(source.x) + (rightward ? Number(source.w) : 0), Number(source.y) + Number(source.h) / 2],
              [Number(target.x) + (rightward ? 0 : Number(target.w)), Number(target.y) + Number(target.h) / 2]];
  } else {
    const sx = Number(source.x) + Number(source.w) / 2;
    const sy = Number(source.y) + Number(source.h);
    const tx = Number(target.x) + Number(target.w) / 2;
    const ty = Number(target.y);
    const mid = (sy + ty) / 2;
    points = [[sx, sy], [sx, mid], [tx, mid], [tx, ty]];
  }
  points = points.map(point => [px(point[0]), py(point[1])]);
  items.push({ id: row.id, type: "polyline", silent: true, z: 0,
    shape: { points }, style: { stroke: color, fill: null, lineWidth: active ? 2.8 : 1,
      lineDash: active ? null : [3, 5], opacity: active ? 1 : .17,
      shadowColor: active ? color : "transparent", shadowBlur: active ? 5 : 0 } });
  if (active) {
    const end = points[points.length - 1], before = points[points.length - 2];
    const angle = Math.atan2(end[1] - before[1], end[0] - before[0]);
    const tip = [end, [end[0] - 8 * Math.cos(angle) + 4 * Math.sin(angle), end[1] - 8 * Math.sin(angle) - 4 * Math.cos(angle)],
                      [end[0] - 8 * Math.cos(angle) - 4 * Math.sin(angle), end[1] - 8 * Math.sin(angle) + 4 * Math.cos(angle)]];
    items.push({ id: row.id + "-head", type: "polygon", silent: true, z: 1, shape: { points: tip }, style: { fill: color } });
  }
}
for (const row of cards.values()) {
  const colors = palette[row.state] || palette.unknown;
  const isHeader = row.kind === "header";
  items.push({ id: row.id + "-box", type: "rect", silent: true, z: 3,
    shape: { x: px(row.x), y: py(row.y), width: Number(row.w) * scale, height: Number(row.h) * scale, r: 7 * scale },
    style: { fill: colors[1], stroke: colors[0], lineWidth: isHeader ? 2 : 1.1, opacity: 1 } });
  items.push({ id: row.id + "-dot", type: "circle", silent: true, z: 4,
    shape: { cx: px(Number(row.x) + 12), cy: py(Number(row.y) + 15), r: 3.5 * scale }, style: { fill: colors[0] } });
  const heading = text(row.id + "-title", Number(row.x) + 22, Number(row.y) + 15, row.label, isHeader ? 17 : 15, "#e8f0f8", 600);
  heading.z = 4;
  items.push(heading);
  const detail = text(row.id + "-detail", Number(row.x) + 12, Number(row.y) + Number(row.h) - 11, row.detail, 11.5, colors[0]);
  detail.z = 4;
  items.push(detail);
}
const legend = [["active", "Активен"], ["ready", "Резерв готов"], ["warning", "Внимание"],
                ["critical", "Отказ"], ["unknown", "Нет данных"], ["off", "Ожидание"]];
legend.forEach(([state, label], i) => {
  items.push(text("legend-" + state, 85 + i * 215, 643, "● " + label, 12, palette[state][0]));
});
if (!cards.size) items.push(text("empty", 500, 300, "Нет свежих данных схемы", 22, palette.warning[0]));
return { backgroundColor: "#0d1622", animation: false,
         graphic: [{ id: "perimeter-layout", type: "group", $action: "replace", children: items }] };

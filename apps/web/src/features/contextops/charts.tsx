"use client";
import { useEffect, useMemo, useRef } from "react";
import * as echarts from "echarts/core";
import { CustomChart, LineChart, ScatterChart, type CustomSeriesOption } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import { compactMs } from "./client";

echarts.use([LineChart, ScatterChart, CustomChart, GridComponent, TooltipComponent, SVGRenderer]);

const ink = "#202b27",
  muted = "#62716a",
  grid = "#edf1ec";

/** One SVG ECharts instance per element; option changes replace the previous option. */
export function EChart({
  option,
  label,
  className,
  height,
}: {
  option: echarts.EChartsCoreOption;
  label: string;
  className?: string;
  height: number;
}) {
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    const node = element.current;
    if (!node) return;
    const instance = echarts.init(node, undefined, { renderer: "svg" });
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(node);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);
  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    chart.current?.setOption({ animation: !reduced, animationDuration: 280, ...option }, true);
  }, [option]);
  return (
    <div ref={element} className={className} style={{ height }} role="img" aria-label={label} />
  );
}

export function Sparkline({
  values,
  labels,
  color,
  caption,
  name,
  format,
}: {
  values: (number | null)[];
  labels: string[];
  color: string;
  caption: string;
  name: string;
  format: (value: number) => string;
}) {
  const known = values.filter((v): v is number => v != null);
  const option = useMemo<echarts.EChartsCoreOption>(
    () => ({
      grid: { left: 3, right: 4, top: 4, bottom: 3 },
      tooltip: {
        trigger: "axis",
        confine: true,
        padding: [4, 8],
        textStyle: { fontSize: 11, color: ink },
        axisPointer: { type: "none" },
        formatter: (items: { dataIndex: number; value: number | null }[]) => {
          const item = items[0];
          return item
            ? `${labels[item.dataIndex]}<br/><b>${item.value == null ? "—" : format(item.value)}</b>`
            : "";
        },
      },
      xAxis: { type: "category", show: false, boundaryGap: false, data: labels },
      yAxis: { type: "value", show: false, scale: true },
      series: [
        {
          type: "line",
          data: values.map((value, index) =>
            index === values.length - 1 && value != null
              ? { value, symbol: "circle", symbolSize: 5 }
              : value,
          ),
          connectNulls: true,
          showSymbol: false,
          symbol: "circle",
          lineStyle: { width: 1.8, color, cap: "round", join: "round" },
          itemStyle: { color },
          areaStyle: { color, opacity: 0.12 },
        },
      ],
    }),
    [values, labels, color, format],
  );
  const last = known.at(-1);
  return (
    <div className="co-kpi-spark">
      {known.length ? (
        <EChart
          option={option}
          height={32}
          label={`${name}${caption}趋势，最近值 ${last == null ? "—" : format(last)}`}
        />
      ) : (
        <span className="co-kpi-spark-empty" aria-label={`${name}${caption}暂无趋势`}>
          —
        </span>
      )}
      <span className="co-kpi-spark-label">{caption}</span>
    </div>
  );
}

export type DumbbellRow = {
  name: string;
  detail?: string;
  p50: number | null;
  p95: number | null;
  danger?: boolean;
};

/** P50 → P95 dumbbell chart for latency distributions. */
export function Dumbbell({ rows, label }: { rows: DumbbellRow[]; label: string }) {
  const option = useMemo<echarts.EChartsCoreOption>(() => {
    const data = rows.filter((r) => r.p50 != null && r.p95 != null);
    const names = data.map((r) => r.name);
    const tone = (r: DumbbellRow, index: number) =>
      r.danger ? "#a34236" : index < 2 ? "#1c5145" : index < 4 ? "#2b7b99" : "#c5882a";
    const renderItem: CustomSeriesOption["renderItem"] = (params, api) => {
      const start = api.coord([api.value(1), api.value(0)]);
      const end = api.coord([api.value(2), api.value(0)]);
      const row = data[params.dataIndex];
      return {
        type: "line",
        shape: { x1: start[0], y1: start[1], x2: end[0], y2: end[1] },
        style: {
          stroke: row ? tone(row, params.dataIndex) : muted,
          lineWidth: 4,
          lineCap: "round",
        },
      };
    };
    return {
      grid: { left: 8, right: 96, top: 12, bottom: 26, containLabel: true },
      tooltip: {
        trigger: "item",
        confine: true,
        textStyle: { fontSize: 12, color: ink },
        formatter: (item: { dataIndex: number }) => {
          const row = data[item.dataIndex];
          return row
            ? `${row.detail ?? row.name}<br/>P50 ${compactMs(row.p50)} · P95 ${compactMs(row.p95)}`
            : "";
        },
      },
      xAxis: {
        type: "value",
        min: 0,
        axisLabel: { color: muted, fontSize: 11, formatter: (v: number) => compactMs(v) },
        splitLine: { lineStyle: { color: grid, type: "dashed" } },
        axisLine: { show: false },
      },
      yAxis: {
        type: "category",
        inverse: true,
        data: names,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: "#dce2da" } },
        axisLabel: {
          fontSize: 12,
          fontWeight: 600,
          color: (_: unknown, index: number) => (data[index]?.danger ? "#a34236" : ink),
        },
      },
      series: [
        {
          type: "custom",
          renderItem,
          encode: { x: [1, 2], y: 0 },
          data: data.map((r, index) => [index, r.p50, r.p95]),
          z: 1,
        },
        {
          type: "scatter",
          symbolSize: 10,
          itemStyle: { color: "#1c5145" },
          data: data.map((r, index) => [r.p50, index]),
          z: 2,
        },
        {
          type: "scatter",
          symbolSize: 10,
          data: data.map((r, index) => ({
            value: [r.p95, index],
            itemStyle: { color: r.danger ? "#a34236" : "#c5882a" },
          })),
          label: {
            show: true,
            position: "right",
            distance: 8,
            fontSize: 11,
            color: muted,
            formatter: (item: { dataIndex: number }) => {
              const row = data[item.dataIndex];
              return row ? `${compactMs(row.p50)} → ${compactMs(row.p95)}` : "";
            },
          },
          z: 3,
        },
      ],
    };
  }, [rows]);
  const visible = rows.filter((r) => r.p50 != null && r.p95 != null).length;
  return visible ? (
    <EChart option={option} label={label} height={Math.max(140, visible * 34 + 48)} />
  ) : (
    <p className="co-empty">当前筛选条件下暂无耗时样本</p>
  );
}

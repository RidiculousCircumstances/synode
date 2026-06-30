export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function formatDateTime(value?: string | null): string {
  if (!value) {
    return "-";
  }
  return new Date(value).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatBytes(value: number | undefined | null): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return "0 B";
  }
  const mb = value / 1024 / 1024;
  if (mb < 1024) {
    return `${mb.toFixed(1)} MB`;
  }
  return `${(mb / 1024).toFixed(2)} GB`;
}

export function formatUnknown(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

export function nestedUnknown(value: unknown, path: string[]): unknown {
  let current = value;
  for (const segment of path) {
    if (!current || typeof current !== "object" || !(segment in current)) {
      return null;
    }
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

export function nestedString(value: unknown, path: string[]): string {
  const nested = nestedUnknown(value, path);
  return typeof nested === "string" ? nested : "";
}

export function asPercent(value: number | undefined | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "-";
  }
  return `${value.toFixed(1)}%`;
}

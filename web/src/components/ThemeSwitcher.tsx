"use client";

import { Palette } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

import { THEME_SWITCHER_OPTIONS } from "@/lib/themes";
import { cn } from "@/lib/utils";

export function ThemeSwitcher({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) {
    return <div className={cn("theme-switcher skeleton", className)} aria-hidden />;
  }

  return (
    <label className={cn("theme-switcher", className)}>
      <Palette size={16} aria-hidden />
      <select aria-label="Theme" value={theme ?? "system"} onChange={(event) => setTheme(event.target.value)}>
        {THEME_SWITCHER_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

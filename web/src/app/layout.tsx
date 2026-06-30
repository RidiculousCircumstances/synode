import "@xyflow/react/dist/style.css";
import "./globals.css";

import type { Metadata } from "next";
import type { ReactNode } from "react";

import AppShell from "@/components/AppShell";
import Providers from "./providers";

export const metadata: Metadata = {
  title: "Synode",
  description: "Multi-agent coding and analysis runtime",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="h-full" suppressHydrationWarning>
      <body className="min-h-screen bg-background text-foreground antialiased">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}

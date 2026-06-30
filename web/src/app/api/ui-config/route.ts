import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json({
    apiBaseUrl: process.env.SYNODE_UI_API_BASE_URL ?? "auto",
    apiPort: process.env.SYNODE_UI_API_PORT ?? "8787",
  });
}

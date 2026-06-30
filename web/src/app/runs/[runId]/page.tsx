import { Suspense } from "react";

import RunDetailClient from "@/components/RunDetailClient";
import { EmptyState } from "@/components/ui/primitives";

export const dynamic = "force-dynamic";

export default async function RunDetailPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  return (
    <Suspense fallback={<EmptyState title="Loading run workspace" text={runId} />}>
      <RunDetailClient runId={runId} />
    </Suspense>
  );
}

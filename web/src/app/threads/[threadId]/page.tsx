import { Suspense } from "react";

import ThreadDetailClient from "@/components/ThreadDetailClient";
import { EmptyState } from "@/components/ui/primitives";

export const dynamic = "force-dynamic";

export default async function ThreadDetailPage({ params }: { params: Promise<{ threadId: string }> }) {
  const { threadId } = await params;
  return (
    <Suspense fallback={<EmptyState title="Loading thread" text={threadId} />}>
      <ThreadDetailClient threadId={threadId} />
    </Suspense>
  );
}

import WorkflowDetailClient from "@/components/workflows/WorkflowDetailClient";

export default async function WorkflowDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <WorkflowDetailClient workflowId={id} />;
}

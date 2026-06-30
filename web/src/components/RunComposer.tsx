"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { createRun } from "@/lib/api";
import type { RunMode } from "@/types";

export default function RunComposer() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [task, setTask] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [provider, setProvider] = useState("ollama");
  const [mode, setMode] = useState<RunMode>("general");

  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: (run) => {
      setTask("");
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      router.push(`/runs/${run.id}`);
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!task.trim()) {
      return;
    }
    mutation.mutate({
      task: task.trim(),
      workspace: workspace.trim() || null,
      model_provider: provider.trim() || null,
      mode,
    });
  };

  return (
    <form className="composer-shell" onSubmit={submit}>
      <label className="field field-task">
        <span>Task</span>
        <textarea
          value={task}
          onChange={(event) => setTask(event.target.value)}
          placeholder="Describe the coding, analysis, research, or database task"
          rows={7}
        />
      </label>
      <div className="form-grid">
        <label className="field">
          <span>Mode</span>
          <select value={mode} onChange={(event) => setMode(event.target.value as RunMode)}>
            <option value="general">general</option>
            <option value="coding">coding</option>
          </select>
        </label>
        <label className="field">
          <span>Provider</span>
          <input value={provider} onChange={(event) => setProvider(event.target.value)} />
        </label>
      </div>
      <label className="field">
        <span>Workspace</span>
        <input value={workspace} onChange={(event) => setWorkspace(event.target.value)} placeholder="/workspace/project" />
      </label>
      {mutation.error ? <div className="error-line">{mutation.error.message}</div> : null}
      <button className="primary-button" type="submit" disabled={mutation.isPending || !task.trim()}>
        {mutation.isPending ? <RefreshCw size={16} aria-hidden className="spin" /> : <Play size={16} aria-hidden />}
        <span>Run</span>
      </button>
    </form>
  );
}
